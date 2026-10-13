"""
downloader.py  –  download a single video URL to a local file.

Strategy (in order):
  1. yt-dlp  (handles HLS, mp4, most CDN redirect chains)
  2. ffmpeg -c copy  (good fallback for HLS / signed CDN streams)

Each strategy is retried up to *retries* times with exponential back-off.
All filenames are sanitised with sanitize_filename() before being used so
that special characters (parentheses, ampersands, etc.) can never break
a shell command or an ffprobe call later.
"""

import asyncio
import logging
import os
from typing import Callable, Optional

import yt_dlp

from config import Config
from modules.utils import sanitize_filename

logger = logging.getLogger(__name__)

# Video extensions we look for after download
_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".ts", ".m4v"}

# ── Public API ───────────────────────────────────────────────────────────────


async def download_video(
    url: str,
    name: str,
    temp_dir: str,
    progress_cb: Optional[Callable[[int, int, float, int], None]] = None,
    stop_event: Optional[asyncio.Event] = None,
    retries: int = None,
) -> Optional[str]:
    """
    Download *url* into *temp_dir*, naming the file after *name*.

    Parameters
    ----------
    url          : remote video URL
    name         : human-readable video name (will be sanitised)
    temp_dir     : existing directory to write into
    progress_cb  : optional  fn(downloaded_bytes, total_bytes, speed, eta)
    stop_event   : set this to abort the download
    retries      : how many times to retry (defaults to Config.DOWNLOAD_RETRIES)

    Returns
    -------
    Absolute path of the downloaded file, or *None* on failure.
    """
    if retries is None:
        retries = Config.DOWNLOAD_RETRIES

    safe_stem = sanitize_filename(name) or "video"
    logger.info("Starting download  name=%r  stem=%r", name[:60], safe_stem)

    # ── Strategy 1: yt-dlp ──────────────────────────────────────────────────
    for attempt in range(1, retries + 1):
        if _stopped(stop_event):
            return None

        logger.info("yt-dlp attempt %d/%d: %s", attempt, retries, safe_stem)
        path = await _ydl_download(url, temp_dir, safe_stem, progress_cb, stop_event)

        if path:
            return path

        if attempt < retries:
            wait = 2 ** attempt          # 2 s, 4 s, 8 s …
            logger.info("Retrying in %ds…", wait)
            await asyncio.sleep(wait)

    if _stopped(stop_event):
        return None

    # ── Strategy 2: ffmpeg direct copy ──────────────────────────────────────
    logger.info("yt-dlp failed – trying ffmpeg fallback for: %s", safe_stem)
    return await _ffmpeg_download(url, temp_dir, safe_stem, stop_event)


# ── yt-dlp helper ───────────────────────────────────────────────────────────

async def _ydl_download(
    url: str,
    temp_dir: str,
    stem: str,
    progress_cb: Optional[Callable],
    stop_event: Optional[asyncio.Event],
) -> Optional[str]:
    """Run yt-dlp in a thread-pool executor and return the local path."""

    outtmpl = os.path.join(temp_dir, f"{stem}.%(ext)s")

    def _hook(d: dict):
        if _stopped(stop_event):
            raise yt_dlp.utils.DownloadError("__user_stop__")
        if d.get("status") == "downloading" and progress_cb:
            try:
                downloaded = d.get("downloaded_bytes") or 0
                total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed      = float(d.get("speed") or 0)
                eta        = int(d.get("eta") or 0)
                progress_cb(downloaded, total, speed, eta)
            except Exception:
                pass

    ydl_opts = {
        "outtmpl"                      : outtmpl,
        "format"                       : (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo+bestaudio/best[ext=mp4]/best"
        ),
        "merge_output_format"          : "mp4",
        "quiet"                        : True,
        "no_warnings"                  : True,
        "retries"                      : 5,
        "fragment_retries"             : 10,
        "concurrent_fragment_downloads": 4,
        "socket_timeout"               : 30,
        "http_headers"                 : {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        "progress_hooks"               : [_hook],
        "ignoreerrors"                 : False,
        "extractor_retries"            : 3,
        # Don't write thumbnail/subtitle files we don't need
        "writethumbnail"               : False,
        "writesubtitles"               : False,
        "writeautomaticsub"            : False,
        "postprocessors"               : [],
    }

    try:
        loop = asyncio.get_event_loop()

        def _run():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await asyncio.wait_for(
            loop.run_in_executor(None, _run),
            timeout=Config.DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("yt-dlp timed out after %ds for: %s", Config.DOWNLOAD_TIMEOUT, stem)
        return None
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if "__user_stop__" in msg:
            logger.info("Download stopped by user: %s", stem)
        else:
            logger.error("yt-dlp DownloadError for %s: %s", stem, msg[:300])
        return None
    except Exception as exc:
        logger.error("yt-dlp unexpected error for %s: %s", stem, exc)
        return None

    return _find_file(temp_dir, stem)


# ── ffmpeg helper ────────────────────────────────────────────────────────────

async def _ffmpeg_download(
    url: str,
    temp_dir: str,
    stem: str,
    stop_event: Optional[asyncio.Event],
) -> Optional[str]:
    """
    Fallback: use  ffmpeg -i URL -c copy output.mp4
    Works well for HLS (.m3u8) and signed CDN streams that yt-dlp can't extract.
    """
    out_path = os.path.join(temp_dir, f"{stem}.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-i", url,              # ← url passed as list arg, NOT in shell string
        "-c", "copy",
        "-movflags", "+faststart",
        out_path,               # ← safe: list arg, sanitised path
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Poll for stop signal
        while proc.returncode is None:
            if _stopped(stop_event):
                logger.info("ffmpeg download stopped by user: %s", stem)
                proc.kill()
                await proc.wait()
                return None
            await asyncio.sleep(1)

        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "ffmpeg exited %d for %s: %s",
                proc.returncode, stem, stderr.decode(errors="replace")[:300],
            )
            return None

    except asyncio.TimeoutError:
        logger.error("ffmpeg timed out for: %s", stem)
        return None
    except Exception as exc:
        logger.error("ffmpeg exception for %s: %s", stem, exc)
        return None

    # Verify result
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        logger.info("ffmpeg download OK: %s", out_path)
        return out_path

    logger.error("ffmpeg produced no output for: %s", stem)
    return None


# ── Internal helpers ─────────────────────────────────────────────────────────

def _find_file(directory: str, stem: str) -> Optional[str]:
    """
    Locate the file that yt-dlp wrote.  yt-dlp may choose a different extension
    than mp4 (e.g. mkv when merging), so we search by stem first, then by any
    video extension.
    """
    # Exact-extension candidates in preference order
    for ext in (".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".ts"):
        p = os.path.join(directory, f"{stem}{ext}")
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p

    # Fuzzy: any file whose name starts with the stem
    stem_prefix = stem[:30]          # use first 30 chars to avoid edge cases
    try:
        for fname in sorted(os.listdir(directory)):
            if not fname.startswith(stem_prefix):
                continue
            _, ext = os.path.splitext(fname)
            if ext.lower() not in _VIDEO_EXTS:
                continue
            fp = os.path.join(directory, fname)
            if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                return fp
    except OSError:
        pass

    return None


def _stopped(event: Optional[asyncio.Event]) -> bool:
    return event is not None and event.is_set()
