from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import config
import enrich
import markdown as md
import parsers
import sync
import utils


class Frontmatter(unittest.TestCase):
    def test_missing_or_unterminated_is_empty(self):
        self.assertEqual(md.parse_frontmatter("just text\n"), {})
        self.assertEqual(md.parse_frontmatter("---\ntitle: X\nno closing"), {})

    def test_scalars_lists_and_comments(self):
        fm = md.parse_frontmatter(
            '---\n# a comment\ntitle: "X"\ntime: "13:00:19"\n'
            "tags:\n  - meeting\n  - custom\nparticipants: [Sam, Ada]\nblank:\n---\nbody"
        )
        self.assertEqual(fm["title"], "X")
        self.assertEqual(fm["time"], "13:00:19")          # colons inside values survive
        self.assertEqual(fm["tags"], ["meeting", "custom"])
        self.assertEqual(fm["participants"], ["Sam", "Ada"])
        self.assertEqual(fm["blank"], "")

    def test_nested_keys_are_ignored_not_guessed(self):
        fm = md.parse_frontmatter("---\ndevices:\n  microphone: X\n---\n")
        self.assertEqual(fm["devices"], "")
        self.assertNotIn("microphone", fm)

    def test_stray_list_item_does_not_attach_to_a_scalar(self):
        self.assertEqual(md.parse_frontmatter("---\nstatus: reviewed\n- bogus\n---\n")["status"], "reviewed")

    def test_quoted_comma_is_not_a_separator(self):
        self.assertEqual(md._split_inline_list('Sam, "Doe, John", Маша'), ["Sam", "Doe, John", "Маша"])

    def test_yaml_scalar_roundtrip_escapes(self):
        for value in [r"C:\Users\alex", "ends with a backslash\\", 'has " quote', "звичайний", "a: b"]:
            with self.subTest(value=value):
                self.assertEqual(md._unquote(md._yaml_scalar(value)), value)

    def test_render_parse_roundtrip(self):
        src = {
            "title": 'Q3 "Plan": review', "date": "2026-08-21", "time": "13:00:19",
            "tags": ["meeting", "custom"], "source": "Meetily", "meetily_id": "abc-123",
            "language": "ukrainian", "transcription_model": "whisper-large-v3",
            "status": "reviewed", "participants": ["Sam", "Doe, John"],
            "detected_participants": [], "duration": "", "microphone": "Mic",
            "created": "2026-01-01 09:00", "updated": "2026-08-21 22:00",
        }
        back = md.parse_frontmatter("\n".join(md._render_frontmatter(src)) + "\n")
        for key in ("title", "time", "tags", "participants", "status", "created", "meetily_id"):
            self.assertEqual(back[key], src[key], key)
        self.assertNotIn("duration", back)                 # empty optional keys are dropped
        self.assertNotIn("detected_participants", back)

    def test_unmanaged_keys_are_preserved_verbatim(self):
        note = ('---\ntitle: "X"\ncssclasses: wide\npublish: true\n'
                "aliases:\n  - WS Jan 2\n---\nbody")
        tail = md.unmanaged_frontmatter_lines(note)
        self.assertEqual(tail, ["cssclasses: wide", "publish: true", "aliases:", "  - WS Jan 2"])
        out = "\n".join(md._render_frontmatter({"title": "X"}, tail_lines=tail)) + "\n"
        # `publish: true` must stay a boolean, not become the string "true"
        self.assertIn("publish: true", out)
        self.assertEqual(md.parse_frontmatter(out)["aliases"], ["WS Jan 2"])

    def test_managed_keys_are_not_duplicated_into_the_tail(self):
        self.assertEqual(md.unmanaged_frontmatter_lines('---\ntitle: "X"\nstatus: reviewed\n---\n'), [])

    def test_blanked_field_can_be_kept(self):
        kept = "\n".join(md._render_frontmatter({"language": ""}, keep_empty=frozenset({"language"})))
        self.assertIn('language: ""', kept)
        self.assertNotIn("language", "\n".join(md._render_frontmatter({"language": ""})))


