"""
Configuration settings and environment variable loaders.
Centralized settings management for database, WhatsApp Cloud API, and platform security.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the repository
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file from project root if it exists
ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()

# Database Configuration
# Default to SQLite database file in the project root
DEFAULT_DB_FILE = BASE_DIR / "whatsapp_campaign.db"
DEFAULT_DB_URL = f"sqlite:///{DEFAULT_DB_FILE.as_posix()}"

DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DB_URL)

# Application Environment
APP_ENV = os.getenv("APP_ENV", "development")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "campaign-hub-secret-key-change-in-prod")

# WhatsApp Cloud API Credentials & Configuration
WHATSAPP_API_TOKEN = os.getenv("WHATSAPP_API_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "")

# WhatsApp Cloud API Operational Settings
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0")
WHATSAPP_API_BASE_URL = os.getenv("WHATSAPP_API_BASE_URL", "https://graph.facebook.com")
WHATSAPP_MODE = os.getenv("WHATSAPP_MODE", "test").lower().strip()  # "test" (default) or "production"
WHATSAPP_API_TIMEOUT = int(os.getenv("WHATSAPP_API_TIMEOUT", "15"))
WHATSAPP_MAX_RETRIES = int(os.getenv("WHATSAPP_MAX_RETRIES", "3"))
WHATSAPP_RATE_LIMIT_RPS = float(os.getenv("WHATSAPP_RATE_LIMIT_RPS", "20.0"))
