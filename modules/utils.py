"""
utils.py  –  filename sanitisation, ffprobe wrapper, formatting helpers.

THE MOST IMPORTANT FIX IS HERE:
  • sanitize_filename() strips every character that could break a shell command
    or a filesystem path.  The result is safe to pass as a plain argument to
    subprocess.run([..., filepath]) without shell=True.
  • get_video_info() ALWAYS passes filepath as a list element – never via a
    shell string – so special characters in the path cannot break the command.
  • get_video_info() checks that the file exists AND is non-empty before it
    calls ffprobe.  It never passes a string like "001) Foo (Bar: …" to float().
"""

import json
import logging
import os
import re
import subprocess
import unicodedata
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# ── Filename sanitisation ────────────────────────────────────────────────────

# Characters that are dangerous in filenames OR shell metacharacters.
# We keep: letters, digits, spaces, hyphens, underscores, dots.
_UNSAFE_CHARS = re.compile(r'[^\w\s.\-]', flags=re.UNICODE)
_MULTI_SPACE  = re.compile(r'[\s_]+')


def sanitize_filename(name: str, max_len: int = 60) -> str:
    """
    Return a filesystem- and shell-safe filename stem (no extension).

    Steps
    -----
    1. Unicode-normalise (NFKC) so accented chars become ASCII equivalents.
    2. Strip every character that is NOT a word char, space, hyphen, or dot.
    3. Collapse multiple spaces / underscores into one space.
    4. Strip leading/trailing spaces and dots.
    5. Truncate to *max_len* characters.
    6. If the result is empty, fall back to "video".
    """
    try:
        name = unicodedata.normalize('NFKC', name)
    except Exception:
        pass

    name = _UNSAFE_CHARS.sub(' ', name)
    name = _MULTI_SPACE.sub(' ', name).strip(' .')
    name = name[:max_len].strip(' .')

    return name if name else 'video'


# ── ffprobe wrapper ──────────────────────────────────────────────────────────

_FFPROBE_DEFAULTS: Dict = {"duration": 0, "width": 1280, "height": 720}


def get_video_info(filepath: str) -> Dict:
    """
    Return {duration (int seconds), width (int px), height (int px)}.

    Falls back to _FFPROBE_DEFAULTS on ANY error so the caller never crashes.
    CRITICAL: filepath is passed as a list element to subprocess – never
    concatenated into a shell string – so special characters are harmless.
    """
    # ── Guard: file must exist and be non-zero ───────────────────────────────
    if not filepath:
        logger.warning("get_video_info: empty filepath")
        return _FFPROBE_DEFAULTS.copy()

    if not os.path.isfile(filepath):
        logger.warning("get_video_info: not a file – %s", filepath)
        return _FFPROBE_DEFAULTS.copy()

    if os.path.getsize(filepath) == 0:
        logger.warning("get_video_info: zero-byte file – %s", filepath)
        return _FFPROBE_DEFAULTS.copy()

    # ── Run ffprobe (list args – NO shell=True) ──────────────────────────────
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                filepath,            # ← safe: list argument, not shell string
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            logger.warning(
                "ffprobe exited %d for %s: %s",
                result.returncode, filepath, result.stderr[:200],
            )
            return _FFPROBE_DEFAULTS.copy()

        data: dict = json.loads(result.stdout)

    except subprocess.TimeoutExpired:
        logger.warning("ffprobe timed out for: %s", filepath)
        return _FFPROBE_DEFAULTS.copy()

    except json.JSONDecodeError as exc:
        logger.warning("ffprobe JSON error for %s: %s", filepath, exc)
        return _FFPROBE_DEFAULTS.copy()

    except Exception as exc:
        logger.warning("ffprobe unexpected error for %s: %s", filepath, exc)
        return _FFPROBE_DEFAULTS.copy()

    # ── Parse duration ───────────────────────────────────────────────────────
    duration = 0
    try:
        raw = data.get("format", {}).get("duration", "0")
        duration = int(float(raw))     # float() is safe because raw is a str from JSON
    except (ValueError, TypeError):
        pass

    # ── Parse width / height from first video stream ─────────────────────────
    width, height = 1280, 720
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            try:
                width  = int(stream.get("width",  1280))
                height = int(stream.get("height",  720))
            except (ValueError, TypeError):
                pass
            break

    return {"duration": duration, "width": width, "height": height}


# ── Formatting helpers ───────────────────────────────────────────────────────

def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def human_time(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    m, s = divmod(total_seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def progress_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    filled = int(width * current / total)
    filled = min(filled, width)
    return "█" * filled + "░" * (width - filled)


# ── File cleanup ─────────────────────────────────────────────────────────────

def safe_remove(*paths: Optional[str]) -> None:
    """Delete files silently, ignoring errors."""
    for p in paths:
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except Exception:
                pass
