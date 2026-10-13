"""
bot.py  –  entry point for the TXT Leech Bot.

Run locally:
    python bot.py

Deploy on Render:
    Start Command:  python bot.py
    (set WEBHOOK=False in environment – polling is used by default)
"""

import asyncio
import logging
import os
import sys

from pyrogram import Client, filters, idle
from pyrogram.types import BotCommand, Message

from config import Config
from modules.handler import cmd_stop, on_text_reply, on_txt_file

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
    stream = sys.stdout,
)
logger = logging.getLogger("bot")

# ── Validate config early ────────────────────────────────────────────────────
try:
    Config.validate()
except EnvironmentError as e:
    logger.critical("Configuration error: %s", e)
    sys.exit(1)

# ── Create the Pyrogram client ───────────────────────────────────────────────
app = Client(
    name      = Config.SESSION_NAME,
    api_id    = Config.API_ID,
    api_hash  = Config.API_HASH,
    bot_token = Config.BOT_TOKEN,
    workers   = 4,
)

# ── Command handlers ─────────────────────────────────────────────────────────

@app.on_message(filters.command("start") & (filters.private | filters.group))
async def start_cmd(_client: Client, message: Message):
    await message.reply_text(
        "👋 **Welcome to TXT Leech Bot!**\n\n"
        "📤 Send me a `.txt` file containing video links.\n\n"
        "**Line format** (any of these works):\n"
        "  `Video Title : https://...`\n"
        "  `001) Video Title : https://...`\n"
        "  `[Section] Video Title : https://...`\n\n"
        "**Commands:**\n"
        "/start — Show this message\n"
        "/stop  — Cancel the current upload session\n"
        "/help  — Detailed help\n"
    )


@app.on_message(filters.command("help") & (filters.private | filters.group))
async def help_cmd(_client: Client, message: Message):
    await message.reply_text(
        "📘 **How to use:**\n\n"
        "1. Prepare a `.txt` file where each video line looks like:\n"
        "   `Video Name : https://...`\n\n"
        "2. Send the file to this bot.\n\n"
        "3. The bot shows a numbered list and asks for a **range**.\n"
        "   Reply with e.g.  `all`,  `1-10`,  `5`,  `1-3,7,10-15`\n\n"
        "4. The bot downloads each video and uploads it here as a\n"
        "   streamable Telegram video with a thumbnail.\n\n"
        "5. Send /stop at any time to abort.\n\n"
        "**Supported URL types**\n"
        "• Direct MP4 / video links\n"
        "• HLS (.m3u8) streams\n"
        "• Signed CDN URLs (ClassX / AppX / etc.)\n"
        "• Most sites yt-dlp supports\n"
    )


@app.on_message(filters.command("stop") & (filters.private | filters.group))
async def stop_cmd(client: Client, message: Message):
    await cmd_stop(client, message)


# ── Document handler (txt file) ───────────────────────────────────────────────

@app.on_message(filters.document & (filters.private | filters.group))
async def doc_handler(client: Client, message: Message):
    fname = (message.document.file_name or "").lower()
    if fname.endswith(".txt"):
        await on_txt_file(client, message)
    else:
        await message.reply_text(
            "❌ Please send a `.txt` file.\n\n"
            "The file should have one video per line:\n"
            "`Video Name : https://...`"
        )


# ── Text handler (range replies) ──────────────────────────────────────────────

@app.on_message(
    filters.text & ~filters.command(["start", "help", "stop"]) &
    (filters.private | filters.group)
)
async def text_handler(client: Client, message: Message):
    await on_text_reply(client, message)


# ── Keep-alive ping for Render free tier ─────────────────────────────────────

async def _keep_alive():
    """
    Render's free tier spins down after 15 min of inactivity.
    This background task logs a heartbeat every 10 minutes so the
    process stays alive (no external pinger needed).
    """
    while True:
        await asyncio.sleep(600)
        logger.info("♥ heartbeat")


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    await app.start()

    # Register bot commands in BotFather
    await app.set_bot_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("stop",  "Stop current upload session"),
        BotCommand("help",  "How to use the bot"),
    ])

    me = await app.get_me()
    logger.info("Bot started: @%s  (id=%d)", me.username, me.id)

    # Start background keep-alive
    asyncio.create_task(_keep_alive())

    await idle()
    await app.stop()
    logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
