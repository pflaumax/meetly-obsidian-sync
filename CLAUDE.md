# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Meetily Scribe — a single-purpose, dependency-free Python CLI that syncs Meetily meeting recordings (transcript JSON + audio + SQLite metadata) into Obsidian markdown notes. Tests are stdlib `unittest` in `test_meetily.py`; there is no linter config and no build step — it runs directly from source.

## Commands

```bash
python3 meetily_to_obsidian.py                    # one-shot sync of all transcripts
python3 meetily_to_obsidian.py --watch            # poll every 30s (the launchd mode)
python3 meetily_to_obsidian.py --resync           # ignore state, rewrite everything
python3 meetily_to_obsidian.py --watch --no-db-updates   # watch without SQLite polling
python3 meetily_to_obsidian.py --debug-db         # dump Meetily DB tables/columns/recent rows

python3 -m unittest test_meetily                  # 48 tests, stdlib only, ~20ms
python3 -m unittest test_meetily.EndToEnd -v      # one class
python3 -m unittest test_meetily.HandEditPrecedence.test_derived_value_wins_when_it_moves

tail -f ~/Library/Logs/meetily_converter.log      # logs (also where the launchd service writes)
launchctl kickstart -k gui/$(id -u)/com.example.meetily-obsidian   # restart service after code changes
rm ~/.meetily_sync_state.json                     # nuclear full re-sync
```

`pyproject.toml` declares `requires-python = ">=3.14"` while the README says 3.10+ and the local interpreter is 3.13 — the code itself only needs 3.10+ (PEP 604 unions, `from __future__ import annotations` everywhere). Don't "fix" the code to match the pin.

## Architecture

Layered, one-way dependency chain — `config` ← `utils`/`parsers` ← `db`/`markdown` ← `sync` ← `meetily_to_obsidian`.

