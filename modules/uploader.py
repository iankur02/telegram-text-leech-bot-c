"""
uploader.py  –  generate thumbnail and upload video to Telegram.
"""

import asyncio
import logging
import os
import time
from typing import Optional

from pyrogram import Client
from pyrogram.types import Message

from modules.utils import get_video_info, human_size, progress_bar, safe_remove

logger = logging.getLogger(__name__)

# Minimum seconds between two consecutive Telegram edit_text calls for progress
_PROGRESS_INTERVAL = 3.0


async def generate_thumbnail(video_path: str, thumb_path: str) -> bool:
    """
    Extract a JPEG frame at 10 s (or at 1 s if the video is very short) and
    scale it to 320 px wide.  Returns True on success.

    Uses subprocess list args – the paths are safe regardless of special chars.
    """
    info = get_video_info(video_path)
    seek = min(10, max(0, info["duration"] - 1)) if info["duration"] > 1 else 1

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek),
        "-i",  video_path,     # ← list arg
        "-frames:v", "1",
        "-vf", "scale=320:-1",
        "-q:v", "5",
        thumb_path,            # ← list arg
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=20)
        ok = os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0
        if not ok:
            logger.warning("Thumbnail generation produced no output: %s", video_path)
        return ok
    except asyncio.TimeoutError:
        logger.warning("Thumbnail generation timed out: %s", video_path)
        return False
    except Exception as exc:
        logger.warning("Thumbnail generation error: %s", exc)
        return False


async def upload_video(
    client: Client,
    chat_id: int,
    filepath: str,
    caption: str,
    thumb_path: Optional[str] = None,
    status_msg: Optional[Message] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> bool:
    """
    Upload *filepath* as a Telegram video to *chat_id*.

    Returns True on success, False on any failure.
    """
    # ── Pre-flight checks ────────────────────────────────────────────────────
    if not os.path.isfile(filepath):
        logger.error("upload_video: file not found: %s", filepath)
        return False

    file_size = os.path.getsize(filepath)
    if file_size == 0:
        logger.error("upload_video: zero-byte file: %s", filepath)
        return False

    info = get_video_info(filepath)
    logger.info(
        "Uploading %s  size=%s  dur=%ds  %dx%d",
        os.path.basename(filepath),
        human_size(file_size),
        info["duration"],
        info["width"],
        info["height"],
    )

    # ── Progress callback ────────────────────────────────────────────────────
    _last_edit = [0.0]

    async def _progress(current: int, total: int):
        if stop_event and stop_event.is_set():
            return
        now = time.monotonic()
        if now - _last_edit[0] < _PROGRESS_INTERVAL:
            return
        _last_edit[0] = now

        if status_msg and total > 0:
            bar = progress_bar(current, total)
            pct = current / total * 100
            try:
                await status_msg.edit_text(
                    f"📤 **Uploading…**\n"
                    f"`[{bar}]` {pct:.1f}%\n"
                    f"📦 {human_size(current)} / {human_size(total)}\n"
                    f"🎬 {caption[:55]}"
                )
            except Exception:
                pass

    # ── Send video ───────────────────────────────────────────────────────────
    send_kwargs = dict(
        chat_id         = chat_id,
        video           = filepath,
        caption         = caption,
        duration        = info["duration"],
        width           = info["width"],
        height          = info["height"],
        supports_streaming = True,
        progress        = _progress,
    )

    if thumb_path and os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
        send_kwargs["thumb"] = thumb_path

    try:
        await client.send_video(**send_kwargs)
        logger.info("Uploaded OK: %s", caption[:60])
        return True

    except Exception as exc:
        logger.error("send_video failed for %s: %s", caption[:60], exc)
        return False