class HandEditPrecedence(unittest.TestCase):
    """Your edit wins while the derived value holds still; when it moves, it wins back."""

    def test_absent_field_takes_the_derived_value(self):
        self.assertEqual(md.resolve_user_fields({"s": "baseline"}, {}, {}), {"s": "baseline"})

    def test_edit_is_kept_while_derived_value_is_unchanged(self):
        self.assertEqual(
            md.resolve_user_fields({"s": "baseline"}, {"s": "reviewed"}, {"s": "baseline"}),
            {"s": "reviewed"})

    def test_derived_value_wins_when_it_moves(self):
        self.assertEqual(
            md.resolve_user_fields({"s": "enhanced"}, {"s": "reviewed"}, {"s": "baseline"}),
            {"s": "enhanced"})

    def test_without_prior_state_the_note_is_trusted(self):
        self.assertEqual(md.resolve_user_fields({"s": "baseline"}, {"s": "reviewed"}, {}), {"s": "reviewed"})

    def test_case_and_spacing_are_not_edits(self):
        self.assertEqual(
            md.resolve_user_fields({"s": "Baseline"}, {"s": " baseline "}, {"s": "Baseline"}),
            {"s": "Baseline"})

    def test_empty_list_and_empty_string_compare_equal(self):
        self.assertEqual(md.normalise([]), md.normalise(""))
        self.assertEqual(md.resolve_user_fields({"p": []}, {"p": ""}, {"p": []}), {"p": []})

    def test_tags_are_unioned_never_replaced(self):
        self.assertEqual(md.merge_tags({}), list(config.TAGS))
        self.assertEqual(md.merge_tags({"tags": ["mine"]}), list(config.TAGS) + ["mine"])
        self.assertEqual(md.merge_tags({"tags": "a, b"}), list(config.TAGS) + ["a", "b"])


class Rendering(unittest.TestCase):
    def test_summary_h1_is_demoted_but_not_inside_a_fence(self):
        self.assertEqual(md.demote_headings("# Title\ntext"), "## Title\ntext")
        self.assertEqual(md.demote_headings("```\n# x\n```"), "```\n# x\n```")

    def test_null_duration_from_an_interrupted_recording(self):
        self.assertEqual(md.format_duration(None), "")
        self.assertEqual(md.format_duration("nonsense"), "")
        self.assertEqual(md.format_duration(603.5), "10m 3s")

    def test_date_time_from_folder_name(self):
        self.assertEqual(md.extract_date_time("Meeting 2026-08-21_16-33-27_2026-08-21_13-33"),
                         ("2026-08-21", "16-33-27"))
        self.assertEqual(md.extract_date_time("nonsense"), ("Unknown-date", "00-00-00"))

    def test_managed_block_split(self):
        self.assertIsNone(md.split_managed_block("no markers"))
        text = f"pre\n{config.MANAGED_BEGIN}\nbody\n{config.MANAGED_END}\nmine\n"
        prefix, block, suffix = md.split_managed_block(text)
        self.assertEqual(suffix.strip(), "mine")
        self.assertIn("body", block)

    def test_speaker_blocks_open_only_on_a_change(self):
        segs = [{"time": "00:00", "text": "a"}, {"time": "00:05", "text": "b"},
                {"time": "00:09", "text": "c", "speaker": "Ada"}]
        out = "\n".join(md.render_speaker_blocks(segs))
        # No speaker data means one heading for the whole meeting, not one per segment.
        self.assertEqual(out.count(f"### {config.SPEAKER_LABEL}"), 1)
        self.assertEqual(out.count("### Ada"), 1)


