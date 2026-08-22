"""Configuration template.

Copy this to `config.py` and edit the three paths at the top:

    cp config.example.py config.py

`config.py` is gitignored, so your paths, roster and vault layout stay local.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ─────────────────────────────────────────────
# User settings
# ─────────────────────────────────────────────
MEETILY_FOLDER = Path("~/Movies/meetily-recordings").expanduser()
OBSIDIAN_FOLDER = Path("~/Documents/Obsidian Vault/meetings").expanduser()
MEETILY_DB = Path(
    "~/Library/Application Support/com.meetily.ai/meeting_minutes.sqlite"
).expanduser()

TAGS = ["meeting", "teams", "transcript"]
COPY_AUDIO = True
CHECK_DB_UPDATES_IN_WATCH = True

# ── Transcription model ──
BASELINE_MODEL = "whisper-large-v3-turbo"   # live recording
ENHANCED_MODEL = "whisper-large-v3"         # after Transcript → Retranscribe

# ── Participants ──
KNOWN_PEOPLE: dict[str, list[str]] = {
    # "Ada":   ["Ada", "Ada Lovelace"],
    # Whisper declines names, so list the forms you actually see:
    # "Taras": ["Taras", "Тарас", "Тараса", "Тарасу", "Тарасом"],
}
OWNER_NAME = ""

# ── Note location ──
# Subfolder under OBSIDIAN_FOLDER, as a pattern. Blank = one flat folder.
# Tokens: {year} {month} {day} {quarter} {date} {time} {title}
NOTE_SUBFOLDER_PATTERN = "{year}/{month}"

# ── Transcript timestamps ──
TRANSCRIPT_TIME_MODE = "elapsed"

# "lines"  → one line per segment: [MM:SS] text
# "blocks" → a "### <speaker> (MM:SS)"
TRANSCRIPT_LAYOUT = "lines"
SPEAKER_LABEL = "Speaker"

SUMMARY_PREFER_ENGLISH = True

LANGUAGE_NAMES = {
    "en": "english", "uk": "ukrainian", "de": "german",
    "pl": "polish", "es": "spanish", "fr": "french", "it": "italian",
}

STATE_FILE = Path.home() / ".meetily_sync_state.json"
LEGACY_CONVERTED_TRACKER = Path.home() / ".meetily_converted.txt"
LOG_FILE = Path.home() / "Library" / "Logs" / "meetily_converter.log"

CACHE_TTL = 60.0
MANAGED_BEGIN = "<!-- MEETILY:BEGIN -->"
MANAGED_END = "<!-- MEETILY:END -->"

# Bumped whenever the note layout changes, so existing notes re-render once
# without needing --resync.
NOTE_FORMAT_VERSION = 2

# ── Logging ──
handlers: list[logging.Handler] = []
try:
    handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3))
except OSError:
    handlers.append(logging.StreamHandler())

_fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
for _h in handlers:
    _h.setFormatter(_fmt)

root = logging.getLogger()
root.setLevel(logging.INFO)
root.handlers = handlers

log = logging.getLogger("meetily")
