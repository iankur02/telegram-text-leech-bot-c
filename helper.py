"""
helper.py  –  download & upload utilities for the Txt-Leech Bot.

All the root-cause bugs from the original code are fixed here:
  1. duration()     — never crashes on missing/corrupt files.
  2. find_file()    — reliably locates the yt-dlp output regardless of extension.
  3. download_video()  — checks the output actually exists before returning.
  4. send_vid()     — validates file existence; falls back to document if video fails.
  5. Shell safety   — yt-dlp is called with a list, not shell=True, so special
                      chars in filenames (parentheses, brackets, etc.) are safe.
"""

import os
import re
import time
import asyncio
import logging
import aiohttp
import aiofiles
import subprocess

from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait

from utils import progress_bar

logger = logging.getLogger(__name__)

# ─────────────────────────── filename helpers ────────────────────────────────

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\[\]{}()\'^`~!@#$%&+=;,]')

def safe_filename(raw: str, max_len: int = 60) -> str:
    """Return a shell-safe, filesystem-safe filename stem (no extension)."""
    cleaned = _UNSAFE.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Remove trailing dots/spaces (Windows can't handle them)
    cleaned = cleaned.rstrip(". ")
    return cleaned[:max_len] if cleaned else "video"


def numbered_name(index: int, raw_title: str) -> str:
    """Return '001) <safe title>'."""
    stem = safe_filename(raw_title)
    return f"{str(index).zfill(3)}) {stem}"


# ─────────────────────────── duration ────────────────────────────────────────

def get_duration(filepath: str) -> int:
    """
    Return video duration in seconds (0 on any failure).
    The original bug: float(result.stdout) crashed when ffprobe printed an
    error message instead of a number because the file didn't exist.
    """
    if not filepath or not os.path.exists(filepath):
        return 0
    if os.path.getsize(filepath) == 0:
        return 0
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        return int(float(result.stdout.decode("utf-8", errors="ignore").strip()))
    except Exception as exc:
        logger.warning("ffprobe failed for %s: %s", filepath, exc)
        return 0


# ─────────────────────────── file-finder ─────────────────────────────────────

def find_downloaded_file(stem: str) -> str | None:
    """
    yt-dlp may add extensions we didn't request (.mkv, .webm, .mp4 …).
    Search common extensions and return the first file found with size > 0.
    """
    candidates = [
        stem,                       # exact match (stem already has extension)
        stem + ".mp4",
        stem + ".mkv",
        stem + ".webm",
        stem + ".mp4.webm",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    # Wider glob-style scan in the current directory
    base = os.path.basename(stem)
    for fname in os.listdir("."):
        if fname.startswith(base) and os.path.getsize(fname) > 0:
            return fname
    return None


# ─────────────────────────── thumbnail ───────────────────────────────────────

def extract_thumbnail(video_path: str) -> str | None:
    """Extract a thumbnail at the 1-minute mark (or 5s for short videos)."""
    thumb_path = video_path + ".jpg"
    dur = get_duration(video_path)
    seek = min(60, max(5, dur // 3))
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek),
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as exc:
        logger.warning("Thumbnail extraction failed: %s", exc)
    return None


# ─────────────────────────── URL resolvers ───────────────────────────────────

async def resolve_classx_url(url: str) -> str:
    """
    ClassX / AppX fetch_video URLs often return JSON or redirect to an m3u8.
    We follow redirects and try to extract the real stream URL.
    Returns the resolved URL, or the original if resolution fails.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://yesofficerapi.classx.co.in/",
        "Accept": "*/*",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                final_url = str(resp.url)
                content_type = resp.content_type or ""

                # Direct m3u8 redirect
                if ".m3u8" in final_url or "mpegurl" in content_type:
                    return final_url

                # JSON response containing a video URL
                try:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        for key in ("url", "videoUrl", "video_url", "stream_url",
                                    "hlsUrl", "hls_url", "mpd_url", "dashUrl"):
                            if data.get(key):
                                return data[key]
                except Exception:
                    pass

                # Scrape m3u8 URL from HTML/text body
                text = await resp.text(errors="ignore")
                m3u8 = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', text)
                if m3u8:
                    return m3u8.group(0)

    except Exception as exc:
        logger.warning("resolve_classx_url failed (%s): %s", url[:60], exc)

    return url   # fallback: return original


# ─────────────────────────── download ────────────────────────────────────────

_MAX_RETRIES = 3

async def download_video(url: str, name_stem: str, quality: str) -> str | None:
    """
    Download a video using yt-dlp (with aria2c acceleration).
    Returns the local filepath on success, None on failure.

    Key fixes vs. original:
    • Uses subprocess list args  → no shell injection from filenames.
    • Checks the output file actually exists & has size > 0.
    • Retries up to _MAX_RETRIES times with a small back-off.
    • Returns None (not a non-existent filename) so callers can check.
    """
    out_tmpl = f"{name_stem}.%(ext)s"

    if "youtu" in url:
        fmt = (
            f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]"
            f"/best[height<={quality}][ext=mp4]/best[height<={quality}]/best"
        )
    elif "jw-prod" in url or "jwplatform" in url:
        fmt = "best"
    else:
        fmt = (
            f"bestvideo[height<={quality}]+bestaudio"
            f"/best[height<={quality}]/best"
        )

    base_cmd = [
        "yt-dlp",
        "--retries", "10",
        "--fragment-retries", "10",
        "--no-abort-on-error",
        "--concurrent-fragments", "4",
        "-f", fmt,
        "-o", out_tmpl,
        "--merge-output-format", "mp4",
        "--add-header", "referer:https://yesofficerapi.classx.co.in/",
        "--add-header", (
            "user-agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]

    for attempt in range(1, _MAX_RETRIES + 1):
        logger.info("Download attempt %d/%d: %s", attempt, _MAX_RETRIES, url[:80])
        try:
            proc = await asyncio.create_subprocess_exec(
                *base_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=600)
            stderr_text = stderr_bytes.decode("utf-8", errors="ignore")

            result = find_downloaded_file(name_stem)
            if result:
                logger.info("Downloaded → %s", result)
                return result

            # Log yt-dlp stderr for debugging
            logger.warning("yt-dlp attempt %d no output file. stderr tail:\n%s",
                           attempt, stderr_text[-400:])
        except asyncio.TimeoutError:
            logger.warning("Download timed out on attempt %d", attempt)
        except Exception as exc:
            logger.warning("Download error attempt %d: %s", attempt, exc)

        if attempt < _MAX_RETRIES:
            await asyncio.sleep(3 * attempt)

    return None


async def download_with_ffmpeg(url: str, name_stem: str) -> str | None:
    """
    Fallback downloader using ffmpeg directly.
    Useful for plain m3u8 or mpd streams that yt-dlp struggles with.
    """
    out_path = f"{name_stem}.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-headers", "referer: https://yesofficerapi.classx.co.in/\r\n",
        "-i", url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        out_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=600)
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        logger.warning("ffmpeg fallback failed: %s", stderr_bytes[-300:].decode(errors="ignore"))
    except Exception as exc:
        logger.warning("ffmpeg fallback exception: %s", exc)
    return None


# ─────────────────────────── send ────────────────────────────────────────────

async def send_vid(
    bot: Client,
    m: Message,
    caption: str,
    filepath: str,
    thumb_url_or_none: str,
    name: str,
):
    """
    Send a downloaded video to Telegram.
    Falls back to send_document if reply_video fails.
    Cleans up all temp files afterwards.
    """
    # ------ sanity checks ------
    if not filepath or not os.path.exists(filepath):
        await m.reply_text(
            f"❌ **Upload skipped** — file not found after download.\n"
            f"**Name:** `{name}`"
        )
        return
    if os.path.getsize(filepath) == 0:
        await m.reply_text(f"❌ **Upload skipped** — file is empty.\n**Name:** `{name}`")
        _cleanup(filepath)
        return

    # ------ thumbnail ------
    if thumb_url_or_none and thumb_url_or_none.startswith(("http://", "https://")):
        thumbnail = "thumb.jpg"   # already downloaded in main handler
    else:
        thumbnail = extract_thumbnail(filepath)

    dur = get_duration(filepath)

    # ------ upload ------
    reply = await m.reply_text(f"**⬆️ Uploading...**\n`{name}`")
    start_time = time.time()

    try:
        await m.reply_video(
            filepath,
            caption=caption,
            supports_streaming=True,
            height=720,
            width=1280,
            thumb=thumbnail,
            duration=dur,
            progress=progress_bar,
            progress_args=(reply, start_time),
        )
    except FloodWait as e:
        time.sleep(e.value)
        await m.reply_video(
            filepath,
            caption=caption,
            supports_streaming=True,
            height=720,
            width=1280,
            thumb=thumbnail,
            duration=dur,
        )
    except Exception as vid_err:
        logger.warning("reply_video failed, trying document: %s", vid_err)
        try:
            await m.reply_document(
                filepath,
                caption=caption,
                progress=progress_bar,
                progress_args=(reply, start_time),
            )
        except Exception as doc_err:
            await m.reply_text(
                f"❌ **Upload failed for** `{name}`\n`{doc_err}`"
            )

    # ------ cleanup ------
    _cleanup(filepath)
    if thumbnail and thumbnail != "thumb.jpg":
        _cleanup(thumbnail)
    try:
        await reply.delete()
    except Exception:
        pass


def _cleanup(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


# ─────────────────────────── PDF / generic doc ───────────────────────────────

async def download_pdf(url: str, name_stem: str) -> str | None:
    """Download a PDF using aiohttp and return local path."""
    dest = f"{name_stem}.pdf"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    async with aiofiles.open(dest, "wb") as f:
                        await f.write(await resp.read())
                    if os.path.getsize(dest) > 0:
                        return dest
    except Exception as exc:
        logger.warning("PDF download failed: %s", exc)
    return None
