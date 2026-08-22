from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config import (TAGS, MANAGED_BEGIN, MANAGED_END, TRANSCRIPT_LAYOUT,
                    SPEAKER_LABEL, NOTE_SUBFOLDER_PATTERN)

# Order frontmatter keys appear in. Anything not listed is dropped on rewrite.
FIELD_ORDER = (
    "title", "date", "time", "tags", "source", "meetily_id", "language",
    "transcription_model", "status", "participants", "detected_participants",
    "duration", "microphone", "created", "updated",
)
_LIST_FIELDS = frozenset({"tags", "participants", "detected_participants"})
# Emitted even when empty, so there is an obvious slot to fill in by hand.
_ALWAYS_EMIT = frozenset({"title", "date", "time", "tags", "source",
                          "participants", "created", "updated"})


def extract_date_time(folder_name: str) -> tuple[str, str]:
    """Extract date and time components from a meeting folder name."""
    parts = folder_name.replace("Meeting ", "").split("_")
    date = "Unknown-date"
    time_raw = "00-00-00"
    for part in parts:
        if len(part) == 10 and part.count("-") == 2:
            date = part
        elif len(part) == 8 and part.count("-") == 2:
            time_raw = part
    return date, time_raw


def render_subfolder(date: str, time_raw: str, title: str) -> str:
    """Render NOTE_SUBFOLDER_PATTERN into a relative path. Blank pattern = flat."""
    if not NOTE_SUBFOLDER_PATTERN.strip():
        return ""

    year, month, day = (date.split("-") + ["", "", ""])[:3]
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return ""
    quarter = f"Q{(int(month) - 1) // 3 + 1}" if 1 <= int(month) <= 12 else ""
    from utils import sanitize_filename
    tokens = {
        "year": year, "month": month, "day": day, "quarter": quarter,
        "date": date, "time": time_raw, "title": sanitize_filename(title),
    }
    rendered = re.sub(
        r"\{(\w+)\}",
        lambda m: tokens.get(m.group(1)) or m.group(0),
        NOTE_SUBFOLDER_PATTERN,
    )
    parts = [sanitize_filename(p) for p in rendered.split("/") if p.strip()]
    return "/".join(p for p in parts if p)


def format_duration(seconds: float | None) -> str:
    """Interrupted recordings leave duration_seconds null, so coerce it."""
    if not isinstance(seconds, (int, float)):
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def read_metadata(folder: Path) -> dict[str, Any]:
    """Read metadata.json from a meeting folder."""
    meta_path = folder / "metadata.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# ── Frontmatter ──

def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\").strip()


def _split_inline_list(inner: str) -> list[str]:
    """Split `a, "b, c", d` on commas without cutting inside quotes."""
    items: list[str] = []
    buf: list[str] = []
    quote = ""
    escaped = False
    for ch in inner:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            buf.append(ch)
            escaped = True
        elif quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    items.append("".join(buf))
    return [_unquote(i) for i in items if i.strip()]


def _frontmatter_bounds(text: str) -> tuple[int, int] | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    return text.find("\n") + 1, end


def unmanaged_frontmatter_lines(text: str) -> list[str]:
    """Raw lines for frontmatter keys this module does not manage.

    Kept verbatim rather than re-emitted from the parsed value: re-quoting would
    turn an Obsidian property like `publish: true` into the string `"true"`.
    """
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return []
    blocks: list[tuple[str, list[str]]] = []
    for line in text[bounds[0]:bounds[1]].splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):", line)
        if match:
            blocks.append((match.group(1), [line]))
        elif blocks:
            blocks[-1][1].append(line)
    return [line for key, lines in blocks if key not in FIELD_ORDER for line in lines]


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse a note's frontmatter into a dict."""
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return {}
    body = text[bounds[0]:bounds[1]]

    fields: dict[str, Any] = {}
    key: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("- ") and key is not None:
            current = fields.get(key)
            if not isinstance(current, list):
                if current:          # a real scalar, not a block-list header
                    continue
                fields[key] = current = []
            current.append(_unquote(stripped[2:]))
            continue

        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if not value:
            fields[key] = ""                      # a block list may follow
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fields[key] = _split_inline_list(inner) if inner else []
        else:
            fields[key] = _unquote(value)
    return fields


def read_existing_created(md_path: Path) -> str | None:
    """Extract the 'created' timestamp from an existing note's frontmatter."""
    if not md_path.exists():
        return None
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    created = parse_frontmatter(text).get("created")
    return str(created).strip() or None if isinstance(created, str) else None


def normalise(value: Any) -> Any:
    """Compare frontmatter values without tripping over case, spacing or empties."""
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value if str(v).strip()] or ""
    return str(value or "").strip().lower()


def resolve_user_fields(
    auto: dict[str, Any],
    existing: dict[str, Any],
    previous_auto: dict[str, Any],
) -> dict[str, Any]:
    """Merge derived values with hand edits."""
    resolved: dict[str, Any] = {}
    for key, auto_value in auto.items():
        current = existing.get(key)
        previous = previous_auto.get(key)
        if key not in existing:
            resolved[key] = auto_value
        elif previous is not None and normalise(previous) != normalise(auto_value):
            resolved[key] = auto_value
        elif normalise(current) != normalise(auto_value):
            resolved[key] = current
        else:
            resolved[key] = auto_value
    return resolved