class Subfolders(unittest.TestCase):
    def setUp(self):
        self._saved = config.NOTE_SUBFOLDER_PATTERN
    def tearDown(self):
        self._set(self._saved)

    @staticmethod
    def _set(pattern):
        import importlib
        config.NOTE_SUBFOLDER_PATTERN = pattern
        importlib.reload(md)

    def test_tokens(self):
        self._set("{year}/{month}")
        self.assertEqual(md.render_subfolder("2026-08-21", "16-33-27", "T"), "2026/08")
        self._set("{year}/{quarter}")
        self.assertEqual(md.render_subfolder("2026-08-21", "16-33-27", "T"), "2026/Q3")
        self._set("{year}/{month}/{day}")
        self.assertEqual(md.render_subfolder("2026-08-21", "16-33-27", "T"), "2026/08/21")

    def test_blank_pattern_means_flat(self):
        self._set("")
        self.assertEqual(md.render_subfolder("2026-08-21", "16-33-27", "T"), "")

    def test_unknown_token_is_left_visible(self):
        self._set("{year}/{nope}")
        self.assertEqual(md.render_subfolder("2026-08-21", "16-33-27", "T"), "2026/{nope}")

    def test_unreadable_date_stays_flat_rather_than_inventing_folders(self):
        self._set("{year}/{month}")
        self.assertEqual(md.render_subfolder("Unknown-date", "00-00-00", "T"), "")

    def test_title_token_is_filename_safe(self):
        self._set("{title}")
        self.assertEqual(md.render_subfolder("2026-08-21", "00-00-00", "a/b:c"), "a b c")


class Timestamps(unittest.TestCase):
    def test_offset_formatting(self):
        self.assertEqual(parsers.format_offset(0), "00:00")
        self.assertEqual(parsers.format_offset(65), "01:05")
        self.assertEqual(parsers.format_offset(3725), "1:02:05")

    def test_retranscribed_segments_still_get_a_timestamp(self):
        # Retranscription drops display_time and writes an ISO `timestamp` of
        # when it ran, which is not a position in the recording.
        self.assertEqual(
            parsers.segment_time({"audio_start_time": 52.63, "timestamp": "2026-03-25T07:41:54Z"}),
            "00:52")

    def test_no_time_data_yields_no_timestamp(self):
        self.assertEqual(parsers.segment_time({}), "")

    def test_parse_segments_skips_empty_text_and_keeps_speakers(self):
        fmt_segments = {"segments": [
            {"text": " ", "audio_start_time": 0},
            {"text": "hi", "audio_start_time": 5, "speaker": "Ada"},
        ]}
        out = parsers.parse_segments(fmt_segments)
        self.assertEqual(out, [{"time": "00:05", "text": "hi", "speaker": "Ada"}])


