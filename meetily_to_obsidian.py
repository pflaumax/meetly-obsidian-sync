#!/usr/bin/env python3
"""Meetily → Obsidian Auto-Converter v4"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path as _Path

if not (_Path(__file__).resolve().parent / "config.py").exists():
    raise SystemExit(
        "config.py not found.\n"
        "Copy the template and edit the three paths at the top:\n"
        "    cp config.example.py config.py"
    )

from config import MEETILY_FOLDER, OBSIDIAN_FOLDER, TAGS, CHECK_DB_UPDATES_IN_WATCH, log
from db import MeetilyDB
from sync import get_transcript_files, load_state, save_state, sync_file, filter_stable


def run_once(*, force: bool) -> None:
    state = load_state()
    files = filter_stable(get_transcript_files(use_cache=False), state)

    if not files:
        log.info("No transcripts found.")
        return

    log.info(f"Found {len(files)} file(s)...")
    db = MeetilyDB()
    try:
        synced = sum(1 for f in files if sync_file(f, state, db, force=force, check_db=True))
    finally:
        db.close()

    save_state(state)
    log.info(f"Done → {OBSIDIAN_FOLDER} ({synced} synced)")


def watch_mode(*, force: bool, check_db: bool) -> None:
    log.info(f"Watching : {MEETILY_FOLDER}")
    log.info(f"Output   : {OBSIDIAN_FOLDER}")
    log.info(f"Tags     : {TAGS}")
    log.info("Checking every 30s. Ctrl+C to stop.\n")

    state = load_state()
    db = MeetilyDB()

    try:
        while True:
            try:
                files = filter_stable(get_transcript_files(use_cache=True), state)
                changed = sum(1 for f in files if sync_file(f, state, db, force=force, check_db=check_db))
                if changed:
                    save_state(state)
                time.sleep(30)
            except KeyboardInterrupt:
                save_state(state)
                log.info("Stopped.")
                break
            except OSError as e:
                log.error(f"File system error: {e}")
                time.sleep(10)
            except Exception as e:
                log.error(f"Loop error: {e}")
                time.sleep(10)
    finally:
        db.close()


def main() -> None:
    if not MEETILY_FOLDER.exists():
        log.error(f"Meetily folder not found: {MEETILY_FOLDER}")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Sync Meetily recordings into Obsidian.")
    parser.add_argument("--watch", action="store_true", help="Watch for changes and sync continuously.")
    parser.add_argument("--resync", action="store_true", help="Force re-sync even if nothing looks changed.")
    parser.add_argument("--no-db-updates", action="store_true", help="In watch mode: skip SQLite summary polling.")
    parser.add_argument("--debug-db", action="store_true", help="Print DB structure and recent rows.")
    args = parser.parse_args()

    if args.debug_db:
        MeetilyDB().debug_dump()
        sys.exit(0)

    if args.watch:
        watch_mode(force=args.resync, check_db=(CHECK_DB_UPDATES_IN_WATCH and not args.no_db_updates))
    else:
        run_once(force=args.resync)


if __name__ == "__main__":
    main()
