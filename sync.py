"""Sync engine: state management, change detection, file synchronization."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    MEETILY_FOLDER, OBSIDIAN_FOLDER, COPY_AUDIO, NOTE_FORMAT_VERSION,
    STATE_FILE, LEGACY_CONVERTED_TRACKER, CACHE_TTL, log,
)
from utils import (sha256_text, sha256_file, atomic_write_text, copy2_if_changed,
                   sanitize_filename, meeting_uuid)
from db import MeetilyDB
from parsers import detect_and_parse
from markdown import (
    extract_date_time, read_metadata, read_existing_created,
    split_managed_block, build_markdown, parse_frontmatter,
    resolve_user_fields, merge_tags, unmanaged_frontmatter_lines, normalise,
    render_subfolder,
)
from enrich import (
    resolve_language, resolve_model, resolve_status, detect_participants,
)

# ── File discovery cache ──
_file_cache: set[Path] = set()
_cache_timestamp: float = 0.0


def get_transcript_files(use_cache: bool = False) -> list[Path]:
    """Find all transcript files. Single directory walk instead of two rglobs."""
    global _file_cache, _cache_timestamp

    now = time.time()
    if use_cache and _file_cache and (now - _cache_timestamp) < CACHE_TTL:
        return list(_file_cache)

    # Single walk — collect both filename variants at once
    names = {"transcripts.json", "transcript.json"}
    found = [p for p in MEETILY_FOLDER.rglob("transcript*.json") if p.name in names]

    if use_cache:
        _file_cache = set(found)
        _cache_timestamp = now

    return found


# ── State persistence ──

def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Failed to read state file: {e}")

    # One-time migration from legacy line-based tracker
    if LEGACY_CONVERTED_TRACKER.exists():
        try:
            lines = [p.strip() for p in LEGACY_CONVERTED_TRACKER.read_text(encoding="utf-8").splitlines() if p.strip()]
            if lines:
                migrated = {p: {"migrated_from_legacy": True} for p in lines}
                log.info(f"Migrated {len(migrated)} entries from legacy tracker")
                return migrated
        except OSError as e:
            log.warning(f"Failed to read legacy tracker: {e}")
    return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        atomic_write_text(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))
    except OSError as e:
        log.error(f"Failed to write state file: {e}")


# ── Title resolution ──

def resolve_meeting_title(
    folder: Path, folder_name: str, db: MeetilyDB
) -> tuple[str, dict[str, str] | None]:
    """Prefer edited Meetily title from DB, fall back to metadata / folder name."""
    record = db.get_meeting_record(str(folder))
    if record and record.get("title"):
        return record["title"], record

    metadata = read_metadata(folder)
    meeting_name = str(metadata.get("meeting_name", "")).strip()
    if meeting_name:
        return meeting_name.replace("_", " "), record

    date, time_raw = extract_date_time(folder_name)
    return f"Meeting {date} {time_raw}", record


# ── Change detection ──

def _metadata_mtime(folder: Path) -> float | None:
    """metadata.json is a second input: retranscribed_at lives there, and it is
    written *after* transcripts.json, so the transcript's mtime alone can miss a
    retranscribe that landed between two passes."""
    try:
        return (folder / "metadata.json").stat().st_mtime
    except OSError:
        return None



def _should_skip(
    json_path: Path,
    md_path: Path,
    entry: dict[str, Any] | None,
    db: MeetilyDB,
    *,
    check_db: bool,
) -> bool:
    """Return True if nothing changed and we can skip this file."""
    if not md_path.exists() or entry is None:
        return False

    # Note layout changed since this note was written — re-render it once.
    if entry.get("format_version") != NOTE_FORMAT_VERSION:
        return False

    try:
        st = json_path.stat()
    except OSError:
        return False

    if entry.get("mtime") != st.st_mtime or entry.get("size") != st.st_size:
        return False

    if entry.get("meta_mtime") != _metadata_mtime(json_path.parent):
        return False

    if not check_db:
        return True

    title, _ = resolve_meeting_title(json_path.parent, json_path.parent.name, db)
    if entry.get("meeting_title_sha") != sha256_text(title):
        return False

    summary = db.get_summary(str(json_path.parent)) or ""
    return entry.get("summary_sha") == sha256_text(summary)


# ── Recovering a note without sync state ──

_id_index: dict[str, Path] | None = None
_FRONTMATTER_PEEK = 4096


def note_id_index(refresh: bool = False) -> dict[str, Path]:
    """Map `meetily_id` to the note carrying it."""
    global _id_index
    if _id_index is not None and not refresh:
        return _id_index

    index: dict[str, Path] = {}
    if OBSIDIAN_FOLDER.exists():
        for note in OBSIDIAN_FOLDER.rglob("*.md"):
            if "audio" in note.relative_to(OBSIDIAN_FOLDER).parts[:-1]:
                continue
            try:
                with note.open(encoding="utf-8") as fh:
                    head = fh.read(_FRONTMATTER_PEEK)
            except OSError:
                continue
            note_id = parse_frontmatter(head).get("meetily_id")
            if isinstance(note_id, str) and note_id.strip():
                index.setdefault(note_id.strip(), note)
    _id_index = index
    return index


def _remember_note(note_id: str, md_path: Path) -> None:
    if note_id:
        note_id_index()[note_id] = md_path


# ── Rename helpers ──

def _resolve_note_paths(
    folder: Path, folder_name: str, entry: dict[str, Any] | None, db: MeetilyDB,
) -> tuple[str, Path, Path | None, dict[str, str] | None]:
    """Build target filenames; return previous path if title changed."""
    title, record = resolve_meeting_title(folder, folder_name, db)
    md_filename = f"{sanitize_filename(title)}.md"
    date, time_raw = extract_date_time(folder_name)
    subfolder = render_subfolder(date, time_raw, title)
    md_path = (OBSIDIAN_FOLDER / subfolder / md_filename) if subfolder else (OBSIDIAN_FOLDER / md_filename)

    prev: Path | None = None
    if entry and entry.get("md_path"):
        prev = Path(str(entry["md_path"]))
    elif record and record.get("id"):
        prev = note_id_index().get(meeting_uuid(record["id"]))
    if prev == md_path:
        prev = None

    return md_filename, md_path, prev, record


def _maybe_rename_note(prev: Path | None, md_path: Path) -> Path:
    """Move a note to its current title and folder, without ever clobbering."""
    if prev is None or prev == md_path or not prev.exists():
        return md_path
    if md_path.exists():
        log.warning(f"Target note already exists, keeping: {md_path.name}")
        return prev
    md_path.parent.mkdir(parents=True, exist_ok=True)
    prev.rename(md_path)
    if prev.parent == md_path.parent:
        log.info(f"Renamed note: {prev.name} → {md_path.name}")
    else:
        log.info(f"Moved note: {prev.name} → {md_path.relative_to(OBSIDIAN_FOLDER)}")
    return md_path


def _maybe_rename_audio(prev: Path | None, md_path: Path) -> None:
    if prev is None or prev == md_path:
        return
    audio_dir = OBSIDIAN_FOLDER / "audio"
    old = audio_dir / prev.with_suffix(".mp4").name
    new = audio_dir / md_path.with_suffix(".mp4").name
    if old.exists() and not new.exists():
        old.rename(new)
        log.info(f"Renamed audio: {old.name} → {new.name}")


# ── Core sync ──

SETTLE_SECONDS = 0.5


def filter_stable(files: list[Path], state: dict[str, Any]) -> list[Path]:
    """Drop transcripts that are still being written."""
    first: dict[Path, tuple[float, int]] = {}
    settled: set[Path] = set()

    for path in files:
        try:
            st = path.stat()
        except OSError as e:
            log.warning(f"Cannot access {path}: {e}")
            continue
        entry = state.get(str(path))
        if isinstance(entry, dict) and entry.get("mtime") == st.st_mtime and entry.get("size") == st.st_size:
            settled.add(path)
        else:
            first[path] = (st.st_mtime, st.st_size)

    if first:
        time.sleep(SETTLE_SECONDS)
        for path, before in first.items():
            try:
                st = path.stat()
            except OSError:
                log.warning(f"File disappeared: {path}")
                continue
            if (st.st_mtime, st.st_size) != before:
                log.info(f"Still writing: {path.name}")
                continue
            settled.add(path)

    return [p for p in files if p in settled]


def sync_file(
    json_path: Path, state: dict[str, Any], db: MeetilyDB,
    *, force: bool, check_db: bool,
) -> bool:
    """Sync a single transcript file to an Obsidian note. Returns True if synced."""
    path_key = str(json_path)
    entry = state.get(path_key) if isinstance(state.get(path_key), dict) else None

    try:
        # Skip permanently-empty / erroring files until the file itself changes
        try:
            st = json_path.stat()
            file_sig = (st.st_mtime, st.st_size)
        except OSError:
            file_sig = None

        if not force and entry and entry.get("skip_reason") and file_sig and \
                entry.get("format_version") == NOTE_FORMAT_VERSION and \
                entry.get("mtime") == file_sig[0] and entry.get("size") == file_sig[1]:
            return False

        fmt, segments = detect_and_parse(json_path)
        if not segments:
            log.warning(f"Empty: {json_path.parent.name}")
            if file_sig:
                state[path_key] = {
                    "skip_reason": "empty", "format_version": NOTE_FORMAT_VERSION,
                    "mtime": file_sig[0], "size": file_sig[1],
                }
            return False

        folder = json_path.parent
        folder_name = folder.name

        md_filename, md_path, prev, record = _resolve_note_paths(folder, folder_name, entry, db)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path = _maybe_rename_note(prev, md_path)
        _maybe_rename_audio(prev, md_path)

        if not force and _should_skip(json_path, md_path, entry, db, check_db=check_db):
            return False

        updated = datetime.now().strftime("%Y-%m-%d %H:%M")

        existing_text = None
        if md_path.exists():
            try:
                existing_text = md_path.read_text(encoding="utf-8")
            except OSError:
                pass
        existing_fm = parse_frontmatter(existing_text) if existing_text else {}

        existing_created = existing_fm.get("created")
        created = (
            (str(existing_created).strip() if isinstance(existing_created, str) else "")
            or (read_existing_created(prev) if prev else None)
            or updated
        )

        summary = db.get_summary(str(folder))
        summary_sha = sha256_text(summary or "")
        note_title = md_path.stem
        meeting_title_raw = (record or {}).get("title", note_title)
        meeting_title_sha = sha256_text(meeting_title_raw)

        # Read metadata once, pass to build_markdown (avoids double read)
        metadata = read_metadata(folder)

        auto_fields = {
            "language": resolve_language(metadata, segments),
            "transcription_model": resolve_model(metadata),
            "status": resolve_status(metadata),
            "participants": [],
        }
        note_id = meeting_uuid((record or {}).get("id", ""))
        fields = resolve_user_fields(auto_fields, existing_fm, (entry or {}).get("auto_fields") or {})
        fields["meetily_id"] = note_id
        fields["detected_participants"] = detect_participants(segments)
        fields["tags"] = merge_tags(existing_fm)

        keep_empty = frozenset(
            key for key in auto_fields
            if key in existing_fm and normalise(existing_fm[key]) == "" and normalise(fields[key]) == ""
        )

        md_text = build_markdown(
            folder, folder_name, fmt, segments, md_filename, metadata,
            note_title=meeting_title_raw, summary=summary,
            created=created, updated=updated, fields=fields,
            keep_empty=keep_empty,
            preserved_lines=unmanaged_frontmatter_lines(existing_text) if existing_text else [],
        )

        if existing_text:
            split = split_managed_block(existing_text)
            if split:
                _, _, suffix = split
                md_text = md_text.rstrip() + "\n\n" + suffix.lstrip()

        if existing_text != md_text:
            atomic_write_text(md_path, md_text)
        _remember_note(note_id, md_path)

        # Copy audio file
        audio_src = folder / "audio.mp4"
        if COPY_AUDIO and audio_src.exists():
            audio_dir = OBSIDIAN_FOLDER / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            audio_dst = audio_dir / md_filename.replace(".md", ".mp4")
            try:
                if copy2_if_changed(audio_src, audio_dst):
                    log.info(f"Audio → audio/{audio_dst.name}")
            except OSError as e:
                log.error(f"Failed to copy audio: {e}")

        # Update state
        try:
            st = json_path.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = None, None

        state[path_key] = {
            "meeting_id": (record or {}).get("id", ""),
            "meetily_id": note_id,
            "meeting_title": meeting_title_raw,
            "meeting_title_sha": meeting_title_sha,
            "md_filename": md_filename,
            "md_path": str(md_path),
            "meeting_folder": str(folder),
            "fmt": fmt,
            "segments_count": len(segments),
            "sha256": sha256_file(json_path),
            "summary_sha": summary_sha,
            "auto_fields": auto_fields,
            "format_version": NOTE_FORMAT_VERSION,
            "meta_mtime": _metadata_mtime(folder),
            "mtime": mtime,
            "size": size,
            "last_sync": updated,
        }

        action = "Updated" if existing_text else "Created"
        log.info(f"{action}: {md_filename} ({len(segments)} segments)")
        return True

    except OSError as e:
        log.error(f"File I/O error: {json_path} — {e}")
        _record_error(json_path, path_key, state, str(e))
    except Exception as e:
        log.error(f"Unexpected error: {json_path} — {e}")
        _record_error(json_path, path_key, state, str(e))
    return False


def _record_error(json_path: Path, path_key: str, state: dict[str, Any], reason: str) -> None:
    """Record a failed file in state so it's skipped until it changes on disk."""
    try:
        st = json_path.stat()
        state[path_key] = {
            "skip_reason": reason, "format_version": NOTE_FORMAT_VERSION,
            "mtime": st.st_mtime, "size": st.st_size,
        }
    except OSError:
        pass
