"""Derived meeting metadata: language, participants, transcription model, status.

None of this is recorded by Meetily. `retranscribed_at` in metadata.json is the
only hard signal it leaves behind; everything else here is inferred from that
flag, from Meetily's own language detection, or from the transcript text.
The knobs live in config.py.
"""
from __future__ import annotations

import re
from typing import Any

from config import (
    BASELINE_MODEL, ENHANCED_MODEL, KNOWN_PEOPLE, OWNER_NAME, LANGUAGE_NAMES,
)

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_UKRAINIAN_ONLY = set("іїєґІЇЄҐ")
# Below this share of Cyrillic letters a transcript is treated as English.
# Measured over 174 local transcripts the two clusters sit at ~0.00 (English)
# and ~0.95 (Ukrainian) with nothing between 0.41 and 0.63, so 0.50 is the
# widest-margin split. Agrees with Meetily's own detection on all 46 meetings
# where Meetily recorded one.
_CYRILLIC_THRESHOLD = 0.50
_LANGUAGE_SAMPLE_CHARS = 20000
# A four-line mic check is genuinely ambiguous; say nothing rather than guess.
_MIN_LETTERS_FOR_LANGUAGE = 200


def _transcript_text(segments: list[dict[str, str]], limit: int | None = None) -> str:
    parts: list[str] = []
    total = 0
    for seg in segments:
        text = seg.get("text", "")
        parts.append(text)
        total += len(text)
        if limit is not None and total >= limit:
            break
    return " ".join(parts)


def language_name(code: str) -> str:
    """Map a BCP-47-ish code onto the word we put in frontmatter."""
    base = str(code or "").strip().split("-")[0].lower()
    if not base:
        return ""
    return LANGUAGE_NAMES.get(base, base)


def detect_language_code(text: str) -> str:
    """Cyrillic-share heuristic, used only when Meetily has no answer."""
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < _MIN_LETTERS_FOR_LANGUAGE:
        return ""
    cyrillic = sum(1 for ch in letters if _CYRILLIC.match(ch))
    if cyrillic / len(letters) < _CYRILLIC_THRESHOLD:
        return "en"
    return "uk" if any(ch in _UKRAINIAN_ONLY for ch in text) else "ru"


def resolve_language(metadata: dict[str, Any], segments: list[dict[str, str]]) -> str:
    """Meetily's own detection first (it sees the audio), then the heuristic.

    summary_language is the override you set in Meetily; detected_summary_language
    is what Meetily worked out for itself. Both are absent on older meetings.
    """
    for field in ("summary_language", "detected_summary_language"):
        name = language_name(str(metadata.get(field) or ""))
        if name:
            return name
    return language_name(detect_language_code(_transcript_text(segments, _LANGUAGE_SAMPLE_CHARS)))


def detect_participants(segments: list[dict[str, str]]) -> list[str]:
    """Suggest who was in the room by matching the roster against the transcript.

    A suggestion only — someone merely talked about counts the same as someone
    present. Sorted by how often they come up.
    """
    if not KNOWN_PEOPLE and not OWNER_NAME:
        return []

    text = _transcript_text(segments)
    counts: dict[str, int] = {}
    for name, aliases in KNOWN_PEOPLE.items():
        # Matching is case-insensitive, so fold duplicate spellings first —
        # otherwise a repeated alias counts the same mention several times and
        # skews the ordering below.
        seen: dict[str, str] = {}
        for alias in (aliases or [name]):
            alias = str(alias).strip()
            if alias:
                seen.setdefault(alias.lower(), alias)
        hits = sum(
            len(re.findall(rf"(?<!\w){re.escape(alias)}(?!\w)", text, re.IGNORECASE))
            for alias in seen.values()
        )
        if hits:
            counts[name] = hits

    found = sorted(counts, key=lambda n: (-counts[n], n))
    if OWNER_NAME:
        found = [OWNER_NAME] + [n for n in found if n != OWNER_NAME]
    return found


def resolve_status(metadata: dict[str, Any]) -> str:
    """baseline = live Turbo pass; enhanced = a retranscribe has run since."""
    return "enhanced" if metadata.get("retranscribed_at") else "baseline"


def resolve_model(metadata: dict[str, Any]) -> str:
    return ENHANCED_MODEL if metadata.get("retranscribed_at") else BASELINE_MODEL
