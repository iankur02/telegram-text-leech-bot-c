"""
main.py  –  Telegram Bot: TXT → Video Leech Bot
Fully rewritten from reference repos with every edge-case fixed.
Deployable on Render (free tier) via webhook keep-alive.
"""

import os
import re
import sys
import time
import asyncio
import logging

from subprocess import getstatusoutput
from aiohttp import web
from pyromod import listen
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait

import helper
from utils import progress_bar
from vars import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USERS

# ─────────────────────── logging ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ─────────────────────── bot client ──────────────────────────────────────────
bot = Client(
    "txt_leech_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ─────────────────────── per-user stop flags ─────────────────────────────────
_stop_flags: dict[int, bool] = {}   # chat_id → True means "user requested stop"


def _is_stopped(chat_id: int) -> bool:
    return _stop_flags.get(chat_id, False)


def _set_stop(chat_id: int, val: bool):
    _stop_flags[chat_id] = val


# ─────────────────────── access control ──────────────────────────────────────
def _allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    allowed = [x.strip() for x in ALLOWED_USERS.split(",") if x.strip()]
    return str(user_id) in allowed


# ─────────────────────── text-file parser ────────────────────────────────────
def parse_links(content: str) -> list[tuple[str, str]]:
    """
    Parse the txt file format used by ClassX / pw platform exports.

    Supported line formats:
      Title : https://...
      [Section] Title : https://...
      Title :https://...         (no space before colon)
      Plain section header with no URL  → skipped

    Returns list of (title, url) tuples.
    """
    links = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Find last " : http" or " :http" to split name from URL
        # (using 'last' avoids false splits if the title itself contains ": ")
        match = re.search(r'^(.*?)\s*:\s*(https?://.+)$', line)
        if not match:
            continue   # section header or blank — skip
        title = match.group(1).strip()
        url   = match.group(2).strip()
        if not title:
            title = "Video"
        links.append((title, url))
    return links


# ─────────────────────── /start ──────────────────────────────────────────────
@bot.on_message(filters.command("start"))
async def cmd_start(bot: Client, m: Message):
    await m.reply_text(
        f"👋 **Hello {m.from_user.mention}!**\n\n"
        "I convert course **TXT files** to Telegram videos.\n\n"
        "**Commands:**\n"
        "• /upload – Start a new batch\n"
        "• /stop   – Cancel the current batch\n"
        "• /help   – Show this message\n\n"
        "Send /upload and follow the steps.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📖 Help", callback_data="help")
        ]])
    )
    await message.reply_text(text)

# ─────────────────────── /stop ───────────────────────────────────────────────
@bot.on_message(filters.command("stop"))
async def cmd_stop(_, m: Message):
    _set_stop(m.chat.id, True)
    await m.reply_text("🛑 **Stopping after the current video…**")


# ─────────────────────── /help ───────────────────────────────────────────────
@bot.on_message(filters.command("help"))
async def cmd_help(_, m: Message):
    await m.reply_text(
        "**📖 How to use:**\n\n"
        "1. Send /upload\n"
        "2. Upload your .txt file (ClassX / PW format)\n"
        "3. Enter the starting video number (1 = from beginning)\n"
        "4. Enter a batch name (shown in caption)\n"
        "5. Choose quality: `144 / 240 / 360 / 480 / 720 / 1080`\n"
        "6. Enter a caption (or send `default`)\n"
        "7. Send a thumbnail URL or send `no`\n\n"
        "**Notes:**\n"
        "• Tokens in txt files expire after a few hours.\n"
        "  Re-generate your txt file for fresh tokens.\n"
        "• Videos encrypted with DRM cannot be downloaded.\n"
        "• Use /stop to cancel at any time.\n"
    )