- `config.py` — hardcoded user paths (`MEETILY_FOLDER`, `OBSIDIAN_FOLDER`, `MEETILY_DB`), tags, the managed-block markers, and root logging setup (rotating file handler at 5 MB × 3, falling back to stderr). Configuration is edited in this file; there is no CLI/env override.
- `parsers.py` — `detect_and_parse()` sniffs the JSON shape and returns `(fmt, segments)`. Two formats are handled: `"segments"` (Meetily's `transcripts.json`) and `"words"` (word-level timings grouped into sentences on a >1500 ms gap or channel change). **In practice every local transcript is `"segments"`** — `parse_words` and its You/Speaker labelling have never fired against real data. Unknown shapes return `("unknown", [])`, which `sync_file` treats as empty. `segment_time()` picks the timestamp: retranscribed files carry no `display_time` (Meetily replaces it with an ISO `timestamp` of when the retranscribe ran), so it falls back to `audio_start_time` as an offset.
- `enrich.py` — everything Meetily does not record: language, participants, transcription model, `baseline`/`enhanced` status. All of it is inferred; see "What Meetily does and does not store" below.
- `db.py` — `MeetilyDB` holds one SQLite connection for a whole sync cycle (callers must `close()` in a `finally`). Meetings are keyed by `meetings.folder_path` matching the transcript's parent directory string. Summaries live in `summary_processes.result` as JSON with a `markdown` key, only when `status = 'completed'`.
- `markdown.py` — note construction *and* frontmatter round-tripping. `parse_frontmatter()` is a deliberately small YAML subset (scalars, quoted scalars, inline and block lists) — enough for what we emit plus a hand edit, and it ignores anything more exotic rather than guessing. `resolve_user_fields()` implements the hand-edit precedence rule; `split_managed_block()` handles content after the END marker.
- `sync.py` — the actual engine (state, change detection, renames, writes).
- `meetily_to_obsidian.py` — argparse and the two loops (`run_once`, `watch_mode`).

### Invariants worth preserving

**The managed block is a contract.** Everything the tool generates lives between the BEGIN/END markers; anything the user wrote after END is read back and re-appended on every sync. Any change to note generation must keep `build_markdown` emitting both markers and `sync_file` re-appending the suffix.

**Hand edits to frontmatter survive, by a specific rule.** The user-editable fields are exactly the keys of the `auto_fields` dict in `sync_file` — `language`, `transcription_model`, `status`, `participants`. `sync_file` snapshots what it *derived* into `state[path]["auto_fields"]`; on the next pass `resolve_user_fields()` keeps the user's value for as long as the derived value is unchanged, and lets the derived value win when it has moved (`baseline` → `enhanced` after a retranscribe), because the transcript under the note was rewritten and an earlier `reviewed` is stale. `tags` is separate — `merge_tags()` unions config `TAGS` with whatever is already in the note, so config tags cannot be removed from a note. `detected_participants` is always derived and always overwritten. Keys the user blanked deliberately are listed in `keep_empty` so they are not dropped and re-derived on the next pass.

**Frontmatter keys outside `FIELD_ORDER` belong to the user.** `_render_frontmatter` only writes the keys it manages, then appends `unmanaged_frontmatter_lines(existing_text)` — the raw lines, verbatim. They are deliberately not re-emitted from the parsed value, because re-quoting would turn an Obsidian property like `publish: true` into the string `"true"`. Adding a key to `FIELD_ORDER` takes it over from the user.

**Both `transcripts.json` and `metadata.json` are inputs.** `status` and `transcription_model` derive from `retranscribed_at` in `metadata.json`, which Meetily writes *after* rewriting `transcripts.json`. `_should_skip` therefore compares `meta_mtime` as well as the transcript's mtime/size; without it a pass landing between the two writes would pin the note to `baseline` permanently.

**Note layout changes need a `NOTE_FORMAT_VERSION` bump.** `_should_skip()` returns False when a note's recorded version differs, so existing notes re-render once without `--resync`. The bump also releases `skip_reason` pins — a file that errored under older code is retried, which `_should_skip` alone cannot do because a pinned file returns from `sync_file` before reaching it. Without the bump, unchanged transcripts are skipped and the new layout never reaches old notes.

**Title is the filename is the identity.** The note name comes from `resolve_meeting_title()` (DB title → `metadata.json` `meeting_name` → `Meeting <date> <time>` from the folder name), sanitized. When a user renames a meeting in Meetily, the next sync renames both the `.md` and its `audio/<same-stem>.mp4` via `_maybe_rename_note` / `_maybe_rename_audio`, using `state[path].md_path` as the previous location. Renaming refuses to clobber an existing target.

**State file drives all skipping.** `~/.meetily_sync_state.json` maps absolute transcript path → entry with `mtime`/`size` (cheap change check), `meeting_title_sha` and `summary_sha` (so DB-side edits are detected without reparsing), plus `md_path` for renames. `_should_skip()` is the gate; `--resync` bypasses it. Failures and empty transcripts write a `skip_reason` entry pinned to the current mtime/size, so a broken file is retried only after it changes on disk. A legacy line-based `~/.meetily_converted.txt` is migrated once on first load.

**Sync is defensive by design.** `_is_file_stable()` double-stats with a 0.5 s pause to avoid reading a transcript mid-write (skipped when mtime/size are unchanged). Writes go through `atomic_write_text` (temp file + `replace`). Audio copies are skipped when size and mtime already match. Notes are only rewritten when the rendered text actually differs. The watch loop catches per-iteration exceptions and backs off 10 s rather than dying — it is expected to run unattended under launchd for weeks.

`_file_cache` in `sync.py` is a module-level 60 s TTL cache used only by watch mode (`use_cache=True`); `run_once` always does a fresh walk.

## What Meetily does and does not store

Verified against the local SQLite DB (156 meetings), 179 recording folders, and the Meetily source (`Zackriya-Solutions/meetily`). Re-check before building on any of it.

- **No transcription model or language is persisted per meeting.** `write_retranscription_metadata` (`frontend/src-tauri/src/audio/retranscription.rs`) writes only `retranscribed_at`, `status` and `transcript_file`; the model and language passed to a retranscribe reach a runtime event and are then dropped. `transcript_settings.model` is the *current global* setting, not what produced any given transcript. This is why `BASELINE_MODEL`/`ENHANCED_MODEL` live in config.
- **No speaker diarisation.** `transcripts.speaker` is NULL across all 53,682 rows. Participants cannot be derived from the data; the roster match in `enrich.py` is the only option.
- **`retranscribed_at` in `metadata.json` is the one reliable signal** distinguishing a retranscribed meeting, and it correlates exactly with the absence of `display_time` in `transcripts.json`.
- **`metadata.json` language fields**: `summary_language` (your override in Meetily) and `detected_summary_language` (Meetily's own detection); both absent on older meetings. The Cyrillic-share fallback in `enrich.py` agrees with `detected_summary_language` on all 46 meetings that have one; the two clusters sit at ~0.00 and ~0.95 with nothing between 0.41 and 0.63.
- **`confidence` in transcript segments is a hardcoded 0.85** in 139 of 140 files — useless as a quality signal. Don't build "which meetings need retranscribing" on it.
- **`summary_processes.result` has three shapes**: `{markdown}`, `{markdown, summary_json}` and `{english_cache: {markdown, output_language, source}, markdown}`. `english_cache` holds the English original of a translated summary, so it is a fallback only — preferring it would show English for a Ukrainian summary. An empty `markdown` with a populated `summary_json` means the summary really is empty, not that it was lost.
- `transcript_chunks.model` / `model_name` is the **LLM that wrote the summary** (`qwen3.5:4b`, `gemma3:1b`), not a Whisper model. `meeting_notes` exists but is empty here.

## Working on this safely

Run `python3 -m unittest test_meetily` before and after any change — it covers the frontmatter round-trip, hand-edit precedence, timestamp fallback, language thresholds, summary-source preference, batch settling and a full `sync_file` pass. Never point a trial run at the real vault — override `config.OBSIDIAN_FOLDER` and `config.STATE_FILE` at a scratch directory and set `COPY_AUDIO = False` (the real folder holds ~180 meetings of audio). Note that `sync.py` imports these by name, so patching `config` alone is not enough; rebind `sync.OBSIDIAN_FOLDER`, `sync.STATE_FILE` and `sync.COPY_AUDIO` too. A full cold pass over the real recordings takes ~2 s (it was ~90 s before `filter_stable` batched the settle sleep); a warm watch cycle is ~90 ms. Verify idempotency by running twice and diffing the notes — the second pass must write nothing.

Interrupted recordings are real and reach the code: `status: "recording"` with `duration_seconds: null` and `completed_at: null` (16 of 179 folders). Anything reading `metadata.json` must tolerate nulls.

`watch_mode` must count with `sum(1 for ...)` rather than `any(...)` — `any()` short-circuits on the first file that syncs, so a batch invalidated all at once (a `NOTE_FORMAT_VERSION` bump does exactly that) would trickle through at one note per 30 s pass.
