import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import json
import re
import time
import requests
from math import radians, cos, sin, asin, sqrt
import googlemaps
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import threading

# Load environment variables
# Get the directory where this script is located
script_dir = os.path.dirname(os.path.abspath(__file__))
env_file = os.path.join(script_dir, '.env')
load_dotenv(env_file)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class LocationBot:
    def __init__(self):
        self.bot_token = os.getenv('BOT_TOKEN')
        self.authorized_users = [int(user_id) for user_id in os.getenv('AUTHORIZED_USERS', '').split(',') if user_id.strip()]
        self.eld_url = os.getenv('ELD_URL')
        self.google_maps_api_key = os.getenv('GOOGLE_MAPS_API_KEY')
        
        # Load drivers configuration from JSON file
        # Use the script directory that was already determined
        self.drivers_config_file = os.path.join(script_dir, 'drivers_config.json')
        self.drivers_config = self.load_drivers_config()
        
        # Create a mapping from chat_id to driver info for quick lookup
        self.chat_to_driver = {}
        for driver in self.drivers_config.get('drivers', []):
            if driver.get('telegram_group_id'):
                self.chat_to_driver[str(driver['telegram_group_id'])] = driver
                logger.info(f"✅ Loaded driver mapping: Chat {driver['telegram_group_id']} -> {driver['name']} (Unit: {driver['unit_number']})")
        
        logger.info(f"📊 Total driver mappings loaded: {len(self.chat_to_driver)}")
        
        # Performance optimizations
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.cache_duration = 15  # Reduce cache duration to 15 seconds for fresher data
        self.executor = ThreadPoolExecutor(max_workers=15)  # Increase workers for better concurrency with multiple groups
        self.selenium_semaphore = threading.Semaphore(8)  # Limit concurrent Selenium instances to prevent resource exhaustion
        
        # Auto-update settings
        self.auto_update_interval = 7200  # 2 hours for automatic updates
        self.application = None
        
        # Store destination addresses and individual timers for each group
        self.group_destinations = {}  # {chat_id: destination_address}
        self.group_update_tasks = {}  # {chat_id: asyncio.Task} - individual timer tasks for each group
        
        # Track driver stop times for extended stop alerts
        self.driver_stop_times = {}  # {driver_url: {'stopped_since': datetime, 'location': str, 'notified': bool}}
        self.extended_stop_threshold = 45 * 60  # 45 minutes in seconds
        
        # Geocoding cache to prevent inconsistent coordinates
        self.geocoding_cache = {}  # {address: (lat, lon, timestamp)}
        self.geocoding_cache_duration = 3600  # 1 hour cache for addresses
        
        # Distance validation to prevent incorrect calculations
        self.distance_cache = {}  # {(chat_id, destination): {'distance': float, 'timestamp': datetime, 'driver_location': str}}
        self.distance_cache_duration = 60  # 1 minute cache for distance calculations
        
        
        if not self.bot_token:
            raise ValueError("BOT_TOKEN not found in environment variables")
        
        # ELD_URL is not needed if drivers_config.json has driver configurations
        if not self.eld_url and not self.drivers_config.get('drivers'):
            raise ValueError("ELD_URL or drivers_config.json with driver configurations not found")
        
        # Initialize Google Maps client if API key is provided
        self.gmaps = None
        self.gmaps_distance_matrix_available = False
        if self.google_maps_api_key and self.google_maps_api_key.strip():
            try:
                self.gmaps = googlemaps.Client(key=self.google_maps_api_key)
                logger.info("Google Maps API client initialized successfully")
                
                # Test the API key with geocoding first
                try:
                    test_result = self.gmaps.geocode("New York, NY")
                    if test_result:
                        logger.info("Google Maps Geocoding API validated successfully")
                        
                        # Test Distance Matrix API specifically
                        try:
                            test_matrix = self.gmaps.distance_matrix(
                                origins=["New York, NY"],
                                destinations=["Los Angeles, CA"],
                                mode="driving",
                                units="imperial"
                            )
                            if test_matrix.get('status') == 'OK':
                                logger.info("Google Maps Distance Matrix API validated successfully")
                                self.gmaps_distance_matrix_available = True
                            else:
                                logger.warning(f"Google Maps Distance Matrix API validation failed: {test_matrix.get('status')} - {test_matrix.get('error_message', 'Unknown error')}")
                        except Exception as e:
                            logger.error(f"Google Maps Distance Matrix API validation failed: {e}")
                            
                    else:
                        logger.error("Google Maps Geocoding API validation failed - no results")
                        self.gmaps = None
                except Exception as e:
                    logger.error(f"Google Maps API key validation failed: {e}")
                    self.gmaps = None
                    
            except Exception as e:
                logger.error(f"Failed to initialize Google Maps client: {e}")
                self.gmaps = None
        else:
            logger.warning("Google Maps API key not found or empty. Using fallback distance calculation.")
            
        # Log the final API status
        if self.gmaps:
            if self.gmaps_distance_matrix_available:
                logger.info("✅ Google Maps fully functional (Geocoding + Distance Matrix)")
            else:
                logger.warning("⚠️ Google Maps partially functional (Geocoding only, Distance Matrix unavailable)")
        else:
            logger.warning("❌ Google Maps API unavailable - using fallback methods")
    
    def osrm_distance(self, origin_lat, origin_lon, dest_lat, dest_lon):
        """Calculate driving distance and time using OSRM public API"""
        try:
            logger.info(f"Calculating OSRM distance from ({origin_lat}, {origin_lon}) to ({dest_lat}, {dest_lon})")
            
            # Build OSRM API URL
            osrm_url = f"http://router.project-osrm.org/route/v1/driving/{origin_lon},{origin_lat};{dest_lon},{dest_lat}?overview=false"
            headers = {'User-Agent': 'TelegramBot/1.0'}
            
            # Request OSRM API
            response = requests.get(osrm_url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data['routes']:
                    distance_meters = data['routes'][0]['distance']
                    duration_seconds = data['routes'][0]['duration']
                    
                    # OSRM always returns distance in meters, convert to miles
                    distance_miles = distance_meters * 0.000621371
                    # Convert seconds to minutes and hours
                    duration_minutes = duration_seconds / 60
                    duration_hours = duration_minutes / 60
                    
                    # Format duration text based on hours
                    if duration_hours >= 1:
                        duration_text = f"{duration_hours:.1f} hr"
                    else:
                        duration_text = f"{duration_minutes:.0f} min"
                    
                    logger.info(f"OSRM distance calculated: {distance_miles:.2f} miles, {duration_hours:.1f} hours")
                    return {
                        'distance_miles': distance_miles,
                        'distance_text': f"{distance_miles:.1f} mi",
                        'duration_text': duration_text,
                        'duration_minutes': duration_minutes,
                        'duration_hours': duration_hours,
                        'method': 'OSRM API'
                    }
                else:
                    logger.warning("OSRM returned no routes")
            else:
                logger.error(f"OSRM API returned status code: {response.status_code}")
            return None
        except Exception as e:
            logger.error(f"OSRM distance calculation error: {e}")
            return None

    
    def is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized to use the bot"""
        # Allow everyone to use the bot
        return True
    
    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two points using Haversine formula"""
        try:
            logger.info(f"Calculating haversine distance between ({lat1}, {lon1}) and ({lat2}, {lon2})")
            
            # Validate inputs
            if not all(isinstance(x, (int, float)) for x in [lat1, lon1, lat2, lon2]):
                logger.error("Invalid coordinate types")
                return None
            
            # Convert to radians
            lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
            
            # Haversine formula
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
            c = 2 * asin(sqrt(a))
            
            # Radius of earth in miles
            r = 3959
            
            distance = c * r
            # Estimated duration (assuming average speed)
            avg_speed_mph = 60  # Assuming a default average speed
            duration_minutes = (distance / avg_speed_mph) * 60
            duration_hours = duration_minutes / 60
            logger.info(f"Haversine distance calculated: {distance:.2f} miles, duration: {duration_hours:.1f} hours")
            return {
                'distance_miles': distance,
                'duration_minutes': duration_minutes,
                'duration_hours': duration_hours
            }
        except Exception as e:
            logger.error(f"Haversine distance calculation error: {e}")
            return None
    
    def parse_and_clean_address(self, address):
        """Parse and clean address for better geocoding success"""
        try:
            import re
            
            # Remove extra whitespace and convert to title case
            address = ' '.join(address.split())
            
            # Common address variations to try
            variations = []
            
            # Original address
            variations.append(address)
            
            # Remove business names at the beginning
            business_removed = re.sub(r'^[A-Z\s]+\s+(?=\d)', '', address)
            if business_removed != address:
                variations.append(business_removed.strip())
            
            # Try with different route formats
            # Convert "US-9" to "Route 9" or "US Route 9"
            us_route_match = re.search(r'US-?(\d+)', address, re.IGNORECASE)
            if us_route_match:
                route_num = us_route_match.group(1)
                variations.append(address.replace(us_route_match.group(0), f"Route {route_num}"))
                variations.append(address.replace(us_route_match.group(0), f"US Route {route_num}"))
                variations.append(address.replace(us_route_match.group(0), f"Highway {route_num}"))
            
            # Try with "RTE" converted to "Route"
            if 'RTE' in address.upper():
                variations.append(re.sub(r'RTE\s*(\d+)', r'Route \1', address, flags=re.IGNORECASE))
                variations.append(re.sub(r'RTE\s*(\d+)', r'Highway \1', address, flags=re.IGNORECASE))
            
            # Try without specific building/business names
            # Remove words like "HANNAFORD", "BROTHERS", etc.
            simple_address = re.sub(r'^[A-Z\s]+(?=\d)', '', address).strip()
            if simple_address and simple_address != address:
                variations.append(simple_address)
            
            # Try with just the street number and main components
            street_match = re.search(r'(\d+)\s+([^,]+),\s*([^,]+),\s*([A-Z]{2})', address)
            if street_match:
                number, street, city, state = street_match.groups()
                variations.append(f"{number} {street}, {city}, {state}")
                variations.append(f"{street}, {city}, {state}")
                variations.append(f"{city}, {state}")
            
            # Remove duplicates while preserving order
            seen = set()
            unique_variations = []
            for var in variations:
                if var and var not in seen:
                    seen.add(var)
                    unique_variations.append(var)
            
            logger.info(f"Address variations to try: {unique_variations}")
            return unique_variations
            
        except Exception as e:
            logger.error(f"Error parsing address: {e}")
            return [address]
    
    def get_cached_geocoding(self, address):
        """Get cached geocoding result if available and valid"""
        if address in self.geocoding_cache:
            lat, lon, timestamp = self.geocoding_cache[address]
            if datetime.now() - timestamp < timedelta(seconds=self.geocoding_cache_duration):
                logger.info(f"Using cached geocoding for: {address} -> ({lat}, {lon})")
                return lat, lon
            else:
                # Remove expired cache
                del self.geocoding_cache[address]
        return None, None
    
    def set_geocoding_cache(self, address, lat, lon):
        """Cache geocoding result"""
        self.geocoding_cache[address] = (lat, lon, datetime.now())
        logger.info(f"Cached geocoding for: {address} -> ({lat}, {lon})")
    
    def geocode_address(self, address):
        """Get coordinates from address using multiple strategies and fallbacks"""
        try:
            logger.info(f"Geocoding address: {address}")
            
            # Check cache first
            cached_lat, cached_lon = self.get_cached_geocoding(address)
            if cached_lat is not None and cached_lon is not None:
                return cached_lat, cached_lon
            
            # Get address variations
            address_variations = self.parse_and_clean_address(address)
            
            # Try Google Maps first if available
            if self.gmaps:
                for addr_variant in address_variations:
                    try:
                        logger.info(f"Attempting Google Maps geocoding for: {addr_variant}")
                        geocode_result = self.gmaps.geocode(addr_variant)
                        if geocode_result:
                            location = geocode_result[0]['geometry']['location']
                            lat, lng = location['lat'], location['lng']
                            logger.info(f"Google Maps geocoding successful: ({lat}, {lng}) for variant: {addr_variant}")
                            # Cache the result
                            self.set_geocoding_cache(address, lat, lng)
                            return lat, lng
                    except Exception as e:
                        logger.error(f"Google Maps geocoding error for '{addr_variant}': {e}")
                        continue
            
            # Fallback to OpenStreetMap Nominatim with multiple strategies
            logger.info("Using OpenStreetMap Nominatim fallback")
            
            for addr_variant in address_variations:
                try:
                    # Try exact search first
                    url = f"https://nominatim.openstreetmap.org/search?q={addr_variant}&format=json&limit=3&countrycodes=us"
                    headers = {'User-Agent': 'TelegramBot/1.0'}
                    
                    response = requests.get(url, headers=headers, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        if data:
                            # Take the first result
                            lat, lon = float(data[0]['lat']), float(data[0]['lon'])
                            logger.info(f"OpenStreetMap geocoding successful: ({lat}, {lon}) for variant: {addr_variant}")
                            # Cache the result
                            self.set_geocoding_cache(address, lat, lon)
                            return lat, lon
                except Exception as e:
                    logger.error(f"OpenStreetMap error for '{addr_variant}': {e}")
                    continue
            
            # Try with structured search if all else fails
            logger.info("Trying structured search as final fallback")
            for addr_variant in address_variations:
                try:
                    # Try to parse structured components
                    import re
                    match = re.search(r'(\d+)\s+([^,]+),\s*([^,]+),\s*([A-Z]{2})', addr_variant)
                    if match:
                        number, street, city, state = match.groups()
                        
                        # Structured search
                        structured_url = f"https://nominatim.openstreetmap.org/search?format=json&housenumber={number}&street={street}&city={city}&state={state}&country=us&limit=1"
                        
                        response = requests.get(structured_url, headers=headers, timeout=10)
                        if response.status_code == 200:
                            data = response.json()
                            if data:
                                lat, lon = float(data[0]['lat']), float(data[0]['lon'])
                                logger.info(f"Structured search successful: ({lat}, {lon}) for: {addr_variant}")
                                # Cache the result
                                self.set_geocoding_cache(address, lat, lon)
                                return lat, lon
                except Exception as e:
                    logger.error(f"Structured search error for '{addr_variant}': {e}")
                    continue
            
            # Final fallback - try just city and state
            logger.info("Trying city/state fallback")
            for addr_variant in address_variations:
                try:
                    import re
                    match = re.search(r'([^,]+),\s*([A-Z]{2})', addr_variant)
                    if match:
                        city, state = match.groups()
                        city_url = f"https://nominatim.openstreetmap.org/search?q={city}, {state}&format=json&limit=1&countrycodes=us"
                        
                        response = requests.get(city_url, headers=headers, timeout=10)
                        if response.status_code == 200:
                            data = response.json()
                            if data:
                                lat, lon = float(data[0]['lat']), float(data[0]['lon'])
                                logger.info(f"City/state fallback successful: ({lat}, {lon}) for: {city}, {state}")
                                # Cache the result
                                self.set_geocoding_cache(address, lat, lon)
                                return lat, lon
                except Exception as e:
                    logger.error(f"City/state fallback error: {e}")
                    continue
            
            logger.error(f"All geocoding attempts failed for address: {address}")
            return None, None
            
        except Exception as e:
            logger.error(f"Geocoding error for '{address}': {e}")
            return None, None
    
    def parse_driver_location(self, location_str):
        """Extract coordinates from driver location string"""
        try:
            # For now, we'll use geocoding for the driver location too
            # In the future, you could extract GPS coordinates directly if available
            return self.geocode_address(location_str)
        except:
            return None, None
    
    def is_distance_valid(self, chat_id, destination, new_distance, driver_location):
        """Validate distance calculation to prevent backtracking issues"""
        if (chat_id, destination) not in self.distance_cache:
            # No previous record, so cache it
            self.distance_cache[(chat_id, destination)] = {
                'distance': new_distance,
                'timestamp': datetime.now(),
                'driver_location': driver_location
            }
            logger.info(f"Distance cache set for chat {chat_id} to destination {destination}")
            return True
        
        cached_data = self.distance_cache[(chat_id, destination)]
        cached_distance = cached_data['distance']
        cache_time_diff = (datetime.now() - cached_data['timestamp']).total_seconds()
        # If cache is still valid (1 minute) and new distance is greater, invalidate it
        if cache_time_diff < self.distance_cache_duration and new_distance > cached_distance:
            logger.warning(f"Distance validation failed. Cached: {cached_distance}, New: {new_distance}")
            return False
        # Update cache
        self.distance_cache[(chat_id, destination)] = {
            'distance': new_distance,
            'timestamp': datetime.now(),
            'driver_location': driver_location
        }
        logger.info(f"Distance cache updated for chat {chat_id} to destination {destination}")
        return True

    def calculate_distance_and_time(self, origin_address, destination_address, chat_id=None, driver_location=None):
        """Calculate distance and travel time using Google Maps Distance Matrix API"""
        try:
            logger.info(f"Calculating distance from '{origin_address}' to '{destination_address}'")
            
            # Clean the addresses
            origin_address = self.sanitize_address(origin_address)
            destination_address = self.sanitize_address(destination_address)
            logger.info(f"Sanitized addresses - Origin: '{origin_address}', Destination: '{destination_address}'")
            
            # Try Google Maps Distance Matrix API if available
            if self.gmaps and self.gmaps_distance_matrix_available:
                try:
                    logger.info("Using Google Maps Distance Matrix API for accurate driving distance")
                    matrix = self.gmaps.distance_matrix(
                        origins=[origin_address],
                        destinations=[destination_address],
                        mode="driving",
                        units="imperial",
                        avoid="tolls"
                    )
                    
                    logger.info(f"Google Maps API response status: {matrix.get('status')}")
                    
                    if matrix.get('status') == 'OK':
                        rows = matrix.get('rows', [])
                        if rows and len(rows) > 0:
                            elements = rows[0].get('elements', [])
                            if elements and len(elements) > 0:
                                element = elements[0]
                                if element.get('status') == 'OK':
                                    distance_info = element.get('distance', {})
                                    duration_info = element.get('duration', {})
                                    
                                    if distance_info and duration_info:
                                        distance_text = distance_info.get('text', 'Unknown')
                                        duration_text = duration_info.get('text', 'Unknown')
                                        
                                        # Parse distance value
                                        distance_value = distance_info.get('value', 0)  # meters
                                        distance_miles = distance_value * 0.000621371  # Convert meters to miles
                                        
                                        # Parse duration value
                                        duration_seconds = duration_info.get('value', 0)
                                        duration_minutes = duration_seconds / 60
                                        duration_hours = duration_minutes / 60
                                        
                                        # Validate distance value
                                        if chat_id is not None and not self.is_distance_valid(chat_id, destination_address, distance_miles, origin_address):
                                            logger.error("Invalid distance calculation: backtracking or inconsistency detected")
                                            return None
                                        
                                        # Convert Google Maps duration text to hours if needed
                                        if duration_hours >= 1:
                                            duration_text = f"{duration_hours:.1f} hr"
                                        else:
                                            duration_text = f"{duration_minutes:.0f} min"
                                        
                                        logger.info(f"✅ Google Maps calculation successful: {distance_text}, {duration_text}")
                                        
                                        return {
                                            'distance_miles': distance_miles,
                                            'distance_text': distance_text,
                                            'duration_text': duration_text,
                                            'duration_minutes': duration_minutes,
                                            'duration_hours': duration_hours,
                                            'method': 'Google Maps Distance Matrix API'
                                        }
                                    else:
                                        logger.error("Google Maps API missing distance or duration info")
                                else:
                                    element_status = element.get('status', 'UNKNOWN')
                                    if element_status == 'NOT FOUND':
                                        logger.warning(f"Google Maps could not find route: One or both addresses not found")
                                    elif element_status == 'ZERO RESULTS':
                                        logger.warning(f"Google Maps found no route between addresses")
                                    else:
                                        logger.warning(f"Google Maps element status: {element_status}")
                            else:
                                logger.error("Google Maps API returned empty elements")
                        else:
                            logger.error("Google Maps API returned empty rows")
                    else:
                        api_status = matrix.get('status', 'UNKNOWN')
                        error_message = matrix.get('error_message', 'No error message')
                        if api_status == 'REQUEST_DENIED':
                            logger.error(f"Google Maps API request denied: {error_message}")
                            logger.error("This usually means the API key is invalid or Distance Matrix API is not enabled")
                            # Disable Distance Matrix API for future requests
                            self.gmaps_distance_matrix_available = False
                        elif api_status == 'OVER_QUERY_LIMIT':
                            logger.warning(f"Google Maps API quota exceeded: {error_message}")
                        else:
                            logger.warning(f"Google Maps API status: {api_status} - {error_message}")
                            
                except Exception as e:
                    logger.error(f"Google Maps Distance Matrix API error: {e}")
                    # If we get repeated errors, disable Distance Matrix for this session
                    self.gmaps_distance_matrix_available = False
            elif self.gmaps:
                logger.info("Google Maps available but Distance Matrix API disabled, using geocoding + haversine")
            else:
                logger.info("Google Maps API not available, using fallback methods")
            
            # Attempt to use OSRM for driving distance
            logger.info("Trying OSRM API for driving distance calculation")
            origin_lat, origin_lon = self.geocode_address(origin_address)
            dest_lat, dest_lon = self.geocode_address(destination_address)
            
            if origin_lat is None or dest_lat is None:
                logger.error("Failed to geocode one or both addresses")
                if origin_lat is None:
                    logger.error(f"❌ Could not geocode origin address: '{origin_address}'")
                if dest_lat is None:
                    logger.error(f"❌ Could not geocode destination address: '{destination_address}'")
                return None
            
            osrm_result = self.osrm_distance(origin_lat, origin_lon, dest_lat, dest_lon)
            if osrm_result is not None:
                # Validate distance value
                if chat_id is not None and not self.is_distance_valid(chat_id, destination_address, osrm_result['distance_miles'], origin_address):
                    logger.error("Invalid distance calculation: backtracking or inconsistency detected")
                    return None
                return osrm_result
            
            # Fallback to haversine calculation
            logger.info("🔄 Using fallback haversine calculation (straight-line distance)")
            haversine_result = self.haversine_distance(origin_lat, origin_lon, dest_lat, dest_lon)
            if haversine_result is None:
                logger.error("Haversine distance calculation failed")
                return None
            
            distance = haversine_result['distance_miles']
            duration_minutes = haversine_result['duration_minutes']
            duration_hours = haversine_result['duration_hours']
            
            # Validate distance value
            if chat_id is not None and not self.is_distance_valid(chat_id, destination_address, distance, origin_address):
                logger.error("Invalid distance calculation: backtracking or inconsistency detected")
                return None
            
            logger.info(f"✅ Haversine calculation successful: {distance:.1f} miles (straight-line)")
            
            # Format duration text based on hours
            if duration_hours >= 1:
                duration_text = f"{duration_hours:.1f} hr (estimated)"
            else:
                duration_text = f"{duration_minutes:.0f} min (estimated)"
            
            # Add note about the calculation method
            fallback_method = "Haversine (straight-line)"
            if not self.gmaps:
                fallback_method += " - Google Maps API unavailable"
            elif not self.gmaps_distance_matrix_available:
                fallback_method += " - Distance Matrix API disabled"
            
            return {
                'distance_miles': distance,
                'distance_text': f"{distance:.1f} mi (straight-line)",
                'duration_text': duration_text,
                'duration_minutes': duration_minutes,
                'duration_hours': duration_hours,
                'method': fallback_method
            }
            
        except Exception as e:
            logger.error(f"Error in calculate_distance_and_time: {e}")
            return None

    def load_drivers_config(self):
        """Load drivers configuration from JSON file"""
        try:
            with open(self.drivers_config_file, 'r') as f:
                config = json.load(f)
            logger.info(f"Loaded {len(config.get('drivers', []))} drivers from configuration")
            return config
        except FileNotFoundError:
            logger.warning(f"Drivers config file {self.drivers_config_file} not found. Creating empty config.")
            return {'drivers': []}
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in {self.drivers_config_file}: {e}")
            return {'drivers': []}
        except Exception as e:
            logger.error(f"Error loading drivers config: {e}")
            return {'drivers': []}

    def save_drivers_config(self):
        """Save drivers configuration to JSON file"""
        try:
            with open(self.drivers_config_file, 'w') as f:
                json.dump(self.drivers_config, f, indent=2)
            logger.info("Drivers configuration saved successfully")
            return True
        except Exception as e:
            logger.error(f"Error saving drivers config: {e}")
            return False
    
    def get_driver_by_chat_id(self, chat_id):
        """Get driver configuration for a specific chat ID"""
        return self.chat_to_driver.get(str(chat_id))
    
    def get_eld_url_for_group(self, chat_id):
        """Get the ELD URL for a given Telegram group ID."""
        # First check if there's a specific driver mapped to this chat
        driver = self.get_driver_by_chat_id(chat_id)
        if driver:
            logger.info(f"🎯 Found driver mapping for chat {chat_id}: {driver['name']} (Unit: {driver['unit_number']})")
            return driver['eld_url']
        
        # Log warning if no driver is assigned to this group
        logger.warning(f"⚠️ No driver assigned to chat {chat_id}. Use /setdriver [driver_name] to assign a driver.")
        
        # Return None instead of default ELD_URL to force proper assignment
        return None
    
    def set_driver_for_group(self, chat_id, driver_name):
        """Set which driver to track for a specific group"""
        # Find the driver by name
        driver = None
        for d in self.drivers_config.get('drivers', []):
            if d['name'].lower() == driver_name.lower():
                driver = d
                break
        
        if not driver:
            return False, f"Driver '{driver_name}' not found in configuration"
        
        # Check if this driver is already assigned to another group
        if driver.get('telegram_group_id') and driver['telegram_group_id'] != chat_id:
            old_chat_id = driver['telegram_group_id']
            logger.info(f"🔄 Driver {driver['name']} reassigned from chat {old_chat_id} to chat {chat_id}")
            # Remove old mapping
            if str(old_chat_id) in self.chat_to_driver:
                del self.chat_to_driver[str(old_chat_id)]
        
        # Check if this group already has a different driver assigned
        if str(chat_id) in self.chat_to_driver:
            old_driver = self.chat_to_driver[str(chat_id)]
            if old_driver['name'] != driver['name']:
                # Clear the old driver's assignment
                old_driver['telegram_group_id'] = None
                logger.info(f"🔄 Group {chat_id} reassigned from driver {old_driver['name']} to {driver['name']}")
        
        # Update the driver's telegram_group_id
        driver['telegram_group_id'] = chat_id
        
        # Update the chat_to_driver mapping
        self.chat_to_driver[str(chat_id)] = driver
        
        # Save the configuration
        if self.save_drivers_config():
            logger.info(f"✅ Successfully linked chat {chat_id} to driver {driver['name']} (Unit: {driver['unit_number']})")
            return True, f"Group linked to driver {driver['name']} (Unit: {driver['unit_number']})"
        else:
            return False, "Failed to save configuration"
    
    def list_available_drivers(self):
        """Get a list of all available drivers"""
        drivers = []
        for driver in self.drivers_config.get('drivers', []):
            drivers.append({
                'name': driver['name'],
                'unit_number': driver['unit_number'],
                'assigned_group': driver.get('telegram_group_id', None)
            })
        return drivers
    
    def get_cached_data(self, cache_key):
        """Get cached data if it's still valid"""
        with self.cache_lock:
            if cache_key in self.cache:
                data, timestamp = self.cache[cache_key]
                if datetime.now() - timestamp < timedelta(seconds=self.cache_duration):
                    logger.info(f"Cache hit for {cache_key}")
                    return data
                else:
                    # Remove expired cache
                    del self.cache[cache_key]
        return None
    
    def set_cached_data(self, cache_key, data):
        """Set cached data with timestamp"""
        with self.cache_lock:
            self.cache[cache_key] = (data, datetime.now())
            logger.info(f"Cache set for {cache_key}")
    
    def get_driver_status(self, driver_data):
        """Determine driver status based on speed"""
        try:
            # Extract speed value from speed string (e.g., "65.2 mph" -> 65.2)
            speed_str = driver_data.get('speed', '0 mph')
            speed_value = float(speed_str.replace(' mph', '').replace(',', ''))
            
            if speed_value > 0:
                return "🚗 Driving", speed_value
            else:
                return "🛑 Stopped", speed_value
        except (ValueError, AttributeError):
            return "❓ Unknown", 0
    
    def track_driver_stop_time(self, eld_url, driver_data):
        """Track how long a driver has been stopped"""
        try:
            status, speed = self.get_driver_status(driver_data)
            current_time = datetime.now()
            current_location = driver_data.get('location', 'Unknown')
            
            if speed == 0:  # Driver is stopped
                if eld_url not in self.driver_stop_times:
                    # First time we see this driver stopped
                    self.driver_stop_times[eld_url] = {
                        'stopped_since': current_time,
                        'location': current_location,
                        'notified': False
                    }
                    logger.info(f"Driver started stopping at {current_time}")
                else:
                    # Update location but keep the original stop time
                    self.driver_stop_times[eld_url]['location'] = current_location
            else:  # Driver is moving
                if eld_url in self.driver_stop_times:
                    # Driver started moving again, clear the stop time
                    del self.driver_stop_times[eld_url]
                    logger.info(f"Driver started moving again")
            
            return self.driver_stop_times.get(eld_url)
        except Exception as e:
            logger.error(f"Error tracking driver stop time: {e}")
            return None
    
    def check_extended_stop(self, eld_url):
        """Check if driver has been stopped for more than 45 minutes"""
        if eld_url not in self.driver_stop_times:
            return False, None
        
        stop_info = self.driver_stop_times[eld_url]
        current_time = datetime.now()
        stop_duration = (current_time - stop_info['stopped_since']).total_seconds()
        
        if stop_duration >= self.extended_stop_threshold and not stop_info['notified']:
            # Mark as notified to avoid spam
            stop_info['notified'] = True
            stop_duration_minutes = int(stop_duration // 60)
            return True, stop_duration_minutes
        
        return False, None
    
    def extract_driver_data_ultra_fast(self, eld_url):
        """Fast extraction using optimized Selenium with concurrency control"""
        # This page is a React app that loads content via JavaScript
        # So we need to use Selenium, but with optimized patterns and concurrency control
        with self.selenium_semaphore:
            return self.extract_driver_data_fast(eld_url)
    
    def extract_driver_data_fast(self, eld_url):
        """Fast extraction with minimal Chrome options"""
        try:
            # Ultra-fast Chrome options
            chrome_options = Options()
            chrome_options.add_argument('--headless')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--disable-images')
            chrome_options.add_argument('--disable-plugins')
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-web-security')
            # DON'T disable JavaScript - this React app needs it!
            # chrome_options.add_argument('--disable-javascript')
            # chrome_options.add_argument('--disable-css')
            chrome_options.add_argument('--disable-features=TranslateUI')
            chrome_options.add_argument('--disable-logging')
            chrome_options.add_argument('--disable-default-apps')
            chrome_options.add_argument('--no-first-run')
            chrome_options.add_argument('--no-default-browser-check')
            chrome_options.add_argument('--disable-backgrounding-occluded-windows')
            chrome_options.add_argument('--disable-renderer-backgrounding')
            chrome_options.add_argument('--disable-background-timer-throttling')
            chrome_options.add_argument('--disable-ipc-flooding-protection')
            chrome_options.add_argument('--window-size=1024,768')
            chrome_options.add_argument('--aggressive-cache-discard')
            chrome_options.add_argument('--memory-pressure-off')
            chrome_options.add_argument('--max_old_space_size=4096')
            
            # Initialize driver
            driver = webdriver.Chrome(options=chrome_options)
            driver.set_page_load_timeout(10)  # Increase timeout for reliability
            driver.implicitly_wait(3)  # Add implicit wait
            
            try:
                # Navigate to ELD page
                driver.get(eld_url)
                
                # Wait for React app to load content
                time.sleep(5)  # Give React app more time to load
                
                # Use WebDriverWait for additional safety
                try:
                    WebDriverWait(driver, 10).until(
                        lambda d: d.execute_script("return document.body.innerText.includes('Name')")
                    )
                except:
                    # Additional wait if needed
                    time.sleep(2)
                
                # Get page text immediately
                page_text = driver.execute_script("return document.body.innerText;")
                
                # Initialize driver data
                driver_data = {
                    'name': 'N/A',
                    'speed': 'N/A',
                    'location': 'N/A',
                    'truck_number': 'N/A'
                }
                
                # Fast regex extraction
                # Extract speed - handle multiple patterns including N/A
                speed_patterns = [
                    r'Speed\s*\n\s*\n\s*([\d\.]+)\s*mph',  # "0 mph" format
                    r'Speed\s*\n\s*\n\s*(N/A)',  # "N/A" format
                    r'([\d\.]+)\s*mph'  # Any number followed by mph
                ]
                
                for pattern in speed_patterns:
                    speed_match = re.search(pattern, page_text, re.IGNORECASE)
                    if speed_match:
                        if speed_match.group(1).upper() == 'N/A':
                            driver_data['speed'] = 'N/A'
                        else:
                            speed_val = float(speed_match.group(1))
                            driver_data['speed'] = f"{speed_val:.1f} mph"
                        break
                
                # Extract name - handle empty data
                name_patterns = [
                    r'Name\s*\n\s*\n\s*([A-Za-z\s]+?)\s*\n\s*\n\s*Truck Number',
                    r'Name\s+([A-Za-z\s]+?)\s+Truck Number',
                    r'Name\s*\n\s*\n\s*([^\n]+?)\s*\n\s*\n\s*Truck Number'
                ]
                
                for pattern in name_patterns:
                    name_match = re.search(pattern, page_text, re.IGNORECASE)
                    if name_match:
                        name_text = name_match.group(1).strip()
                        if name_text and len(name_text) > 0:
                            driver_data['name'] = name_text
                        else:
                            driver_data['name'] = 'No driver name available'
                        break
                
                # Extract location - handle "Open in Google Maps" case
                location_patterns = [
                    r'Current Location\s*\n\s*\n\s*([^\n]+)',
                    r'Current Location\s+([^\n]+)',
                    r'Current Location\s*\n\s*\n\s*([^\n\r]+)'
                ]
                
                for pattern in location_patterns:
                    location_match = re.search(pattern, page_text, re.IGNORECASE)
                    if location_match:
                        location_text = location_match.group(1).strip()
                        # Check if it's just "Open in Google Maps" (no real location)
                        if 'Open in Google Maps' in location_text or location_text.lower() in ['n/a', 'not available', 'offline']:
                            driver_data['location'] = 'Location not available (driver may be offline)'
                        else:
                            driver_data['location'] = location_text
                        break
                
                # Extract truck number
                truck_patterns = [
                    r'Truck Number\s*\n\s*\n\s*([^\n]+)',
                    r'Truck Number\s+([^\n]+)',
                    r'Truck Number\s*\n\s*\n\s*([\w\-]+)'
                ]
                
                for pattern in truck_patterns:
                    truck_match = re.search(pattern, page_text, re.IGNORECASE)
                    if truck_match:
                        truck_text = truck_match.group(1).strip()
                        if truck_text and len(truck_text) > 0:
                            driver_data['truck_number'] = truck_text
                        break
                
                # Log the extracted data for debugging
                logger.info(f"Extracted driver data: {driver_data}")
                
                # Location data extracted successfully
                
                return driver_data
                
            finally:
                driver.quit()
                
        except Exception as e:
            logger.error(f"Error in extract_driver_data_fast: {e}")
            return {
                'name': 'Error',
                'speed': 'Error',
                'location': 'Error extracting data'
            }

    def extract_driver_data(self, eld_url):
        """Extract driver data from ELD page using Selenium"""
        try:
            # Setup Chrome options for maximum speed while keeping functionality
            chrome_options = Options()
            chrome_options.add_argument('--headless')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--disable-images')
            chrome_options.add_argument('--disable-plugins')
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-web-security')
            chrome_options.add_argument('--disable-features=TranslateUI')
            chrome_options.add_argument('--disable-logging')
            chrome_options.add_argument('--disable-default-apps')
            chrome_options.add_argument('--no-first-run')
            chrome_options.add_argument('--no-default-browser-check')
            chrome_options.add_argument('--disable-blink-features=AutomationControlled')
            chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            chrome_options.add_argument('--window-size=1280,720')
            
            # Initialize driver with timeouts
            driver = webdriver.Chrome(options=chrome_options)
            driver.set_page_load_timeout(8)  # 8 second timeout
            driver.implicitly_wait(3)  # Add implicit wait
            
            try:
                # Navigate to ELD page
                driver.get(eld_url)
                
                # Use WebDriverWait for better performance
                try:
                    WebDriverWait(driver, 5).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                except:
                    pass  # Continue if timeout
                
                # Initialize driver data
                driver_data = {
                    'name': 'N/A',
                    'speed': 'N/A',
                    'location': 'N/A'
                }
                
                # Try to extract data using various methods
                page_source = driver.page_source
                
                # Try to find elements by common selectors
                name_selectors = [
                    '[data-testid="driver-name"]',
                    '.driver-name',
                    '#driver-name',
                    '.name',
                    '#name'
                ]
                
                for selector in name_selectors:
                    try:
                        element = driver.find_element(By.CSS_SELECTOR, selector)
                        if element and element.text:
                            driver_data['name'] = element.text.strip()
                            break
                    except:
                        continue
                
                # Try to find speed
                speed_selectors = [
                    '[data-testid="speed"]',
                    '.speed',
                    '#speed',
                    '.velocity',
                    '#velocity'
                ]
                
                for selector in speed_selectors:
                    try:
                        element = driver.find_element(By.CSS_SELECTOR, selector)
                        if element and element.text:
                            speed_text = element.text.strip()
                            speed_match = re.search(r'(\d+\.?\d*)\s*mph', speed_text, re.IGNORECASE)
                            if speed_match:
                                driver_data['speed'] = f"{speed_match.group(1)} mph"
                                break
                    except:
                        continue
                
                # Try to find location
                location_selectors = [
                    '[data-testid="location"]',
                    '.location',
                    '#location',
                    '.address',
                    '#address'
                ]
                
                for selector in location_selectors:
                    try:
                        element = driver.find_element(By.CSS_SELECTOR, selector)
                        if element and element.text:
                            driver_data['location'] = element.text.strip()
                            break
                    except:
                        continue
                
                # Reduced wait time for dynamic content
                time.sleep(1)
                
                # Get page text directly using Python (much faster)
                try:
                    page_text = driver.execute_script("return document.body.innerText;")
                    
                    # Extract speed using Python regex (handle both integer and decimal)
                    if driver_data['speed'] == 'N/A':
                        speed_match = re.search(r'(\d+\.?\d*)\s*mph', page_text, re.IGNORECASE)
                        if speed_match:
                            speed_val = float(speed_match.group(1))
                            driver_data['speed'] = f"{speed_val:.1f} mph"
                    
                    # Extract name (between 'Name' and 'Truck Number')
                    if driver_data['name'] == 'N/A':
                        name_match = re.search(r'Name\s*\n\s*\n\s*([A-Za-z\s]+?)\s*\n\s*\n\s*Truck Number', page_text, re.IGNORECASE)
                        if name_match:
                            driver_data['name'] = name_match.group(1).strip()
                    
                    # Extract location (after 'Current Location')
                    if driver_data['location'] == 'N/A':
                        location_match = re.search(r'Current Location\s*\n\s*\n\s*([^\n]+)', page_text, re.IGNORECASE)
                        if location_match:
                            driver_data['location'] = location_match.group(1).strip()
                    
                except Exception as e:
                    logger.error(f"Fast extraction failed: {e}")
                
                # Fallback: try simple text extraction if JavaScript failed
                if driver_data['name'] == 'N/A' or driver_data['speed'] == 'N/A' or driver_data['location'] == 'N/A':
                    try:
                        page_text = driver.execute_script("return document.body.innerText;")
                        
                        # Simple fallback patterns
                        if driver_data['name'] == 'N/A':
                            name_match = re.search(r'Name\s*\n\s*\n\s*([A-Z][a-z]+\s+[A-Z][a-z]+)', page_text, re.IGNORECASE)
                            if name_match:
                                driver_data['name'] = name_match.group(1).strip()
                        
                        if driver_data['speed'] == 'N/A':
                            speed_match = re.search(r'(\d+\.\d+)\s*mph', page_text, re.IGNORECASE)
                            if speed_match:
                                speed_val = float(speed_match.group(1))
                                driver_data['speed'] = f"{speed_val:.1f} mph"
                        
                        if driver_data['location'] == 'N/A':
                            location_match = re.search(r'Current Location\s*\n\s*\n\s*([^\n]+)', page_text, re.IGNORECASE)
                            if location_match:
                                driver_data['location'] = location_match.group(1).strip()
                    
                    except Exception as e:
                        logger.error(f"Fallback extraction failed: {e}")
                
                return driver_data
                
            finally:
                driver.quit()
                
        except Exception as e:
            logger.error(f"Error in extract_driver_data: {e}")
            return {
                'name': 'Error',
                'speed': 'Error',
                'location': 'Error extracting data'
            }
    
    def sanitize_address(self, address):
        """Clean and sanitize address for geocoding"""
        if not address or address.strip() == '':
            return ''
        
        # Remove trailing non-alphabetic characters and excessive whitespace
        address = address.strip()
        # Fix the regex pattern - remove double backslashes
        address = re.sub(r'[^a-zA-Z0-9,\s]+$', '', address)
        address = re.sub(r'\s+', ' ', address)
        
        # Remove common problematic phrases
        address = re.sub(r'\bOpen in Google Maps\b', '', address, flags=re.IGNORECASE)
        address = re.sub(r'\bLocation not available\b', '', address, flags=re.IGNORECASE)
        address = address.strip()
        
        logger.info(f"Sanitized address: '{address}'")
        return address
    
    def shorten_location(self, location):
        """Shorten location to extract county, state, and zip code in the format: County, State, ZIP"""
        if not location or location == 'N/A':
            return location
        
        try:
            import re
            
            # Patterns to extract county, state, and zip from full address
            # Example: "3292, Rennie Smith Drive, South Chicago Heights, Bloom Township, Cook County, Illinois, 60411, United States"
            # Expected output: "Cook County, Illinois, 60411"
            
            patterns = [
                # Pattern 1: Full address with county - "..., County Name, State, ZIP, Country"
                r'.*,\s*([^,]*County[^,]*),\s*([A-Z]{2}|[A-Za-z]+),\s*(\d{5}(?:-\d{4})?)(?:,\s*[^,]*)?$',
                # Pattern 2: Full address with county - "..., County Name, State ZIP, Country"
                r'.*,\s*([^,]*County[^,]*),\s*([A-Z]{2}|[A-Za-z]+)\s+(\d{5}(?:-\d{4})?)(?:,\s*[^,]*)?$',
                # Pattern 3: Standard format - "..., City, County, State, ZIP, Country"
                r'.*,\s*[^,]+,\s*([^,]+),\s*([A-Z]{2}|[A-Za-z]+),\s*(\d{5}(?:-\d{4})?)(?:,\s*[^,]*)?$',
                # Pattern 4: Alternative format - "..., City, State, ZIP"
                r'.*,\s*([^,]+),\s*([A-Z]{2}|[A-Za-z]+),\s*(\d{5}(?:-\d{4})?)(?:,\s*[^,]*)?$',
                # Pattern 5: Just city, state, zip - "City, State ZIP"
                r'([^,]+),\s*([A-Z]{2}|[A-Za-z]+)\s+(\d{5}(?:-\d{4})?)(?:,\s*[^,]*)?$'
            ]
            
            for pattern in patterns:
                match = re.search(pattern, location, re.IGNORECASE)
                if match:
                    location_part = match.group(1).strip()
                    state = match.group(2).strip()
                    zip_code = match.group(3).strip()
                    
                    # Clean up location part (remove extra spaces and unwanted characters)
                    location_clean = re.sub(r'\s+', ' ', location_part).strip()
                    
                    # Handle state abbreviations vs full state names
                    if len(state) == 2:
                        # It's already an abbreviation
                        state_abbrev = state.upper()
                    else:
                        # Convert full state name to abbreviation
                        state_map = {
                            'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR', 'California': 'CA',
                            'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE', 'Florida': 'FL', 'Georgia': 'GA',
                            'Hawaii': 'HI', 'Idaho': 'ID', 'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA',
                            'Kansas': 'KS', 'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
                            'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS',
                            'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV', 'New Hampshire': 'NH',
                            'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY', 'North Carolina': 'NC',
                            'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK', 'Oregon': 'OR', 'Pennsylvania': 'PA',
                            'Rhode Island': 'RI', 'South Carolina': 'SC', 'South Dakota': 'SD', 'Tennessee': 'TN',
                            'Texas': 'TX', 'Utah': 'UT', 'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA',
                            'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY'
                        }
                        state_abbrev = state_map.get(state.title(), state.upper())
                    
                    if location_clean and len(location_clean) > 0:
                        return f"{location_clean}, {state_abbrev}, {zip_code}"
            
            # If no pattern matches, try to extract just city and state
            # Fallback patterns for city, state format
            fallback_patterns = [
                # Pattern: "City, State"
                r'.*,\s*([^,]+),\s*([A-Z]{2}|[A-Za-z]+)(?:,\s*[^,]*)?$',
                # Pattern: "City, State ZIP"
                r'.*,\s*([^,]+),\s*([A-Z]{2}|[A-Za-z]+)\s+\d{5}(?:-\d{4})?(?:,\s*[^,]*)?$'
            ]
            
            for pattern in fallback_patterns:
                match = re.search(pattern, location, re.IGNORECASE)
                if match:
                    city = match.group(1).strip()
                    state = match.group(2).strip()
                    
                    # Clean up city name
                    city_clean = re.sub(r'\s+', ' ', city).strip()
                    
                    # Handle state abbreviations vs full state names
                    if len(state) == 2:
                        state_abbrev = state.upper()
                    else:
                        state_map = {
                            'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR', 'California': 'CA',
                            'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE', 'Florida': 'FL', 'Georgia': 'GA',
                            'Hawaii': 'HI', 'Idaho': 'ID', 'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA',
                            'Kansas': 'KS', 'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
                            'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS',
                            'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV', 'New Hampshire': 'NH',
                            'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY', 'North Carolina': 'NC',
                            'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK', 'Oregon': 'OR', 'Pennsylvania': 'PA',
                            'Rhode Island': 'RI', 'South Carolina': 'SC', 'South Dakota': 'SD', 'Tennessee': 'TN',
                            'Texas': 'TX', 'Utah': 'UT', 'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA',
                            'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY'
                        }
                        state_abbrev = state_map.get(state.title(), state.upper())
                    
                    if city_clean and len(city_clean) > 0:
                        return f"{city_clean}, {state_abbrev}"
            
            # Final fallback - return first 50 characters
            return location[:50] + "..." if len(location) > 50 else location
            
        except Exception as e:
            logger.error(f"Error shortening location: {e}")
            return location

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        welcome_message = """
🚛 **Driver Location Tracking Bot**

Welcome! This bot helps you track driver locations and calculate distances.

**Available Commands:**
• `/location` - Get current driver location data
• `/distance [address]` - Calculate distance to destination
• `/help` - Show detailed help

**Quick Start:**
📍 Send `/location` to get current driver status
📏 Type any address to calculate distance!

**Examples:**
• `123 Main Street, New York, NY`
• `Times Square, NYC`
• `LAX Airport`

**Note:** This bot is restricted to authorized users only.
        """
        
        await update.message.reply_text(welcome_message, parse_mode='Markdown')
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        user_id = update.effective_user.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        help_message = """
🚛 **Driver Location Tracking Bot Help**

**Commands:**
• `/location` - Fetch current driver location, speed, and name
• `/distance [address]` - Calculate distance to destination + enable auto-updates
• `/drivers` - List all available drivers
• `/setdriver [name]` - Assign a driver to this group
• `/groupinfo` - Show group configuration
• `/setdestination [address]` - Set destination for automatic updates
• `/stop` - Stop automatic updates
• `/start` - Welcome message
• `/help` - Show this help

**Setup:**
🔧 **First Time Setup:**
1. Use `/drivers` to see available drivers
2. Use `/setdriver [driver_name]` to assign a driver to this group
3. Use `/location` to test the setup

**Usage:**
📍 **Get Location:** Send `/location` to get current driver status
📏 **Calculate Distance:** 
  - `/distance 123 Main St, New York, NY`
  - Or just type any address directly!
  - **Auto-updates will start every 2 hours!**

🔄 **Auto-Updates:**
• Use `/distance` or `/setdestination` to enable auto-updates
• Bot will send location + distance updates every 2 hours
• Use `/stop` to stop auto-updates

**Examples:**
• `1600 Pennsylvania Ave, Washington DC`
• `Times Square, New York`
• `LAX Airport, Los Angeles`

**Data Format:**
🚛 Name: [Driver Name]
💨 Speed: [Speed] mph
📍 Location: [Current Location]
📏 Distance: [X.X] miles
        """
        
        await update.message.reply_text(help_message, parse_mode='Markdown')
    
    async def location_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /location command"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        # Send "fetching data" message
        status_message = await update.message.reply_text("🔄 Fetching driver location data...")
        
        try:
            # Determine the correct ELD URL for the group
            eld_url = self.get_eld_url_for_group(chat_id)
            
            # Check if no driver is assigned to this group
            if eld_url is None:
                await status_message.edit_text(
                    "❌ **No driver assigned to this group!**\n\n"
                    "Please assign a driver first:\n"
                    "1. Use `/drivers` to see available drivers\n"
                    "2. Use `/setdriver [driver_name]` to assign a driver\n\n"
                    "**Example:** `/setdriver Khan Bismillah`",
                    parse_mode='Markdown'
                )
                return
            
            # DEBUG: Log concurrent processing
            logger.info(f"🔄 [QUEUE] Location command enqueued for chat {chat_id}")
            
            # DEBUG: Log which driver is being used for this group
            driver = self.get_driver_by_chat_id(chat_id)
            if driver:
                logger.info(f"📍 [CONCURRENT] Location command for chat {chat_id} using driver: {driver['name']} (Unit: {driver['unit_number']})")
            
            logger.info(f"📍 Using ELD URL: {eld_url}")
            
            # Include chat_id in cache key to avoid collisions when multiple drivers share the same ELD URL
            cache_key = f"location_{chat_id}_{eld_url}"
            
            # Check cache first
            driver_data = self.get_cached_data(cache_key)
            
            if driver_data is None:
                # Run extraction in thread pool for better performance
                loop = asyncio.get_event_loop()
                driver_data = await loop.run_in_executor(
                    self.executor, 
                    self.extract_driver_data_ultra_fast, 
                    eld_url
                )
                # Cache the result
                self.set_cached_data(cache_key, driver_data)
            
            # Check if driver is offline
            is_offline = (driver_data['location'] == 'N/A' or 
                         'Location not available' in driver_data['location'] or 
                         'Error' in driver_data['location'])
            
            display_location = driver_data['location']
            offline_warning = ""
            
            if is_offline:
                offline_warning = "\n⚠️ **Status:** Driver offline - location not available"
            
            # Get driver status and track stop time
            driver_status, speed_value = self.get_driver_status(driver_data)
            stop_info = self.track_driver_stop_time(eld_url, driver_data)
            
            # Format response - shortened format
            truck_info = f" (Truck: {driver_data['truck_number']})" if driver_data.get('truck_number') and driver_data['truck_number'] != 'N/A' else ""
            
            # Determine status icon based on speed
            if is_offline:
                status_icon = "🔴"
                status_text = "Offline"
            else:
                status_icon = "🟢" if speed_value > 0 else "🔴"
                status_text = "Driving" if speed_value > 0 else "Stopped"
            
            # Shorten location to city/state if it's too long
            short_location = self.shorten_location(display_location) if not is_offline else display_location
            
            response = f"""📊 **Status:** {status_icon} {status_text}
📍 **Current Location:** {short_location}"""
            
            # Add stop duration if driver is stopped
            if stop_info and speed_value == 0:
                stop_duration = (datetime.now() - stop_info['stopped_since']).total_seconds()
                stop_minutes = int(stop_duration // 60)
                if stop_minutes > 0:
                    response += f"\n⏱️ **Stopped for:** {stop_minutes} minute(s)"
            
            # Update the status message with the result
            await status_message.edit_text(response, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error in location_command: {e}")
            await status_message.edit_text("❌ Error fetching driver data. Please try again later.")
    
    async def distance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /distance command"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        # Check if destination address is provided
        if not context.args:
            await update.message.reply_text("📍 Please provide a destination address.\n\n**Example:** `/distance 123 Main St, New York, NY`")
            return
        
        destination = ' '.join(context.args)
        logger.info(f"Distance command requested: {destination}")
        
        # Send processing message
        status_message = await update.message.reply_text("🔄 Calculating distance...")
        
        try:
            # Determine the correct ELD URL for the group
            eld_url = self.get_eld_url_for_group(chat_id)
            
            # Check if no driver is assigned to this group
            if eld_url is None:
                await status_message.edit_text(
                    "❌ **No driver assigned to this group!**\n\n"
                    "Please assign a driver first:\n"
                    "1. Use `/drivers` to see available drivers\n"
                    "2. Use `/setdriver [driver_name]` to assign a driver\n\n"
                    "**Example:** `/setdriver Khan Bismillah`",
                    parse_mode='Markdown'
                )
                return
            
            # Include chat_id in cache key to avoid collisions when multiple drivers share the same ELD URL
            cache_key = f"location_{chat_id}_{eld_url}"
            
            # Check cache first for driver location
            driver_data = self.get_cached_data(cache_key)
            
            if driver_data is None:
                # Run extraction in thread pool for better performance
                loop = asyncio.get_event_loop()
                driver_data = await loop.run_in_executor(
                    self.executor, 
                    self.extract_driver_data_ultra_fast, 
                    eld_url
                )
                # Cache the result
                self.set_cached_data(cache_key, driver_data)
            
            current_location = driver_data['location']
            logger.info(f"Driver current location: {current_location}")
            
            if current_location == 'N/A' or current_location == 'Location not available (driver may be offline)':
                await status_message.edit_text("❌ Driver is currently offline - location not available.")
                return
            
            # Get driver status and track stop time
            driver_status, speed_value = self.get_driver_status(driver_data)
            stop_info = self.track_driver_stop_time(eld_url, driver_data)
            
            # Calculate distance and time
            distance_data = self.calculate_distance_and_time(current_location, destination, chat_id, current_location)
            
            if distance_data is None:
                error_msg = "❌ Error calculating distance. Please check the addresses and try again."
                if not self.gmaps:
                    error_msg += "\n\n⚠️ Using OpenStreetMap geocoding service. Some addresses may not be found. Try a simpler address format."
                elif not self.gmaps_distance_matrix_available:
                    error_msg += "\n\n⚠️ Google Maps Distance Matrix API is disabled. Using OSRM API with OpenStreetMap geocoding."
                await status_message.edit_text(error_msg)
                return
            
            # Format response - shortened format
            truck_info = f" (Truck: {driver_data['truck_number']})" if driver_data.get('truck_number') and driver_data['truck_number'] != 'N/A' else ""
            
            # Determine status text based on speed
            status_text = "Driving" if speed_value > 0 else "Stopped"
            
            # Shorten location to city/state if it's too long
            short_location = self.shorten_location(current_location)
            
            response = f"""Status: {status_text}
Miles left: {distance_data['distance_text']}
ETA: {distance_data['duration_text']}"""
            
            # Add stop duration if driver is stopped
            if stop_info and speed_value == 0:
                stop_duration = (datetime.now() - stop_info['stopped_since']).total_seconds()
                stop_minutes = int(stop_duration // 60)
                if stop_minutes > 0:
                    response += f"\nStopped for: {stop_minutes} minute(s)"
            
            # Add warning if using fallback method
            if "straight-line" in distance_data['method'].lower():
                response += "\n\nNote: This is straight-line distance, not driving distance. Actual driving distance may be longer."
            
            # Store destination for automatic updates
            self.group_destinations[chat_id] = destination
            
            # Start individual auto-update timer for this group if not already running
            await self.start_group_auto_update(chat_id)
            
            await status_message.edit_text(response)
            
        except Exception as e:
            logger.error(f"Error in distance_command: {e}")
            await status_message.edit_text("❌ Error calculating distance. Please try again later.")
    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages as potential addresses for distance calculation"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not self.is_authorized(user_id):
            return
        
        text = update.message.text.strip()
        logger.info(f"Text message received: {text}")
        
        # Check if it looks like an address (contains numbers and letters)
        if re.search(r'\d+.*[a-zA-Z]|[a-zA-Z].*\d+', text) and len(text) > 10:
            # Send processing message
            status_message = await update.message.reply_text("🔄 Calculating distance to this address...")
            
            try:
                # Determine the correct ELD URL for the group
                eld_url = self.get_eld_url_for_group(chat_id)
                
                # Check if no driver is assigned to this group
                if eld_url is None:
                    await status_message.edit_text(
                        "❌ **No driver assigned to this group!**\n\n"
                        "Please assign a driver first:\n"
                        "1. Use `/drivers` to see available drivers\n"
                        "2. Use `/setdriver [driver_name]` to assign a driver\n\n"
                        "**Example:** `/setdriver Khan Bismillah`",
                        parse_mode='Markdown'
                    )
                    return
                
                # Get current driver location using the fast method
                loop = asyncio.get_event_loop()
                driver_data = await loop.run_in_executor(
                    self.executor, 
                    self.extract_driver_data_ultra_fast, 
                    eld_url
                )
                current_location = driver_data['location']
                
                logger.info(f"Driver current location for text message: {current_location}")
                
                if current_location == 'N/A' or current_location == 'Location not available (driver may be offline)':
                    await status_message.edit_text("❌ Driver is currently offline - location not available.")
                    return
                
                # Get driver status and track stop time
                driver_status, speed_value = self.get_driver_status(driver_data)
                stop_info = self.track_driver_stop_time(eld_url, driver_data)
                
                # Calculate distance and time
                distance_data = self.calculate_distance_and_time(current_location, text, chat_id, current_location)
                
                if distance_data is None:
                    error_msg = "❌ Could not find coordinates for the address. Please check the address format."
                    if not self.gmaps:
                        error_msg += "\n\n⚠️ Using OpenStreetMap geocoding service. Try a simpler address format (e.g., 'Main St, City, State')."
                    await status_message.edit_text(error_msg)
                    return
                
                # Format response - shortened format
                truck_info = f" (Truck: {driver_data['truck_number']})" if driver_data.get('truck_number') and driver_data['truck_number'] != 'N/A' else ""
                
                # Determine status text based on speed
                status_text = "Driving" if speed_value > 0 else "Stopped"
                
                # Shorten location to city/state if it's too long
                short_location = self.shorten_location(current_location)
                
                response = f"""Status: {status_text}
Miles left: {distance_data['distance_text']}
ETA: {distance_data['duration_text']}"""
                
                # Add stop duration if driver is stopped
                if stop_info and speed_value == 0:
                    stop_duration = (datetime.now() - stop_info['stopped_since']).total_seconds()
                    stop_minutes = int(stop_duration // 60)
                    if stop_minutes > 0:
                        response += f"\nStopped for: {stop_minutes} minute(s)"
                
                # Add warning if using fallback method
                if "straight-line" in distance_data['method'].lower():
                    response += "\n\nNote: This is straight-line distance, not driving distance. Actual driving distance may be longer."
                
                # Store destination for automatic updates
                self.group_destinations[chat_id] = text
                
                # Start individual auto-update timer for this group if not already running
                await self.start_group_auto_update(chat_id)
                
                await status_message.edit_text(response)
                
            except Exception as e:
                logger.error(f"Error in handle_text_message: {e}")
                await status_message.edit_text("❌ Error calculating distance. Please try again later.")
    
    async def drivers_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /drivers command - list all available drivers"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        # DEBUG: Log the drivers command request
        logger.info(f"🚛 Drivers command requested by chat {chat_id}")
        
        # DEBUG: Check if drivers_config exists and what's in it
        logger.info(f"🔍 DEBUG: drivers_config type: {type(self.drivers_config)}")
        logger.info(f"🔍 DEBUG: drivers_config content: {self.drivers_config}")
        
        # Try to reload configuration if it seems to be missing
        if not self.drivers_config or not self.drivers_config.get('drivers'):
            logger.warning("🔄 Drivers config seems empty, attempting to reload...")
            self.drivers_config = self.load_drivers_config()
            logger.info(f"🔄 Reloaded config: {len(self.drivers_config.get('drivers', []))} drivers")
        
        drivers = self.list_available_drivers()
        
        # DEBUG: Log the number of drivers loaded
        logger.info(f"🚛 Found {len(drivers)} drivers in configuration")
        
        if not drivers:
            await update.message.reply_text("❌ No drivers found in configuration.")
            return
        
        response = "🚛 **Available Drivers:**\n\n"
        
        for i, driver in enumerate(drivers, 1):
            status = "✅ Assigned" if driver['assigned_group'] else "⚪ Available"
            response += f"{i}. **{driver['name']}** (Unit: {driver['unit_number']}) - {status}\n"
        
        response += "\n💡 **Tip:** Use `/setdriver [driver_name]` to assign a driver to this group"
        
        await update.message.reply_text(response, parse_mode='Markdown')
    
    async def setdriver_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /setdriver command - assign a driver to this group"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        if not context.args:
            await update.message.reply_text("📝 Please provide a driver name.\n\n**Example:** `/setdriver Addam Hayder`\n\n**Tip:** Use `/drivers` to see available drivers")
            return
        
        driver_name = ' '.join(context.args)
        logger.info(f"Setting driver '{driver_name}' for group {chat_id}")
        
        success, message = self.set_driver_for_group(chat_id, driver_name)
        
        if success:
            await update.message.reply_text(f"✅ {message}")
        else:
            await update.message.reply_text(f"❌ {message}")
    
    async def groupinfo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /groupinfo command - show current group configuration"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        driver = self.get_driver_by_chat_id(chat_id)
        
        response = f"📊 **Group Information**\n\n"
        response += f"**Chat ID:** `{chat_id}`\n"
        
        if driver:
            response += f"**Assigned Driver:** {driver['name']}\n"
            response += f"**Unit Number:** {driver['unit_number']}\n"
            response += f"**Status:** ✅ Configured"
        else:
            response += f"**Assigned Driver:** None\n"
            response += f"**Status:** ⚪ Not configured\n\n"
            response += f"💡 **Tip:** Use `/setdriver [driver_name]` to assign a driver to this group"
        
        await update.message.reply_text(response, parse_mode='Markdown')
    
    async def unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle unknown commands"""
        user_id = update.effective_user.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        await update.message.reply_text("❓ Unknown command. Use /help to see available commands.")
    
    async def set_destination_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /setdestination command - set destination for automatic updates"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        if not context.args:
            await update.message.reply_text("📍 Please provide a destination address.\n\n**Example:** `/setdestination 123 Main St, New York, NY`")
            return
        
        destination = ' '.join(context.args)
        self.group_destinations[chat_id] = destination
        
        # Start individual auto-update timer for this group if not already running
        await self.start_group_auto_update(chat_id)
        
        await update.message.reply_text(f"✅ **Destination set for automatic updates:**\n\n📍 {destination}\n\n🔄 The bot will now send location updates every {self.auto_update_interval//3600} hour(s) with distance remaining to this destination.")
    
    async def clear_destination_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop command - clear destination for automatic updates"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not self.is_authorized(user_id):
            await update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        
        if chat_id in self.group_destinations:
            del self.group_destinations[chat_id]
            
            # Cancel the individual auto-update task for this group
            await self.stop_group_auto_update(chat_id)
            
            await update.message.reply_text("✅ **Automatic updates disabled.**\n\nDestination cleared. The bot will no longer send automatic location updates.")
        else:
            await update.message.reply_text("⚠️ No destination is currently set for this group.")
    
    
    async def process_group_update(self, chat_id, destination):
        """Process update for a single group - designed to run concurrently"""
        try:
            logger.info(f"Processing auto-update for group {chat_id} to destination {destination}")
            
            # Get driver for this group
            driver = self.get_driver_by_chat_id(chat_id)
            if not driver:
                logger.warning(f"No driver assigned to group {chat_id}, skipping auto-update")
                return
            
            # Get ELD URL for this group
            eld_url = self.get_eld_url_for_group(chat_id)
            
            # Extract current driver data
            driver_data = await asyncio.get_event_loop().run_in_executor(
                self.executor, 
                self.extract_driver_data_ultra_fast, 
                eld_url
            )
            
            current_location = driver_data['location']
            logger.info(f"Auto-update: Driver current location: {current_location}")
            
            if current_location == 'N/A' or current_location == 'Location not available (driver may be offline)':
                logger.warning(f"Driver offline for group {chat_id}, skipping auto-update")
                return
            
            # Get driver status and track stop time
            driver_status, speed_value = self.get_driver_status(driver_data)
            stop_info = self.track_driver_stop_time(eld_url, driver_data)
            
            # Check for extended stop and send alert if needed
            extended_stop, stop_minutes = self.check_extended_stop(eld_url)
            if extended_stop:
                alert_message = f"""🚨 **EXTENDED STOP ALERT**
                
🚛 **Driver:** {driver_data['name']}
🛑 **Status:** Driver has been stopped for {stop_minutes} minutes
📍 **Location:** {current_location}
⚠️ **Alert:** Driver stopped for more than 45 minutes"""
                
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=alert_message,
                    parse_mode='Markdown'
                )
                logger.info(f"Sent extended stop alert to group {chat_id}")
            
            # Calculate distance and time to destination
            distance_data = self.calculate_distance_and_time(current_location, destination, chat_id, current_location)
            
            if distance_data is None:
                logger.error(f"Failed to calculate distance for group {chat_id}")
                return
            
            # Format the automatic update message - shortened format
            truck_info = f" (Truck: {driver_data['truck_number']})" if driver_data.get('truck_number') and driver_data['truck_number'] != 'N/A' else ""
            
            # Determine status text based on speed
            status_text = "Driving" if speed_value > 0 else "Stopped"
            
            # Shorten location to city/state if it's too long
            short_location = self.shorten_location(current_location)
            
            update_message = f"""Status: {status_text}
Miles left: {distance_data['distance_text']}
ETA: {distance_data['duration_text']}"""
            
            # Add stop duration if driver is stopped
            if stop_info and speed_value == 0:
                stop_duration = (datetime.now() - stop_info['stopped_since']).total_seconds()
                stop_minutes = int(stop_duration // 60)
                if stop_minutes > 0:
                    update_message += f"\nStopped for: {stop_minutes} minute(s)"
            
            # Add warning if using fallback method
            if "straight-line" in distance_data['method'].lower():
                update_message += "\n\nNote: This is straight-line distance, not driving distance. Actual driving distance may be longer."
            
            # Send the update message to the group
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=update_message
            )
            
            logger.info(f"Sent auto-update to group {chat_id}")
            
        except Exception as e:
            logger.error(f"Error in auto-update for group {chat_id}: {e}")
    
    async def start_group_auto_update(self, chat_id):
        """Start individual auto-update timer for a specific group"""
        # Cancel existing task if running
        await self.stop_group_auto_update(chat_id)
        
        # Create new task for this group
        task = asyncio.create_task(self.group_auto_update_loop(chat_id))
        self.group_update_tasks[chat_id] = task
        
        logger.info(f"Started individual auto-update timer for group {chat_id}")
    
    async def stop_group_auto_update(self, chat_id):
        """Stop individual auto-update timer for a specific group"""
        if chat_id in self.group_update_tasks:
            task = self.group_update_tasks[chat_id]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            del self.group_update_tasks[chat_id]
            logger.info(f"Stopped individual auto-update timer for group {chat_id}")
    
    async def group_auto_update_loop(self, chat_id):
        """Individual auto-update loop for a specific group"""
        logger.info(f"Starting individual auto-update loop for group {chat_id}")
        
        while True:
            try:
                # Wait for the specified interval (2 hours)
                await asyncio.sleep(self.auto_update_interval)
                
                # Check if destination is still set for this group
                if chat_id not in self.group_destinations:
                    logger.info(f"No destination set for group {chat_id}, stopping auto-update")
                    break
                
                destination = self.group_destinations[chat_id]
                logger.info(f"Running individual auto-update for group {chat_id} to destination {destination}")
                
                # Process update for this specific group
                await self.process_group_update(chat_id, destination)
                
                logger.info(f"Completed individual auto-update for group {chat_id}")
                
            except asyncio.CancelledError:
                logger.info(f"Auto-update loop cancelled for group {chat_id}")
                break
            except Exception as e:
                logger.error(f"Error in individual auto-update loop for group {chat_id}: {e}")
                # Continue the loop even if there's an error
    
    def run(self):
        """Start the bot"""
        try:
            # Create application
            application = Application.builder().token(self.bot_token).build()
            
            # Store application reference for auto-updates
            self.application = application
            
            # Add handlers
            application.add_handler(CommandHandler("start", self.start_command))
            application.add_handler(CommandHandler("help", self.help_command))
            application.add_handler(CommandHandler("location", self.location_command))
            application.add_handler(CommandHandler("distance", self.distance_command))
            application.add_handler(CommandHandler("drivers", self.drivers_command))
            application.add_handler(CommandHandler("setdriver", self.setdriver_command))
            application.add_handler(CommandHandler("groupinfo", self.groupinfo_command))
            application.add_handler(CommandHandler("setdestination", self.set_destination_command))
            application.add_handler(CommandHandler("stop", self.clear_destination_command))
            
            # Add text message handler for address detection
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))
            
            # Add error handler
            async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
                logger.error(f"Exception while handling an update: {context.error}")
            
            application.add_error_handler(error_handler)
            
            # No global auto-update task needed - individual group timers are started when needed
            async def post_init(application):
                """Initialize application after start"""
                logger.info("Bot initialized - individual group auto-update timers will start when distance commands are used")
            
            application.post_init = post_init
            
            # Start the bot
            logger.info("Starting bot...")
            application.run_polling(allowed_updates=Update.ALL_TYPES)
            
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            raise

def main():
    """Main function"""
    try:
        bot = LocationBot()
        bot.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
