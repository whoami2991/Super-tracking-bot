# 🚛 Telegram Driver Location Tracking Bot

A comprehensive Python-based Telegram bot that tracks multiple drivers' locations from State ELD tracking links, calculates distances, and provides automatic location updates.

## ✨ Features

- 🚛 **Multi-Driver Support** - Track multiple drivers with individual assignments
- 💨 **Real-Time Speed Tracking** - Monitor vehicle speed in mph
- 📍 **Live Location Updates** - Get current driver positions
- 📏 **Distance Calculations** - Calculate distances to destinations with multiple APIs
- 🔄 **Automatic Updates** - Auto-location updates every 2 hours
- 🎯 **Destination Management** - Set and manage destinations for groups
- 🔐 **Group-Based Access** - Driver assignments per Telegram group
- 🌍 **Multiple Distance APIs** - Google Maps, OSRM, and Haversine fallbacks

## 🚀 Available Commands

### Core Commands
- `/start` - Welcome message and bot introduction
- `/help` - Show detailed help and commands
- `/location` - Get current driver location, speed, and status
- `/distance [address]` - Calculate distance to destination + enable auto-updates

### Driver Management
- `/drivers` - List all available drivers
- `/setdriver [name]` - Assign a driver to current group
- `/groupinfo` - Show current group configuration


### Destination Management
- `/setdestination [address]` - Set destination for automatic updates
- `/stop` - Stop automatic updates

## 📋 Prerequisites

- Python 3.8 or higher
- Chrome browser (for Selenium WebDriver)
- Telegram Bot Token (from @BotFather)
- Internet connection

## 🛠️ Installation & Setup

### 1. Download Project
```bash
# Download and extract to your server
cd /path/to/your/project
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment
Edit the `.env` file:
```env
# Telegram Bot Configuration
BOT_TOKEN=YOUR_BOT_TOKEN_HERE

# Authorized User IDs (comma-separated)
AUTHORIZED_USERS=123456789,987654321

# ELD Tracking URL (fallback)
ELD_URL=https://state-eld.us/shared-driver-link/your-default-url

# Google Maps API Key (optional, for better distance calculations)
GOOGLE_MAPS_API_KEY=your_google_maps_api_key_here
```

### 4. Configure Drivers
Edit `drivers_config.json` to add your drivers:
```json
{
  "drivers": [
    {
      "name": "Driver Name",
      "unit_number": "123",
      "eld_url": "https://state-eld.us/shared-driver-link/driver-uuid",
      "telegram_group_id": null
    }
  ]
}
```

### 5. Start the Bot
```bash
python main.py
```

**Or use the convenience scripts:**
- Windows: `run.bat`
- PowerShell: `run.ps1`

## 📱 Usage Guide

### Initial Setup
1. **Start the bot** on your server
2. **Add bot to Telegram group** or chat directly
3. **Send `/start`** to see welcome message
4. **Use `/drivers`** to see available drivers
5. **Use `/setdriver [name]`** to assign a driver to the group

### Basic Usage
```
/location                    # Get current driver status
/distance 123 Main St, NY    # Calculate distance + enable auto-updates
123 Main Street, New York    # Direct address input (same as distance)
/setdestination LAX Airport  # Set destination for auto-updates
/stop                       # Stop auto-updates
```

### Example Output
```
🚛 **Driver:** Mohammad Zahir (Truck: 786)
💨 **Speed:** 65.2 mph
📊 **Status:** Moving
📍 **Current Location:** I-95 North, Philadelphia, PA

