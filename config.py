"""
config.py — Stock Screener Configuration
Single source of truth for credentials, paths, and filter defaults.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# ====================================================================
# PATHS
# ====================================================================
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
VENV_PYTHON = PROJECT_DIR / "venv" / "bin" / "python3"
CHINA_CACHE_FILE = PROJECT_DIR / ".china_filter_cache.json"

# ====================================================================
# CREDENTIALS
# ====================================================================
load_dotenv(PROJECT_DIR / ".env")
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

# ====================================================================
# SCREENER DEFAULTS
# ====================================================================
SCREENER_TOP = 20                # Number of top movers to pull
SCREENER_MIN_CHANGE = 20.0       # Min % change to include
SCREENER_MAX_PRICE = 22.0        # Max price filter ($)
SCREENER_MIN_PRICE = 1.0         # Min price filter ($)
NEWS_LOOKBACK_HOURS = 48         # How far back to check for news catalysts

# ====================================================================
# SEC EDGAR (China stock filter)
# ====================================================================
SEC_USER_AGENT = "StockScreener admin@localhost"
# Country codes: F4=China, G6=Hong Kong, E9=Cayman Islands, K6=British Virgin Islands
CHINA_COUNTRY_CODES = {"F4", "G6", "E9", "K6"}

# ====================================================================
# AUTOPILOT SCHEDULE (24/7 autonomous operation)
# ====================================================================
AUTO_ENABLED = True
AUTO_RESET_MINUTE = 480          # 8:00 AM ET — clear previous session
AUTO_SCAN_MINUTE = 560           # 9:20 AM ET — first scan
AUTO_RESCAN_INTERVAL = 5         # Minutes between rescans (pre-market)
MARKET_OPEN_MINUTE = 570         # 9:30 AM ET — stop rescanning
MARKET_CLOSE_MINUTE = 960        # 4:00 PM ET — end of day

# ====================================================================
# VERSION
# ====================================================================
VERSION = "v1.0.0"
