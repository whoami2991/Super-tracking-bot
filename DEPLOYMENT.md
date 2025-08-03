# 🚀 Railway Deployment Guide

## Quick Deploy to Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template)

## Environment Variables Required

Set these variables in Railway:

| Variable | Description | Required |
|----------|-------------|----------|
| `BOT_TOKEN` | Your Telegram bot token from @BotFather | ✅ Yes |
| `GOOGLE_MAPS_API_KEY` | Google Maps API key for distance calculations | ❌ Optional |
| `AUTHORIZED_USERS` | Comma-separated user IDs (not used, bot is open) | ❌ Optional |
| `ELD_URL` | Fallback ELD URL (not needed if drivers_config.json is set) | ❌ Optional |

## How to Deploy

1. **Fork this repository** or **Import to Railway**
2. **Connect to Railway:**
   - Go to [railway.app](https://railway.app)
   - Sign up with GitHub
   - Click "New Project"
   - Select "Deploy from GitHub repo"
   - Choose this repository

3. **Set Environment Variables:**
   - In Railway dashboard, go to your project
   - Click "Variables" tab
   - Add `BOT_TOKEN` with your actual bot token
   - Add `GOOGLE_MAPS_API_KEY` if you have one

4. **Deploy:**
   - Railway will automatically build and deploy
   - Your bot will be running 24/7

## Features

- 🚛 Track multiple drivers with individual assignments
- 💨 Real-time speed tracking
- 📍 Live location updates
- 📏 Distance calculations with multiple APIs
- 🔄 Automatic updates every 2 hours
- 🎯 Destination management
- 🔐 Group-based access

## Commands

- `/start` - Welcome message
- `/help` - Show help
- `/location` - Get driver location
- `/distance [address]` - Calculate distance + auto-updates
- `/drivers` - List all drivers
- `/setdriver [name]` - Assign driver to group
- `/setdestination [address]` - Set destination
- `/stop` - Stop auto-updates

## Support

For issues or questions, check the main README.md file.