🎯 **Destination:** 123 Main Street, New York, NY
📏 **Distance Remaining:** 87.3 mi
⏱️ **Time Remaining:** 1.4 hr
```

## 🔧 Configuration Files

### `.env` - Environment Variables
```env
BOT_TOKEN=your_telegram_bot_token
AUTHORIZED_USERS=user_id1,user_id2,user_id3
ELD_URL=fallback_eld_url
GOOGLE_MAPS_API_KEY=optional_google_maps_key
```

### `drivers_config.json` - Driver Configuration
```json
{
  "drivers": [
    {
      "name": "Driver Name",
      "unit_number": "Unit123",
      "eld_url": "https://state-eld.us/shared-driver-link/uuid",
      "telegram_group_id": -1234567890
    }
  ]
}
```


## 🏗️ Project Structure

```
📁 Project/
├── 📄 main.py                    # Main bot application
├── 📄 drivers_config.json        # Driver configuration
├── 📄 .env                       # Environment variables
├── 📄 requirements.txt           # Python dependencies
├── 📄 run.bat                    # Windows run script
├── 📄 run.ps1                    # PowerShell run script
└── 📄 README.md                  # This documentation
```

## 🔄 Auto-Update Feature

The bot automatically sends location updates every 2 hours when:
1. A destination is set using `/distance` or `/setdestination`
2. A driver is assigned to the group
3. The bot is running

**Auto-update includes:**
- Current driver location and speed
- Distance to destination
- Estimated time remaining
- Stop duration if driver is stopped

## 🌐 Distance Calculation APIs

1. **Google Maps API** (Primary) - Most accurate driving routes
2. **OSRM API** (Fallback) - Free routing service
3. **Haversine Formula** (Emergency) - Straight-line distance


## 🔐 Security Features

- **User Authorization**: Only authorized users can use the bot
- **Group-Based Access**: Each group can only see their assigned driver
- **Secure Token Storage**: Bot token stored in `.env` file
- **No Public Access**: Bot requires explicit user authorization

## 🚨 Troubleshooting

### Common Issues

1. **"No driver assigned to this group"**
   - Use `/drivers` to see available drivers
   - Use `/setdriver [name]` to assign a driver

2. **"Error fetching data"**
   - Check internet connection
   - Verify ELD URL is accessible
   - Check Chrome browser installation

3. **Distance calculation fails**
   - Google Maps API key may be invalid
   - Bot will fallback to OSRM API automatically

4. **Bot doesn't respond**
   - Verify bot token is correct
   - Check if user ID is in AUTHORIZED_USERS
   - Ensure bot is running without errors

### Log Messages
The bot provides detailed logging:
```
2025-07-14 17:18:26,672 - __main__ - INFO - ✅ Loaded driver mapping: Chat -4921537392 -> Abdolah Janami (Unit: 01)
2025-07-14 17:18:26,673 - __main__ - INFO - 📊 Total driver mappings loaded: 5
```

## 🖥️ Server Deployment

### Linux/Ubuntu Server
```bash
# Install Python and pip
sudo apt update
sudo apt install python3 python3-pip

# Install Chrome for Selenium
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | sudo apt-key add -
sudo sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list'
sudo apt update
sudo apt install google-chrome-stable

# Install project dependencies
pip3 install -r requirements.txt

# Run the bot
python3 main.py
```

### Windows Server
```cmd
# Install Python from python.org
# Install Chrome browser
# Install dependencies
pip install -r requirements.txt

# Run the bot
python main.py
```

### Running as Service (Linux)
```bash
# Create systemd service file
sudo nano /etc/systemd/system/location-bot.service

[Unit]
Description=Location Tracking Bot
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/your/bot
ExecStart=/usr/bin/python3 main.py
Restart=always

[Install]
WantedBy=multi-user.target

# Enable and start service
sudo systemctl enable location-bot.service
sudo systemctl start location-bot.service
```

## 📋 Dependencies

```txt
python-telegram-bot==21.9    # Telegram bot framework
selenium==4.20.0            # Web automation
webdriver-manager==4.0.1    # Chrome driver management
python-dotenv==1.0.0        # Environment variables
requests==2.31.0            # HTTP requests
googlemaps==4.10.0          # Google Maps API
```

## 🔧 Technical Details

- **Framework**: python-telegram-bot (async)
- **Web Scraping**: Selenium WebDriver with Chrome
- **Data Extraction**: Regex patterns + DOM selectors
- **Distance APIs**: Google Maps, OSRM, Haversine
- **Caching**: 15-second cache for location data
- **Concurrency**: Multi-threaded processing
- **Auto-Updates**: Asyncio-based automatic updates

## 🤝 Support

If you encounter issues:
1. Check console output for error messages
2. Verify all configuration files are correct
3. Ensure Chrome browser is installed
4. Check internet connectivity
5. Verify bot token and user IDs

---

**🔒 Security Note**: Keep your bot token secure and never share it publicly. Only authorized users can access the bot functionality.