class LanguageAndPeople(unittest.TestCase):
    def setUp(self):
        self._people, self._owner = enrich.KNOWN_PEOPLE, enrich.OWNER_NAME
    def tearDown(self):
        enrich.KNOWN_PEOPLE, enrich.OWNER_NAME = self._people, self._owner

    def test_language_detection(self):
        self.assertEqual(enrich.detect_language_code("This is an English meeting. " * 20), "en")
        self.assertEqual(enrich.detect_language_code("Це українська зустріч про роботу. " * 20), "uk")
        # Cyrillic without any Ukrainian-only letter reads as Russian.
        self.assertEqual(enrich.detect_language_code("Это русская встреча о работе. " * 20), "ru")

    def test_short_transcripts_get_no_language(self):
        self.assertEqual(enrich.detect_language_code("Проверка звуку. Yeah."), "")

    def test_meetily_detection_beats_the_heuristic(self):
        ukrainian = [{"text": "Це українська зустріч. " * 30}]
        self.assertEqual(enrich.resolve_language({"detected_summary_language": "en"}, ukrainian), "english")
        self.assertEqual(enrich.resolve_language({"summary_language": "pl"}, ukrainian), "polish")
        self.assertEqual(enrich.resolve_language({}, ukrainian), "ukrainian")

    def test_unknown_code_falls_back_to_the_code(self):
        self.assertEqual(enrich.language_name("zz"), "zz")
        self.assertEqual(enrich.language_name("en-GB"), "english")
        self.assertEqual(enrich.language_name(""), "")

    def test_duplicate_aliases_do_not_inflate_the_count(self):
        enrich.OWNER_NAME = ""
        enrich.KNOWN_PEOPLE = {"Taras": ["Тарас", "Тарас", "Тарас"], "Grace": ["Grace"]}
        segs = [{"text": "Тарас сказав. Grace теж. Grace ще раз. Grace втретє."}]
        # Grace has three real mentions; Taras has one counted once, not three.
        self.assertEqual(enrich.detect_participants(segs), ["Grace", "Taras"])

    def test_owner_is_listed_first_even_without_mentions(self):
        enrich.OWNER_NAME, enrich.KNOWN_PEOPLE = "Me", {"Taras": ["Тарас"]}
        self.assertEqual(enrich.detect_participants([{"text": "Тарас говорив"}]), ["Me", "Taras"])

    def test_matching_is_whole_word(self):
        enrich.OWNER_NAME, enrich.KNOWN_PEOPLE = "", {"Alexis": ["Alexis"]}
        self.assertEqual(enrich.detect_participants([{"text": "Alexisandra was here"}]), [])
        self.assertEqual(enrich.detect_participants([{"text": "alexis was here"}]), ["Alexis"])

    def test_status_and_model_follow_retranscribed_at(self):
        self.assertEqual(enrich.resolve_status({}), "baseline")
        self.assertEqual(enrich.resolve_status({"retranscribed_at": "2026-08-21"}), "enhanced")
        self.assertEqual(enrich.resolve_model({}), config.BASELINE_MODEL)
        self.assertEqual(enrich.resolve_model({"retranscribed_at": "x"}), config.ENHANCED_MODEL)


class Utils(unittest.TestCase):
    def test_meeting_uuid_strips_the_prefix(self):
        self.assertEqual(utils.meeting_uuid("meeting-abc-123"), "abc-123")
        self.assertEqual(utils.meeting_uuid(""), "")

    def test_sanitize_filename(self):
        self.assertEqual(utils.sanitize_filename('a/b:c*d?"e<f>g|h'), "a b c d e f g h")
        self.assertEqual(utils.sanitize_filename("   "), "Untitled Meeting")
        self.assertEqual(utils.sanitize_filename("trailing."), "trailing")


class SummarySource(unittest.TestCase):
    """english_cache holds the English original of a *translated* summary."""

    def _db_with(self, payload: dict) -> Path:
        self._seq += 1
        path = Path(self.tmp.name) / f"m{self._seq}.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE meetings (id TEXT, title TEXT, created_at TEXT, "
                     "updated_at TEXT, folder_path TEXT)")
        conn.execute("CREATE TABLE summary_processes (meeting_id TEXT, status TEXT, result TEXT)")
        conn.execute("INSERT INTO meetings VALUES ('m1','T','','','/f')")
        conn.execute("INSERT INTO summary_processes VALUES ('m1','completed',?)",
                     (json.dumps(payload),))
        conn.commit(); conn.close()
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._seq = 0
        self._saved = (config.MEETILY_DB, config.SUMMARY_PREFER_ENGLISH)

    def tearDown(self):
        import db as db_module, importlib
        config.MEETILY_DB, config.SUMMARY_PREFER_ENGLISH = self._saved
        importlib.reload(db_module)          # leave the module bound to the real config
        self.tmp.cleanup()

    def _summary(self, payload, prefer_english):
        import db as db_module, importlib
        path = self._db_with(payload)
        config.MEETILY_DB = path
        config.SUMMARY_PREFER_ENGLISH = prefer_english
        importlib.reload(db_module)
        handle = db_module.MeetilyDB()
        try:
            return handle.get_summary("/f")
        finally:
            handle.close()

    def test_prefers_english_when_configured(self):
        payload = {"markdown": "Український текст",
                   "english_cache": {"markdown": "English text", "output_language": "English"}}
        self.assertEqual(self._summary(payload, True), "English text")
        self.assertEqual(self._summary(payload, False), "Український текст")

    def test_falls_back_when_the_preferred_side_is_empty(self):
        self.assertEqual(self._summary({"markdown": "only this"}, True), "only this")
        self.assertEqual(self._summary({"markdown": "", "english_cache": {"markdown": "cached"}}, False),
                         "cached")

    def test_empty_summary_is_none_not_a_crash(self):
        self.assertIsNone(self._summary({"markdown": "", "summary_json": [{"content": []}]}, True))

    def test_non_object_payload_does_not_raise(self):
        self.assertIsNone(self._summary(None, True))


