"""SQLite access for Meetily database: meeting records and summaries.

Uses a shared connection per sync cycle to avoid repeated open/close overhead.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from config import MEETILY_DB, SUMMARY_PREFER_ENGLISH, log


class MeetilyDB:
    """Lightweight wrapper that reuses a single SQLite connection per cycle."""

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection | None:
        if not MEETILY_DB.exists():
            return None
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(str(MEETILY_DB), timeout=5.0)
            except sqlite3.Error as e:
                log.warning(f"Cannot open DB: {e}")
                return None
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def get_meeting_record(self, folder_path: str) -> dict[str, str] | None:
        """Read meeting metadata from Meetily DB, including edited title."""
        conn = self._get_conn()
        if conn is None:
            return None
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, title, created_at, updated_at "
                "FROM meetings WHERE folder_path = ? LIMIT 1",
                (folder_path,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0] or "",
                "title": (row[1] or "").strip(),
                "created_at": row[2] or "",
                "updated_at": row[3] or "",
            }
        except sqlite3.Error as e:
            log.warning(f"Meeting DB read error: {e}")
        return None

    def get_summary(self, folder_path: str) -> str | None:
        """Read completed summary markdown for a meeting."""
        conn = self._get_conn()
        if conn is None:
            return None
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT sp.result FROM summary_processes sp "
                "JOIN meetings m ON m.id = sp.meeting_id "
                "WHERE m.folder_path = ? AND sp.status = 'completed' "
                "AND sp.result IS NOT NULL LIMIT 1",
                (folder_path,),
            )
            row = cur.fetchone()
            if row and row[0]:
                payload = json.loads(row[0])
                if not isinstance(payload, dict):
                    return None
                # A populated english_cache means `markdown` is a translation and
                # the cache holds the English it was translated from.
                cache = payload.get("english_cache")
                cached_english = (cache.get("markdown") or "").strip() if isinstance(cache, dict) else ""
                as_generated = (payload.get("markdown") or "").strip()
                md = (cached_english or as_generated) if SUMMARY_PREFER_ENGLISH \
                    else (as_generated or cached_english)
                return md if md else None
        except (sqlite3.Error, json.JSONDecodeError, KeyError) as e:
            log.warning(f"Summary DB read error: {e}")
        return None

    def debug_dump(self) -> None:
        """Print DB structure and recent rows for debugging."""
        if not MEETILY_DB.exists():
            print(f"DB not found: {MEETILY_DB}")
            return

        size_kb = MEETILY_DB.stat().st_size / 1024
        print(f"DB found: {MEETILY_DB}")
        print(f"Size: {round(size_kb, 1)} KB\n")

        try:
            with closing(sqlite3.connect(str(MEETILY_DB), timeout=5.0)) as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [r[0] for r in cur.fetchall()]
                print(f"Tables: {tables}\n")

                for table in tables:
                    # Safe identifier quoting via double-quote escaping
                    safe = table.replace('"', '""')
                    cur.execute(f'PRAGMA table_info("{safe}")')
                    cols = [r[1] for r in cur.fetchall()]
                    print(f"Table [{table}]: {cols}")
                    try:
                        cur.execute(f'SELECT * FROM "{safe}" ORDER BY rowid DESC LIMIT 2')
                        for i, row in enumerate(cur.fetchall()):
                            print(f"  Row {i + 1}: {str(row)[:300]}")
                    except Exception as e:
                        print(f"  Cannot read: {e}")
                    print()
        except Exception as e:
            print(f"Error: {e}")
