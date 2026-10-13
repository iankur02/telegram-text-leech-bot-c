import os

API_ID    = int(os.environ.get("API_ID", "0"))
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Render / web-server settings
PORT      = int(os.environ.get("PORT", 8080))

# Optional: restrict bot to these chat IDs (comma-separated). Leave blank = allow all.
ALLOWED_USERS = os.environ.get("ALLOWED_USERS", "")   # e.g. "123456,789012"
