# Meetily Scribe

Turns [Meetily](https://meetily.ai) meeting recordings into Obsidian notes —
frontmatter, transcript, summary and playable audio — and keeps them yours.

Everything the tool writes lives inside a managed block. Anything you add
outside it, and any frontmatter field you edit by hand, survives every re-sync.
It also knows the difference between a live recording and one you re-ran through
a bigger Whisper model, and says so in the note.

Runs locally, no cloud, no dependencies beyond the Python standard library.

## Features

- Frontmatter with tags, date, duration, microphone, language, transcription model and status
- `# Meeting: <title>` heading, summary from Meetily's SQLite database
- Audio embedded as `![[audio/file.mp4]]` (playable in Obsidian)
- Managed content block — your notes after `<!-- MEETILY:END -->` are preserved across re-syncs
- Hand-edited frontmatter fields are preserved too (see [Editing notes by hand](#editing-notes-by-hand))
- Detects `baseline` vs `enhanced` meetings from Meetily's retranscription marker
- Language detection, and participant suggestions from a roster you define
- Incremental sync — only processes changed files
- Watch mode for continuous monitoring
- Automatic note renaming when meeting title changes in Meetily

## Setup

Copy the template and edit the three paths:

```bash
cp config.example.py config.py
```

```python
MEETILY_FOLDER  = Path("/path/to/meetily-recordings")
OBSIDIAN_FOLDER = Path("/path/to/obsidian-vault/meetings")
MEETILY_DB      = Path("/path/to/meeting_minutes.sqlite")
```

Optionally fill in the roster used to suggest participants, and the Whisper
models you actually use:

```python
KNOWN_PEOPLE = {
    # Whisper declines names, so list the forms you actually see:
    "Taras": ["Taras", "Тарас", "Тараса", "Тарасу", "Тарасом"],
    "Ada":   ["Ada", "Ada Lovelace"],
}
OWNER_NAME = "Your Name"

BASELINE_MODEL = "whisper-large-v3-turbo"   # live recording
ENHANCED_MODEL = "whisper-large-v3"         # after Transcript → Retranscribe
```

## Usage

```bash
# Sync all existing meetings
python3 meetily_to_obsidian.py

# Watch for new recordings every 30s
python3 meetily_to_obsidian.py --watch

# Force re-sync everything
python3 meetily_to_obsidian.py --resync

# Skip DB polling in watch mode
python3 meetily_to_obsidian.py --watch --no-db-updates

# Debug: inspect Meetily database structure
python3 meetily_to_obsidian.py --debug-db
```

## Project Structure

```
config.py                 # Paths, constants, logging setup
utils.py                  # Hashing, atomic writes, file helpers
db.py                     # SQLite access (shared connection per cycle)
parsers.py                # Transcript JSON parsing
enrich.py                 # Derived metadata: language, participants, model, status
markdown.py               # Obsidian note generation and frontmatter round-tripping
sync.py                   # State management, change detection, sync engine
meetily_to_obsidian.py    # CLI entry point
```

## Where notes go

`NOTE_SUBFOLDER_PATTERN` in `config.py` places notes under `OBSIDIAN_FOLDER`:

```
meetings/
├─ 2026/
│  ├─ 07/  Meeting notes for July
│  └─ 08/  …
└─ audio/  every .mp4, flat
```

Tokens: `{year}` `{month}` `{day}` `{quarter}` `{date}` `{time}` `{title}`.
Blank for one flat folder. Changing it moves existing notes on the next sync.

## Note format

```yaml
---
title: "Weekly planning"
date: 2026-06-09
time: "12:43:31"
tags:
  - meeting
  - teams
  - transcript
source: "Meetily"
language: "ukrainian"
transcription_model: "whisper-large-v3"
status: "enhanced"
participants: []
detected_participants: [Ada, Taras]
duration: "42m 18s"
microphone: "Built-in Microphone"
created: "2026-06-09 13:30"
updated: "2026-08-21 22:15"
---
```

`status` and `transcription_model` follow Meetily's two transcription paths:

| Path | status | transcription_model |
|---|---|---|
| Live recording | `baseline` | `BASELINE_MODEL` |
| Transcript → Retranscribe has run | `enhanced` | `ENHANCED_MODEL` |

Meetily records *that* a meeting was retranscribed (`retranscribed_at` in the
folder's `metadata.json`) but never *which* Whisper model ran — the model is
handed to the retranscription task and then dropped. So the model name comes
from config, not from Meetily. Correct any single note by editing it.

`language` comes from Meetily's own detection when it has one
(`summary_language`, then `detected_summary_language` in `metadata.json`), and
otherwise from the share of Cyrillic letters in the transcript. Transcripts
shorter than ~200 letters get no language at all rather than a guess.

`detected_participants` is only a suggestion — it lists roster names that come
up in the transcript, which includes people merely talked about. There is no
speaker diarisation in Meetily (its `transcripts.speaker` column is always
NULL), so `participants` is yours to fill in.

## Editing notes by hand

You can edit `language`, `transcription_model`, `status` and `participants`
directly in a note, plus add your own `tags`. Your value is kept for as long as
the value the syncer derives stays put.

When the derived value itself moves — `baseline` → `enhanced`, once you run
Retranscribe — it wins back, and a `status: reviewed` you set beforehand is
replaced. That is deliberate: the transcript under the note has been rewritten,
so the earlier review no longer describes what is in the note. Mark it
`reviewed` again once you have read the new version.

Any other frontmatter key you add — `cssclasses`, `aliases`, `publish`, whatever
Obsidian or your plugins use — is kept exactly as you wrote it. Only the keys
listed in the table above are managed.

Everything after `<!-- MEETILY:END -->` is yours and is never touched.

## Auto-Start with macOS (launchd)

To keep the sync running in the background — including after reboots:

**1. Create the plist:**

```bash
cat > ~/Library/LaunchAgents/com.example.meetily-obsidian.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.meetily-obsidian</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env python3</string>
        <string>/ABSOLUTE/PATH/TO/meetily_to_obsidian/meetily_to_obsidian.py</string>
        <string>--watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOUR_USERNAME/Library/Logs/meetily_converter.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USERNAME/Library/Logs/meetily_converter.log</string>
</dict>
</plist>
EOF
```

**2. Load the service:**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.meetily-obsidian.plist
```

**3. Useful commands:**

```bash
# Check status
launchctl print gui/$(id -u)/com.example.meetily-obsidian

# Restart (e.g. after code changes)
launchctl kickstart -k gui/$(id -u)/com.example.meetily-obsidian

# Stop and unload
launchctl bootout gui/$(id -u)/com.example.meetily-obsidian

# View logs
tail -f ~/Library/Logs/meetily_converter.log
```

The service starts automatically on login (`RunAtLoad`) and restarts if it crashes (`KeepAlive`).

## Sync State

State is stored in `~/.meetily_sync_state.json`. Delete this file to force a full re-sync on next run.

The state file also records the note layout version. When that version is bumped
in `config.py`, every note re-renders once on the next sync without needing
`--resync`.

Logs are written to `~/Library/Logs/meetily_converter.log` and stdout.

## Tests

```bash
python3 -m unittest test_meetily
```

Stdlib `unittest`, no dependencies.

## Requirements

Python 3.10+ (no external dependencies).
