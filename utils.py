import time
from datetime import timedelta
from pyrogram.errors import FloodWait


class Timer:
    def __init__(self, time_between: float = 5):
        self.start_time = time.time()
        self.time_between = time_between

    def can_send(self) -> bool:
        if time.time() > (self.start_time + self.time_between):
            self.start_time = time.time()
            return True
        return False


_timer = Timer(time_between=5)


def human_readable_size(value: float, digits: int = 2) -> str:
    """Convert bytes to a human-readable string."""
    if value is None:
        return "0B"
    chosen_unit = "B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        if value > 1000:
            value /= 1024
            chosen_unit = unit
        else:
            break
    return f"{value:.{digits}f}{chosen_unit}"


def human_readable_time(seconds: float, precision: int = 1) -> str:
    """Convert seconds to a human-readable ETA string."""
    pieces = []
    value = timedelta(seconds=int(seconds))

    if value.days:
        pieces.append(f"{value.days}d")

    secs = value.seconds
    if secs >= 3600:
        h = secs // 3600
        pieces.append(f"{h}h")
        secs -= h * 3600
    if secs >= 60:
        m = secs // 60
        pieces.append(f"{m}m")
        secs -= m * 60
    if secs > 0 or not pieces:
        pieces.append(f"{secs}s")

    return "".join(pieces[:precision] if precision else pieces)


async def progress_bar(current: int, total: int, reply, start: float):
    """Upload/download progress callback for Pyrogram."""
    if not _timer.can_send():
        return
    now = time.time()
    diff = now - start
    if diff < 1 or total == 0:
        return

    perc = current * 100 / total
    speed = current / diff
    eta = human_readable_time((total - current) / speed) if speed > 0 else "-"
    sp = human_readable_size(speed) + "/s"
    tot = human_readable_size(total)
    cur = human_readable_size(current)

    filled = int(current * 10 / total)
    bar = "▰" * filled + "▱" * (10 - filled)

    text = (
        f"**⬆️ Uploading...**\n\n"
        f"`{bar}` `{perc:.1f}%`\n"
        f"📦 **Size:** {cur} / {tot}\n"
        f"🚀 **Speed:** {sp}\n"
        f"⏱ **ETA:** {eta}"
    )
    try:
        await reply.edit(text)
    except FloodWait as e:
        time.sleep(e.value)
    except Exception:
        pass
