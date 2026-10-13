import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Required ────────────────────────────────────────────────────────────
    API_ID: int   = int(os.environ.get("API_ID", 0))
    API_HASH: str = os.environ.get("API_HASH", "")
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")

    # ── Optional ────────────────────────────────────────────────────────────
    # Channel/group id to forward every uploaded video (0 = disabled)
    DUMP_CHANNEL: int = int(os.environ.get("DUMP_CHANNEL", 0))

    # Max file size the bot will try to upload (MB).
    # Telegram Bot API limit is 2 000 MB; Pyrogram via MTProto supports 4 000 MB.
    MAX_FILE_SIZE_MB: int = int(os.environ.get("MAX_FILE_SIZE_MB", 2000))

    # Seconds to wait for one video to finish downloading before aborting it
    DOWNLOAD_TIMEOUT: int = int(os.environ.get("DOWNLOAD_TIMEOUT", 600))

    # Seconds to wait for a user's range reply after showing the file list
    REPLY_TIMEOUT: int = int(os.environ.get("REPLY_TIMEOUT", 120))

    # Number of download retries per video
    DOWNLOAD_RETRIES: int = int(os.environ.get("DOWNLOAD_RETRIES", 3))

    # Pyrogram session name (just a label – no file is created when using a bot token)
    SESSION_NAME: str = os.environ.get("SESSION_NAME", "txt_leech_bot")

    # Render / webhook settings (set WEBHOOK=True on Render)
    PORT: int  = int(os.environ.get("PORT", 8080))
    WEBHOOK: bool = os.environ.get("WEBHOOK", "False").strip().lower() == "true"

    # Comma-separated Telegram user IDs that are allowed to use the bot.
    # Leave empty to allow everyone.
    ALLOWED_USERS: list = [
        int(uid.strip())
        for uid in os.environ.get("ALLOWED_USERS", "").split(",")
        if uid.strip().isdigit()
    ]

    @classmethod
    def validate(cls):
        missing = []
        if not cls.API_ID:
            missing.append("API_ID")
        if not cls.API_HASH:
            missing.append("API_HASH")
        if not cls.BOT_TOKEN:
            missing.append("BOT_TOKEN")
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