# ─────────────────────── /upload ─────────────────────────────────────────────
@bot.on_message(filters.command("upload"))
async def cmd_upload(bot: Client, m: Message):
    chat_id = m.chat.id

    if not _allowed(m.from_user.id):
        await m.reply_text("⛔ You are not authorised to use this bot.")
        return

    # Reset stop flag for this session
    _set_stop(chat_id, False)

    # ── Step 1: get txt file ──────────────────────────────────────────────────
    prompt = await m.reply_text("📤 **Send your .txt file**")
    try:
        file_msg: Message = await bot.listen(chat_id, timeout=120)
    except asyncio.TimeoutError:
        await prompt.edit("⌛ Timed out waiting for file. Send /upload to retry.")
        return

    if not file_msg.document:
        await file_msg.reply_text("❌ That's not a document. Send /upload and try again.")
        await prompt.delete()
        return

    await file_msg.delete()
    local_path = await file_msg.download()

    try:
        with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        os.remove(local_path)
    except Exception as e:
        await prompt.edit(f"❌ **Could not read file:** `{e}`")
        return

    links = parse_links(content)
    total = len(links)

    if total == 0:
        await prompt.edit(
            "❌ **No valid links found in the file.**\n\n"
            "Expected format per line:\n"
            "`Title : https://...`"
        )
        return

    # ── Step 2: start index ───────────────────────────────────────────────────
    await prompt.edit(
        f"✅ **{total} videos found.**\n\n"
        f"Enter the **starting number** (1 to {total}):\n"
        f"_(Send `1` to start from the beginning)_"
    )
    try:
        idx_msg: Message = await bot.listen(chat_id, timeout=60)
    except asyncio.TimeoutError:
        await prompt.edit("⌛ Timed out. Send /upload to retry.")
        return
    await idx_msg.delete()

    try:
        start_index = max(1, int(idx_msg.text.strip()))
    except (ValueError, AttributeError):
        start_index = 1

    # ── Step 3: batch name ────────────────────────────────────────────────────
    await prompt.edit("📝 **Enter a batch name** (shown in the video caption):")
    try:
        batch_msg: Message = await bot.listen(chat_id, timeout=60)
    except asyncio.TimeoutError:
        await prompt.edit("⌛ Timed out. Send /upload to retry.")
        return
    await batch_msg.delete()
    batch_name = batch_msg.text.strip() if batch_msg.text else "Batch"

    # ── Step 4: quality ───────────────────────────────────────────────────────
    await prompt.edit(
        "📸 **Choose video quality:**\n"
        "`144 / 240 / 360 / 480 / 720 / 1080`\n"
        "_(Send the number)_"
    )
    try:
        qual_msg: Message = await bot.listen(chat_id, timeout=60)
    except asyncio.TimeoutError:
        await prompt.edit("⌛ Timed out. Send /upload to retry.")
        return
    await qual_msg.delete()

    quality_raw = (qual_msg.text or "720").strip()
    quality = quality_raw if quality_raw in ("144","240","360","480","720","1080") else "720"

    # ── Step 5: caption ───────────────────────────────────────────────────────
    await prompt.edit(
        "💬 **Enter a caption** to add on each video\n"
        "_(or send `default` to use the video name only)_"
    )
    try:
        cap_msg: Message = await bot.listen(chat_id, timeout=60)
    except asyncio.TimeoutError:
        await prompt.edit("⌛ Timed out. Send /upload to retry.")
        return
    await cap_msg.delete()
    caption_template = cap_msg.text.strip() if cap_msg.text else "default"

    # ── Step 6: thumbnail ─────────────────────────────────────────────────────
    await prompt.edit(
        "🖼 **Send thumbnail URL** (must start with http/https)\n"
        "or send `no` to auto-extract from video"
    )
    try:
        thumb_msg: Message = await bot.listen(chat_id, timeout=60)
    except asyncio.TimeoutError:
        await prompt.edit("⌛ Timed out. Send /upload to retry.")
        return
    await thumb_msg.delete()
    await prompt.delete()

    thumb_input = (thumb_msg.text or "no").strip()
    thumb_url = None
    if thumb_input.startswith(("http://", "https://")):
        thumb_url = thumb_input
        # Download thumbnail once for the whole batch
        rc, _ = getstatusoutput(f"wget -q '{thumb_url}' -O 'thumb.jpg'")
        if rc != 0 or not os.path.exists("thumb.jpg"):
            await m.reply_text("⚠️ Could not download thumbnail URL — will auto-extract.")
            thumb_url = None

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = await m.reply_text(
        f"🚀 **Starting batch…**\n\n"
        f"📂 **Batch:** `{batch_name}`\n"
        f"📹 **Quality:** {quality}p\n"
        f"🔢 **Videos:** {start_index}–{total} of {total}\n\n"
        f"Send /stop to cancel at any time."
    )

    # ── Main download/upload loop ─────────────────────────────────────────────
    success_count = 0
    fail_count    = 0

    for i, (raw_title, url) in enumerate(links[start_index - 1 :], start=start_index):

        if _is_stopped(chat_id):
            await summary.edit(
                f"🛑 **Stopped by user.**\n"
                f"✅ Done: {success_count}  ❌ Failed: {fail_count}"
            )
            break

        # ── Build display name ─────────────────────────────────────────────
        name = helper.numbered_name(i, raw_title)

        # ── Announce ──────────────────────────────────────────────────────
        status_msg = await m.reply_text(
            f"⬇️ **Downloading ({i}/{total})…**\n"
            f"📝 **Name:** `{name}`\n"
            f"🔗 **URL:** `{url[:80]}…`"
        )

        # ── Resolve URL (ClassX / AppX special handling) ──────────────────
        actual_url = url
        if "fetch_video" in url or "classx" in url or "appx" in url:
            try:
                actual_url = await helper.resolve_classx_url(url)
                if actual_url != url:
                    logger.info("Resolved to: %s", actual_url[:80])
            except Exception as resolve_err:
                logger.warning("URL resolve failed: %s", resolve_err)

        # ── Build caption ──────────────────────────────────────────────────
        if caption_template.lower() == "default":
            caption = f"🎬 **{name}**\n📚 **Batch:** {batch_name}"
        else:
            caption = f"{caption_template}\n\n🎬 **{name}**\n📚 **Batch:** {batch_name}"

        # ── PDF / document ─────────────────────────────────────────────────
        if url.lower().endswith(".pdf") or "drive.google.com" in url:
            try:
                pdf_path = await helper.download_pdf(actual_url, helper.safe_filename(raw_title))
                if pdf_path:
                    await m.reply_document(pdf_path, caption=caption)
                    helper._cleanup(pdf_path)
                    success_count += 1
                else:
                    raise RuntimeError("PDF download returned no file")
            except Exception as e:
                fail_count += 1
                await m.reply_text(
                    f"❌ **Download failed** (PDF)\n"
                    f"**Name:** `{name}`\n"
                    f"**Error:** `{type(e).__name__}: {e}`\n"
                    f"**URL:** `{url[:120]}`"
                )
            finally:
                await _safe_delete(status_msg)
            await asyncio.sleep(1)
            continue

        # ── Video download ─────────────────────────────────────────────────
        name_stem = helper.safe_filename(raw_title)

        try:
            filepath = await helper.download_video(actual_url, name_stem, quality)

            # Fallback: try ffmpeg if yt-dlp produced nothing
            if not filepath and ".m3u8" in actual_url:
                await status_msg.edit(
                    f"🔄 **Trying ffmpeg fallback…** `{name}`"
                )
                filepath = await helper.download_with_ffmpeg(actual_url, name_stem)

            if not filepath:
                raise RuntimeError(
                    "Download produced no output file. "
                    "Possible causes: token expired, DRM-encrypted stream, "
                    "IP-blocked by CDN, or unsupported URL format."
                )

            await _safe_delete(status_msg)

            # ── Upload ──────────────────────────────────────────────────────
            await helper.send_vid(bot, m, caption, filepath, thumb_url, name)
            success_count += 1

        except Exception as e:
            fail_count += 1
            await _safe_delete(status_msg)
            await m.reply_text(
                f"❌ **Download failed** ({i}/{total})\n"
                f"**Name:** `{name}`\n"
                f"**Error:** `{type(e).__name__}: {str(e)[:200]}`\n"
                f"**URL:** `{url[:120]}`"
            )

        finally:
            # Ensure stale partial files are cleaned up
            for ext in (".mp4", ".mkv", ".webm", ".mp4.webm", ".part"):
                stale = name_stem + ext
                if os.path.exists(stale):
                    try:
                        os.remove(stale)
                    except Exception:
                        pass

        await asyncio.sleep(2)   # brief pause between videos

    # ── Batch complete ────────────────────────────────────────────────────────
    if not _is_stopped(chat_id):
        await summary.edit(
            f"✅ **Batch Complete!**\n\n"
            f"📂 **Batch:** {batch_name}\n"
            f"✅ **Uploaded:** {success_count}\n"
            f"❌ **Failed:** {fail_count}\n"
            f"📹 **Total:** {total}"
        )

    # Clean up shared thumbnail
    if thumb_url and os.path.exists("thumb.jpg"):
        helper._cleanup("thumb.jpg")

    _set_stop(chat_id, False)


# ─────────────────────── helpers ─────────────────────────────────────────────
async def _safe_delete(msg: Message):
    try:
        await msg.delete()
    except Exception:
        pass


# ─────────────────────── aiohttp keep-alive (Render) ─────────────────────────
async def health_handler(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health server running on port %d", PORT)


# ─────────────────────── entry point ─────────────────────────────────────────
async def main():
    await start_web_server()
    await bot.start()
    logger.info("Bot started. Waiting for messages…")
    await asyncio.Event().wait()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
