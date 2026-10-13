"""
handler.py  –  all Telegram message handlers (txt upload, stop, range reply).
"""

import asyncio
import logging
import os
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.types import Message

from config import Config
from modules.downloader import download_video
from modules.parser import apply_range, parse_txt_file
from modules.uploader import generate_thumbnail, upload_video
from modules.utils import human_size, human_time, safe_remove

logger = logging.getLogger(__name__)

# ── Per-user state ───────────────────────────────────────────────────────────

# user_id → asyncio.Event  (set it to request a stop)
_stop_events: Dict[int, asyncio.Event] = {}

# user_id → asyncio.Queue  (used to pass range replies from the message handler)
_range_queues: Dict[int, asyncio.Queue] = {}

# ── Exported coroutines (called from bot.py) ─────────────────────────────────


async def cmd_stop(client: Client, message: Message) -> None:
    uid = message.from_user.id
    ev  = _stop_events.get(uid)
    if ev and not ev.is_set():
        ev.set()
        await message.reply_text("🛑 Stop signal sent. Finishing the current video…")
    else:
        await message.reply_text("ℹ️ No active upload session to stop.")


async def on_txt_file(client: Client, message: Message) -> None:
    """Entry point when a user sends a .txt file."""
    uid = message.from_user.id

    # ── Access control ───────────────────────────────────────────────────────
    if Config.ALLOWED_USERS and uid not in Config.ALLOWED_USERS:
        await message.reply_text("⛔ You are not authorised to use this bot.")
        return

    # ── Only one active session per user ─────────────────────────────────────
    if uid in _stop_events and not _stop_events[uid].is_set():
        await message.reply_text(
            "⚠️ You already have an active upload session.\n"
            "Send /stop to cancel it, then try again."
        )
        return

    status = await message.reply_text("📥 Receiving your TXT file…")

    # ── Download the txt to disk ─────────────────────────────────────────────
    try:
        txt_path = await client.download_media(message)
    except Exception as exc:
        await status.edit_text(f"❌ Failed to download TXT file: `{exc}`")
        return

    if not txt_path or not os.path.isfile(txt_path):
        await status.edit_text("❌ TXT file download failed (empty path).")
        return

    # ── Read + parse ─────────────────────────────────────────────────────────
    try:
        with open(txt_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except Exception as exc:
        await status.edit_text(f"❌ Cannot read TXT file: `{exc}`")
        safe_remove(txt_path)
        return
    finally:
        safe_remove(txt_path)

    entries: List[Tuple[str, str]] = parse_txt_file(content)

    if not entries:
        await status.edit_text(
            "❌ No valid video entries found in the file.\n\n"
            "Expected format per line:\n"
            "`Video Name : https://...`"
        )
        return

    total = len(entries)

    # ── Show preview and ask for range ───────────────────────────────────────
    lines = [f"📋 **Found {total} video(s):**\n"]
    for i, (name, _url) in enumerate(entries[:20], 1):
        lines.append(f"`{i:3d}.` {name[:65]}")
    if total > 20:
        lines.append(f"_… and {total - 20} more_")

    lines += [
        "",
        "📌 **Reply with your range** (or `all`):",
        "  • `all`  — every video",
        "  • `1-10` — videos 1 to 10",
        "  • `5`    — only video 5",
        "  • `1-5,8,10-15` — mixed",
        "",
        "_Waiting 120 s for your reply…_",
    ]
    await status.edit_text("\n".join(lines))

    # ── Wait for the range reply ─────────────────────────────────────────────
    q: asyncio.Queue = asyncio.Queue()
    _range_queues[uid] = q

    try:
        range_str: str = await asyncio.wait_for(q.get(), timeout=Config.REPLY_TIMEOUT)
    except asyncio.TimeoutError:
        _range_queues.pop(uid, None)
        await status.edit_text("⏰ Timed out waiting for range. Operation cancelled.")
        return
    finally:
        _range_queues.pop(uid, None)

    # ── Build the selected list ───────────────────────────────────────────────
    selected = apply_range(entries, range_str)
    if not selected:
        await status.edit_text(
            "❌ No valid entries in the given range.\n"
            "Please send the TXT file again and try a different range."
        )
        return

    # ── Launch batch processing ───────────────────────────────────────────────
    stop_ev = asyncio.Event()
    _stop_events[uid] = stop_ev

    try:
        await _process_batch(client, message.chat.id, selected, status, stop_ev)
    finally:
        _stop_events.pop(uid, None)


async def on_text_reply(client: Client, message: Message) -> None:
    """
    Called for every plain-text message.  If the user has a pending range
    queue we forward the text into it; otherwise we show a hint.
    """
    uid = message.from_user.id
    q   = _range_queues.get(uid)
    if q:
        await q.put(message.text.strip())
    # If no queue is pending we just ignore plain text silently
    # (or the /start handler already replied).


# ── Batch processing ─────────────────────────────────────────────────────────

async def _process_batch(
    client: Client,
    chat_id: int,
    selected: List[Tuple[int, str, str]],
    status_msg: Message,
    stop_ev: asyncio.Event,
) -> None:
    total      = len(selected)
    success    = 0
    failed     = 0
    failed_list: List[str] = []
    batch_start = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="txtleech_") as tmproot:
        for batch_idx, (orig_num, name, url) in enumerate(selected, 1):
            if stop_ev.is_set():
                break

            short_name = name[:55]
            logger.info("[%d/%d] Processing: %s", batch_idx, total, short_name)

            # Status: downloading
            try:
                await status_msg.edit_text(
                    f"⬇️ **Downloading** [{batch_idx}/{total}]\n"
                    f"🎬 **{short_name}**\n"
                    f"✅ {success}  ❌ {failed}"
                )
            except Exception:
                pass

            # Each video gets its own sub-directory to prevent name collisions
            video_dir = os.path.join(tmproot, f"v{batch_idx:04d}")
            os.makedirs(video_dir, exist_ok=True)

            # ── Progress callback for download display ──────────────────────
            _last_dl_edit = [0.0]

            def _dl_progress(downloaded: int, total_b: int, speed: float, eta: int):
                now = time.monotonic()
                if now - _last_dl_edit[0] < 3:
                    return
                _last_dl_edit[0] = now
                if total_b > 0:
                    pct = downloaded / total_b * 100
                    spd = f"{human_size(int(speed))}/s" if speed else "?"
                    try:
                        asyncio.get_event_loop().call_soon_threadsafe(
                            asyncio.ensure_future,
                            status_msg.edit_text(
                                f"⬇️ **Downloading** [{batch_idx}/{total}]\n"
                                f"🎬 **{short_name}**\n"
                                f"📊 {pct:.1f}%  🚀 {spd}  ⏱ {human_time(eta)}\n"
                                f"✅ {success}  ❌ {failed}"
                            ),
                        )
                    except Exception:
                        pass

            # ── Download ────────────────────────────────────────────────────
            filepath = await download_video(
                url=url,
                name=name,
                temp_dir=video_dir,
                progress_cb=_dl_progress,
                stop_event=stop_ev,
            )

            if stop_ev.is_set():
                break

            if not filepath:
                # Download failed – log and continue
                failed += 1
                failed_list.append(f"{orig_num}. {name[:60]}")
                logger.warning("Download failed for: %s", name[:60])
                try:
                    await status_msg.edit_text(
                        f"⚠️ **Download failed** [{batch_idx}/{total}]\n"
                        f"🎬 {short_name}\n"
                        f"Moving to next video…"
                    )
                except Exception:
                    pass
                await asyncio.sleep(1)
                continue

            # ── Thumbnail ───────────────────────────────────────────────────
            thumb_path = os.path.join(video_dir, "thumb.jpg")
            has_thumb  = await generate_thumbnail(filepath, thumb_path)

            # Status: uploading
            try:
                await status_msg.edit_text(
                    f"📤 **Uploading** [{batch_idx}/{total}]\n"
                    f"🎬 **{short_name}**\n"
                    f"✅ {success}  ❌ {failed}"
                )
            except Exception:
                pass

            # Caption shown on the video in Telegram
            caption = f"🎬 **{batch_idx:03d}. {name}**"

            # ── Upload ──────────────────────────────────────────────────────
            ok = await upload_video(
                client    = client,
                chat_id   = chat_id,
                filepath  = filepath,
                caption   = caption,
                thumb_path= thumb_path if has_thumb else None,
                status_msg= status_msg,
                stop_event= stop_ev,
            )

            # Dump to extra channel if configured
            if ok and Config.DUMP_CHANNEL:
                try:
                    await client.send_video(
                        chat_id    = Config.DUMP_CHANNEL,
                        video      = filepath,
                        caption    = caption,
                        supports_streaming = True,
                    )
                except Exception as exc:
                    logger.warning("Dump-channel upload failed: %s", exc)

            # ── Cleanup video files immediately to free disk ─────────────────
            safe_remove(filepath, thumb_path if has_thumb else None)

            if ok:
                success += 1
            else:
                failed += 1
                failed_list.append(f"{orig_num}. {name[:60]}")

            # Brief pause so we don't flood Telegram
            await asyncio.sleep(1.5)

    # ── Final summary ─────────────────────────────────────────────────────────
    elapsed  = human_time(int(time.monotonic() - batch_start))
    stopped  = "🛑 **Stopped early.**\n\n" if stop_ev.is_set() else ""

    summary  = (
        f"{stopped}"
        f"✅ **Batch complete!**\n\n"
        f"📊 **Summary**\n"
        f"✅ Uploaded : {success} / {total}\n"
        f"❌ Failed   : {failed} / {total}\n"
        f"⏱ Total time: {elapsed}\n"
    )

    if failed_list:
        cap = failed_list[:10]
        summary += "\n❌ **Failed videos:**\n" + "\n".join(cap)
        if len(failed_list) > 10:
            summary += f"\n_…and {len(failed_list) - 10} more_"

    try:
        await status_msg.edit_text(summary)
    except Exception:
        try:
            await client.send_message(chat_id, summary)
        except Exception:
            pass
