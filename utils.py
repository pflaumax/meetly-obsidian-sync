from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    """Write content to path atomically via a temp file rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        # Clean up orphaned tmp on any failure
        tmp.unlink(missing_ok=True)
        raise


def copy2_if_changed(src: Path, dst: Path) -> bool:
    """Copy src->dst only if dst is missing or differs by size/mtime. Returns True if copied."""
    try:
        if dst.exists():
            ss = src.stat()
            ds = dst.stat()
            if ss.st_size == ds.st_size and abs(ss.st_mtime - ds.st_mtime) < 0.001:
                return False
    except OSError:
        pass
    shutil.copy2(src, dst)
    return True


def meeting_uuid(meeting_id: str) -> str:
    """Meetily's `meeting-<uuid>` id, without the prefix, for frontmatter."""
    return str(meeting_id or "").removeprefix("meeting-").strip()


def sanitize_filename(name: str) -> str:
    """Keep filenames safe for Obsidian/macOS while preserving readable titles."""
    cleaned = re.sub(r'[\\/:*?"<>|]', " ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip(".") or "Untitled Meeting"
