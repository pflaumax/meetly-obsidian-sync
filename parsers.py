from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config import TRANSCRIPT_TIME_MODE

log = logging.getLogger("meetily")


def format_offset(seconds: float) -> str:
    """Offset into audio.mp4, as MM:SS (or H:MM:SS past an hour)."""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def segment_time(seg: dict) -> str:
    """Best timestamp available for a segment."""
    if TRANSCRIPT_TIME_MODE == "clock":
        clock = str(seg.get("display_time") or "").strip()
        if clock:
            return clock

    start = seg.get("audio_start_time")
    if isinstance(start, (int, float)):
        return format_offset(start)

    return str(seg.get("display_time") or "").strip()


def parse_segments(data: dict) -> list[dict[str, str]]:
    """Parse transcripts.json with top-level 'segments' array."""
    result = []
    for seg in data.get("segments", []):
        text = seg.get("text", "").strip()
        if text:
            entry = {"time": segment_time(seg), "text": text}
            speaker = str(seg.get("speaker") or "").strip()
            if speaker:
                entry["speaker"] = speaker
            result.append(entry)
    return result


def parse_words(data: dict) -> list[dict[str, str]]:
    """Parse transcript.json with word-level timing data."""
    result: list[dict[str, str]] = []
    for transcript in data.get("transcripts", []):
        words = transcript.get("words", [])
        if not words:
            continue
        words_sorted = sorted(words, key=lambda w: w.get("start_ms", 0))
        sentences: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        prev_end: int | None = None
        prev_channel: int | None = None

        for word in words_sorted:
            start = word.get("start_ms") or 0
            end = word.get("end_ms") or 0
            channel = word.get("channel", 0)
            text = word.get("text", "").strip()
            if not text:
                continue
            gap = (start - prev_end) if prev_end is not None else 0
            changed = (channel != prev_channel) if prev_channel is not None else False
            if current and (gap > 1500 or changed):
                sentences.append({"channel": prev_channel, "start_ms": current[0]["start_ms"], "words": current})
                current = []
            current.append(word)
            prev_end, prev_channel = end, channel

        if current:
            sentences.append({"channel": prev_channel, "start_ms": current[0]["start_ms"], "words": current})

        for sent in sentences:
            text = "".join(w.get("text", "") for w in sent["words"]).strip()
            start_ms = sent["start_ms"]
            speaker = "You" if sent["channel"] == 0 else "Speaker"
            if text:
                result.append({
                    "time": f"{int(start_ms // 60000):02d}:{int((start_ms % 60000) // 1000):02d}",
                    "speaker": speaker,
                    "text": text,
                })

    result.sort(key=lambda x: x.get("time", ""))
    return result


def detect_and_parse(json_path: Path) -> tuple[str, list[dict[str, str]]]:
    """Auto-detect transcript format and parse it."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error(f"Invalid JSON in {json_path}: {e}")
        return "unknown", []
    except OSError as e:
        log.error(f"Cannot read {json_path}: {e}")
        return "unknown", []

    if "segments" in data:
        return "segments", parse_segments(data)
    if "transcripts" in data:
        return "words", parse_words(data)
    return "unknown", []
