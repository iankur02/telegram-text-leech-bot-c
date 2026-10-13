"""
parser.py  –  parse the .txt file and return (name, url) pairs.

Supported line formats (all seen in the wild):
  1.  "Name : https://..."
  2.  "001) Name : https://..."
  3.  "[Section] Name : https://..."
  4.  "Name - https://..."
  5.  "Name https://..."          (space only)
  6.  "https://..."               (URL-only – uses generic name)
Lines without a URL are treated as section headers and ignored.
"""

import re
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Regex that matches a URL starting with http:// or https://
_URL_RE = re.compile(r'(https?://\S+)', re.IGNORECASE)

# Characters we strip from the trailing edge of the name portion
_TRAIL_STRIP = re.compile(r'[\s:\-|]+$')

# Leading numbering like "001)" or "1."
_LEAD_NUM_RE = re.compile(r'^\s*\d+[\).\-]\s*')


def parse_txt_file(content: str) -> List[Tuple[str, str]]:
    """
    Return a list of (name, url) tuples from *content*.
    Empty / header-only lines are skipped silently.
    """
    entries: List[Tuple[str, str]] = []
    counter = 1

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _URL_RE.search(line)
        if not m:
            # No URL on this line → section header, skip
            logger.debug("Skipping header line: %s", line[:80])
            continue

        url = m.group(1).rstrip('.,;)')   # trim common trailing punctuation

        # Everything before the URL is the name candidate
        name_raw = line[: m.start()]
        name_raw = _TRAIL_STRIP.sub('', name_raw)   # strip trailing  : - |
        name_raw = _LEAD_NUM_RE.sub('', name_raw)    # strip leading  001)

        name = name_raw.strip()
        if not name:
            name = f"Video {counter:03d}"

        entries.append((name, url))
        counter += 1

    logger.info("Parsed %d video entries", len(entries))
    return entries


def apply_range(entries: List[Tuple[str, str]], range_str: str) \
        -> List[Tuple[int, str, str]]:
    """
    Return a filtered list of (1-based-index, name, url).

    range_str examples
    ------------------
    "all"        → every entry
    "5"          → only the 5th
    "1-10"       → 1 through 10
    "1-5,8,10-15"→ mixed
    """
    total = len(entries)
    if not entries:
        return []

    text = range_str.strip().lower()

    if text in ('all', '*', ''):
        return [(i + 1, name, url) for i, (name, url) in enumerate(entries)]

    selected: set = set()
    for part in text.replace(' ', '').split(','):
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                a, b = int(a), int(b)
                a = max(1, a)
                b = min(total, b)
                selected.update(range(a, b + 1))
            except ValueError:
                logger.warning("Bad range segment: %s", part)
        else:
            try:
                i = int(part)
                if 1 <= i <= total:
                    selected.add(i)
            except ValueError:
                logger.warning("Bad index: %s", part)

    result = []
    for i in sorted(selected):
        name, url = entries[i - 1]
        result.append((i, name, url))

    return result