class Settling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_files_matching_state_never_reach_the_sleep(self):
        path = self.dir / "transcripts.json"
        path.write_text("{}", encoding="utf-8")
        st = path.stat()
        state = {str(path): {"mtime": st.st_mtime, "size": st.st_size}}
        import time
        started = time.perf_counter()
        self.assertEqual(sync.filter_stable([path], state), [path])
        self.assertLess(time.perf_counter() - started, sync.SETTLE_SECONDS / 2)

    def test_a_file_still_being_written_is_dropped(self):
        path = self.dir / "transcripts.json"
        path.write_text("{}", encoding="utf-8")

        real_sleep = sync.time.sleep
        def grow(_seconds):                      # simulate Meetily appending mid-check
            path.write_text('{"segments": []}', encoding="utf-8")
        sync.time.sleep = grow
        try:
            self.assertEqual(sync.filter_stable([path], {}), [])
        finally:
            sync.time.sleep = real_sleep

    def test_one_sleep_covers_the_whole_batch(self):
        paths = []
        for i in range(5):
            p = self.dir / f"t{i}.json"
            p.write_text("{}", encoding="utf-8")
            paths.append(p)
        calls = []
        real_sleep = sync.time.sleep
        sync.time.sleep = lambda s: calls.append(s)
        try:
            self.assertEqual(sync.filter_stable(paths, {}), paths)
        finally:
            sync.time.sleep = real_sleep
        self.assertEqual(len(calls), 1, "one sleep per batch, not per file")