def merge_tags(existing: dict[str, Any]) -> list[str]:
    """config TAGS plus whatever you have added to the note yourself."""
    current = existing.get("tags")
    if isinstance(current, str):
        current = [t.strip() for t in current.split(",")]
    merged = list(TAGS)
    for tag in current or []:
        tag = str(tag).strip()
        if tag and tag not in merged:
            merged.append(tag)
    return merged


def _yaml_scalar(value: str) -> str:
    """Double-quoted YAML, where backslash is itself an escape character."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def _yaml_list(values: list[str]) -> str:
    parts = []
    for value in values:
        value = str(value).strip()
        parts.append(_yaml_scalar(value) if re.search(r"""[,\[\]:"']""", value) else value)
    return "[" + ", ".join(parts) + "]"


def _render_frontmatter(
    fields: dict[str, Any],
    *,
    keep_empty: frozenset[str] = frozenset(),
    tail_lines: list[str] | None = None,
) -> list[str]:
    """Managed keys in FIELD_ORDER, then any hand-added keys exactly as found."""
    always_emit = _ALWAYS_EMIT | keep_empty
    lines = ["---"]
    for key in FIELD_ORDER:
        if key not in fields:
            continue
        value = fields[key]
        if key in _LIST_FIELDS:
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            value = [str(v).strip() for v in (value or []) if str(v).strip()]
            if not value and key not in always_emit:
                continue
            if key == "tags":       # block style, as Obsidian notes conventionally use
                lines.append("tags:")
                lines.extend(f"  - {t}" for t in value)
            else:
                lines.append(f"{key}: {_yaml_list(value)}")
            continue

        text = str(value or "").strip()
        if not text and key not in always_emit:
            continue
        lines.append(f"{key}: {_yaml_scalar(text)}" if key != "date" else f"date: {text}")
    lines.extend(tail_lines or [])
    lines.append("---")
    return lines


# ── Managed block ──

def split_managed_block(existing: str) -> tuple[str, str, str] | None:
    """Return (prefix, managed_block, suffix) if markers exist, else None."""
    begin = existing.find(MANAGED_BEGIN)
    end = existing.find(MANAGED_END)
    if begin == -1 or end == -1 or end < begin:
        return None
    end += len(MANAGED_END)
    return existing[:begin].rstrip() + "\n", existing[begin:end].strip() + "\n", existing[end:].lstrip()


def demote_headings(md: str) -> str:
    """Push a summary's own H1s down one level so the note keeps a single H1."""
    out: list[str] = []
    in_fence = False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"^#\s+", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def render_speaker_blocks(segments: list[dict[str, str]]) -> list[str]:
    """Speaker headings, opened only when the speaker actually changes."""
    lines: list[str] = []
    last_speaker: str | None = None
    for seg in segments:
        speaker = str(seg.get("speaker") or "").strip() or SPEAKER_LABEL
        if speaker != last_speaker:
            stamp = f" ({seg['time']})" if seg.get("time") else ""
            lines += ["", f"### {speaker}{stamp}", ""]
            last_speaker = speaker
        t = f"[{seg['time']}]" if seg.get("time") else ""
        lines.append(f"{t} {seg['text']}  ")
    return lines


def build_markdown(
    folder: Path,
    folder_name: str,
    fmt: str,
    segments: list[dict[str, str]],
    md_filename: str,
    metadata: dict[str, Any],
    *,
    note_title: str,
    summary: str | None,
    created: str,
    updated: str,
    fields: dict[str, Any],
    keep_empty: frozenset[str] = frozenset(),
    preserved_lines: list[str] | None = None,
) -> str:
    """Build the full markdown note content (frontmatter + managed block)."""
    date, time_raw = extract_date_time(folder_name)

    frontmatter = {
        "title": note_title,
        "date": date,
        "time": time_raw.replace("-", ":"),
        "tags": fields.get("tags", list(TAGS)),
        "source": "Meetily",
        "meetily_id": fields.get("meetily_id", ""),
        "language": fields.get("language", ""),
        "transcription_model": fields.get("transcription_model", ""),
        "status": fields.get("status", ""),
        "participants": fields.get("participants", []),
        "detected_participants": fields.get("detected_participants", []),
        "duration": format_duration(metadata.get("duration_seconds")) if metadata else "",
        "microphone": (metadata.get("devices") or {}).get("microphone", "") if metadata else "",
        "created": created,
        "updated": updated,
    }

    lines: list[str] = [MANAGED_BEGIN, "", f"# Meeting: {note_title}", ""]

    audio_path = folder / "audio.mp4"
    if audio_path.exists():
        audio_file = md_filename.replace(".md", ".mp4")
        lines += ["## Audio", "", f"![[audio/{audio_file}]]", ""]

    if summary:
        lines += ["## Summary", "", demote_headings(summary), ""]

    lines += ["---", "", "## Transcript", ""]

    if not segments:
        lines.append("_Transcript is empty._")
    elif fmt == "words" or TRANSCRIPT_LAYOUT == "blocks":
        lines += render_speaker_blocks(segments)
    else:
        for seg in segments:
            t = f"[{seg['time']}]" if seg.get("time") else ""
            lines.append(f"{t} {seg['text']}  ")

    lines += ["", "---", f"_Meetily • synced {updated}_", "", MANAGED_END]

    rendered = _render_frontmatter(frontmatter, keep_empty=keep_empty, tail_lines=preserved_lines)
    return "\n".join(rendered) + "\n\n" + "\n".join(lines) + "\n"