class EndToEnd(unittest.TestCase):
    """A whole meeting through sync_file, against temp folders."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.folder = root / "recordings" / "Meeting 2026-08-21_16-33-27_2026-08-21_13-33"
        self.folder.mkdir(parents=True)
        self.vault = root / "vault"
        (self.folder / "transcripts.json").write_text(json.dumps({"segments": [
            {"text": "This is an English meeting about work. " * 8, "audio_start_time": 0.0},
            {"text": "Second segment here.", "audio_start_time": 65.0},
        ]}), encoding="utf-8")
        (self.folder / "metadata.json").write_text(json.dumps({
            "meeting_name": "Team sync", "duration_seconds": 120.0,
            "devices": {"microphone": "Mic"},
        }), encoding="utf-8")
        self._saved = {k: getattr(sync, k) for k in ("OBSIDIAN_FOLDER", "COPY_AUDIO")}
        sync.OBSIDIAN_FOLDER = self.vault
        sync.COPY_AUDIO = False
        md.OBSIDIAN_FOLDER = self.vault
        sync._id_index = None
        sub = md.render_subfolder("2026-08-21", "16-33-27", "Team sync")
        self.note = (self.vault / sub / "Team sync.md") if sub else (self.vault / "Team sync.md")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(sync, k, v)
        self.tmp.cleanup()

    class _NoDB:
        def get_meeting_record(self, _): return None
        def get_summary(self, _): return None

    def _sync(self, state, force=False):
        return sync.sync_file(self.folder / "transcripts.json", state,
                              self._NoDB(), force=force, check_db=False)

    def test_creates_a_note_and_is_idempotent(self):
        state = {}
        self.assertTrue(self._sync(state))
        note = self.note
        self.assertTrue(note.exists(), f"expected a note at {note}")
        text = note.read_text(encoding="utf-8")
        fm = md.parse_frontmatter(text)
        self.assertEqual(fm["source"], "Meetily")
        self.assertEqual(fm["status"], "baseline")
        self.assertEqual(fm["language"], "english")
        self.assertEqual(fm["transcription_model"], config.BASELINE_MODEL)
        self.assertIn("# Meeting: Team sync", text)
        self.assertIn("[01:05]", text)                 # elapsed offset, not wall clock
        before = text
        self.assertFalse(self._sync(state))            # nothing changed -> skipped
        self.assertEqual(note.read_text(encoding="utf-8"), before)

    def test_retranscribe_flips_status_and_reclaims_a_stale_review(self):
        state = {}
        self._sync(state)
        note = self.note
        note.write_text(note.read_text(encoding="utf-8")
                        .replace('status: "baseline"', 'status: "reviewed"'), encoding="utf-8")
        self._sync(state, force=True)
        self.assertEqual(md.parse_frontmatter(note.read_text(encoding="utf-8"))["status"], "reviewed")

        meta = json.loads((self.folder / "metadata.json").read_text(encoding="utf-8"))
        meta["retranscribed_at"] = "2026-08-21T20:00:00+00:00"
        (self.folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        self._sync(state, force=True)
        fm = md.parse_frontmatter(note.read_text(encoding="utf-8"))
        self.assertEqual(fm["status"], "enhanced")
        self.assertEqual(fm["transcription_model"], config.ENHANCED_MODEL)

    def test_user_content_and_properties_survive(self):
        state = {}
        self._sync(state)
        note = self.note
        text = note.read_text(encoding="utf-8")
        text = text.replace('source: "Meetily"', 'source: "Meetily"\ncssclasses: wide')
        text = text.replace("participants: []", "participants: [Sam, Ada]")
        note.write_text(text.rstrip() + "\n\n## Mine\n\nFollow up.\n", encoding="utf-8")
        self._sync(state, force=True)
        after = note.read_text(encoding="utf-8")
        self.assertIn("cssclasses: wide", after)
        self.assertIn("Follow up.", after)
        self.assertEqual(md.parse_frontmatter(after)["participants"], ["Sam", "Ada"])

    def test_changing_the_pattern_moves_the_note_instead_of_copying_it(self):
        import importlib
        state = {}
        self._sync(state)
        self.assertTrue(self.note.exists())
        saved = config.NOTE_SUBFOLDER_PATTERN
        try:
            config.NOTE_SUBFOLDER_PATTERN = "{year}/{quarter}"
            importlib.reload(md)
            sync.render_subfolder = md.render_subfolder
            self._sync(state, force=True)
            moved = self.vault / "2026" / "Q3" / "Team sync.md"
            self.assertTrue(moved.exists(), "note should have moved")
            self.assertFalse(self.note.exists(), "no copy left behind")
        finally:
            config.NOTE_SUBFOLDER_PATTERN = saved
            importlib.reload(md)
            sync.render_subfolder = md.render_subfolder

    def test_empty_transcript_is_pinned_until_the_file_changes(self):
        (self.folder / "transcripts.json").write_text('{"segments": []}', encoding="utf-8")
        state = {}
        self.assertFalse(self._sync(state))
        entry = state[str(self.folder / "transcripts.json")]
        self.assertEqual(entry["skip_reason"], "empty")
        self.assertEqual(entry["format_version"], config.NOTE_FORMAT_VERSION)

    def test_a_stale_error_pin_is_released_by_a_format_bump(self):
        key = str(self.folder / "transcripts.json")
        st = (self.folder / "transcripts.json").stat()
        state = {key: {"skip_reason": "boom", "mtime": st.st_mtime, "size": st.st_size,
                       "format_version": config.NOTE_FORMAT_VERSION - 1}}
        self.assertTrue(self._sync(state), "an older format version must be retried")


if __name__ == "__main__":
    unittest.main()
