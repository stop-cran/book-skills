"""Shared workflow regressions: fake TTS, real local MP3 encoding and decoding."""

import contextlib
import glob
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import synthesize as s
import workflow as w
from test_synthesize import ENDPOINT_A, ENDPOINT_B, Project

REAL_ENCODE = s.encode_mp3
REAL_TAG = s.tag_mp3


class Workflow(Project):
    def setUp(self):
        super().setUp()
        for name, function in (("encode_mp3", REAL_ENCODE), ("tag_mp3", REAL_TAG)):
            patcher = mock.patch.object(s, name, function)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write("01-first", "# First\n\nOpening passage.\n\n## Diagram\n\nAlpha becomes Beta.\n\n"
                   "Beta becomes Gamma.\n\n## Afterwards\n\nClosing passage.\n")
        self.write("02-second", "# Second\n\nAnother chapter.\n")
        self.config = {
            "schema_version": 1, "text_dir": "text", "out_dir": "audio",
            "language": "en", "xml_lang": "en-US", "voice": "en-US-Ethan:MAI-Voice-2",
            "metadata": {"artist": "Example author", "album": "Example book", "year": "2026",
                         "comment": "AI-assisted synopsis, not the original."},
        }
        self.profile = self.root / "book.json"
        self.profile.write_text(json.dumps(self.config), encoding="utf-8")
        self.record_path = self.audio_dir / "test.run.json"

    def workflow(self, command, *args, endpoint=ENDPOINT_A):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = w.main([command, "--record", str(self.record_path), "--endpoint", endpoint, *args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def plan(self, *args):
        code, output = self.workflow("plan", "--project", str(self.profile), "--max-chars", "500", *args)
        self.assertEqual(code, 0, output)
        return self.record()

    def record(self):
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    def preview(self, *args, name="opening"):
        return self.workflow("preview", "--stem", "01-first", "--name", name, *args)

    def render_book(self):
        code, output = self.main("--project", str(self.profile), "--endpoint", ENDPOINT_A,
                                 "--max-chars", "500", "--all")
        self.assertEqual(code, 0, output)

    def rewrite_receipt(self, stem="01-first", **audio_changes):
        output = self.audio_dir / f"{stem}.mp3"
        receipt_path = s.manifest_path(output)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["audio"].update(sha256=s.file_hash(output), bytes=output.stat().st_size, **audio_changes)
        s.write_json(receipt_path, receipt)

    def test_plan_round_trip_freezes_ordered_chunks_without_network(self):
        pipeline = s.pipeline_hash()
        record = self.plan("--all")
        self.assertEqual(self.providers, 0)
        self.assertEqual(self.tts.sent, [])
        self.assertEqual(s.pipeline_hash(), pipeline)
        self.assertNotIn(ENDPOINT_A, self.record_path.read_text(encoding="utf-8"))
        self.assertEqual([t["stem"] for t in record["plan"]["tracks"]], ["01-first", "02-second"])
        for track in record["plan"]["tracks"]:
            self.assertEqual(s.json_hash(track["chunks"]), track["request"]["chunks_sha256"])
        self.assertEqual(w.load_record(self.record_path, None, ENDPOINT_A), record)
        self.assertEqual(self.workflow("plan", "--project", str(self.profile))[0], 1)
        self.assertEqual(self.record(), record)

    def test_plan_rejects_out_of_output_record_and_invalid_inputs_before_login(self):
        self.record_path = self.root / "outside.run.json"
        self.assertEqual(self.workflow("plan", "--project", str(self.profile))[0], 1)
        self.assertFalse(self.record_path.exists())
        self.record_path = self.audio_dir / "test.run.json"
        for args in (("--all", "01-first"), ("--chapters", "1-3"), ("--max-chars", "0"),
                     ("--voice", "ru-RU-Lev:MAI-Voice-2")):
            with self.subTest(args=args):
                self.assertEqual(self.workflow("plan", "--project", str(self.profile), *args)[0], 1)
        self.assertEqual(self.providers, 0)

    def test_drift_and_tampered_plan_fail_before_network(self):
        record = self.plan()
        self.assertEqual(self.workflow("verify", endpoint=ENDPOINT_B)[0], 1)
        original = self.profile.read_bytes()
        self.profile.write_bytes(original + b"\n")
        self.assertEqual(self.preview("--chunks", "1")[0], 1)
        self.profile.write_bytes(original)
        with mock.patch.object(s, "pipeline_hash", return_value="changed"):
            self.assertEqual(self.preview("--chunks", "1")[0], 1)
        record["plan"]["tracks"][0]["chunks"][0][0] = "Not approved text."
        record["plan_sha256"] = s.json_hash(record["plan"])
        s.write_json(self.record_path, record)
        self.assertEqual(self.preview("--chunks", "1")[0], 1)
        self.assertEqual(self.providers, 0)

    def test_complete_diagram_selection_is_inclusive_and_dry(self):
        self.plan()
        code, output = self.preview("--from-text", "Diagram", "--through-text", "Beta becomes Gamma.",
                                    "--dry-run", name="diagram")
        self.assertEqual(code, 0, output)
        entry = self.record()["previews"]["diagram"]
        self.assertEqual([c[0] for c in entry["chunks"]],
                         ["Diagram", "Alpha becomes Beta.", "Beta becomes Gamma."])
        self.assertIsNone(entry["audio"])
        self.assertFalse(Path(entry["output"]).exists())
        self.assertEqual(self.providers, 0)
        self.assertEqual(w.load_record(self.record_path, None, ENDPOINT_A), self.record())

    def test_missing_ambiguous_reversed_and_out_of_bounds_selectors_fail(self):
        self.write("01-first", "# First\n\nRepeat Repeat.\n\n## Ending\n\nLast paragraph.\n")
        self.plan()
        for args in (("--from-text", "Repeat"), ("--from-text", "Missing"),
                     ("--from-text", "Last", "--through-text", "First"),
                     ("--chunks", "0"), ("--chunks", "1-99"), ("--chunks", "3-2"),
                     ("--chunks", "1", "--through-text", "Last")):
            with self.subTest(args=args):
                code, output = self.preview(*args)
                self.assertEqual(code, 1, output)
        self.assertEqual(self.providers, 0)
        self.assertEqual(self.record()["previews"], {})

    def test_preview_reuses_verified_output_and_is_not_a_canonical_track(self):
        self.plan()
        code, output = self.preview("--chunks", "1-2")
        self.assertEqual(code, 0, output)
        entry = self.record()["previews"]["opening"]
        self.assertTrue(Path(entry["output"]).is_file())
        self.assertFalse((self.audio_dir / "01-first.mp3").exists())
        self.assertEqual(list(self.audio_dir.glob("*.manifest.json")), [])
        sent, providers = len(self.tts.sent), self.providers
        self.assertEqual(self.preview("--chunks", "1-2")[0], 0)
        self.assertEqual((len(self.tts.sent), self.providers), (sent, providers))
        self.assertEqual(self.preview("--chunks", "2-3")[0], 1)
        Path(entry["output"]).write_bytes(b"corrupt")
        self.assertEqual(self.preview("--chunks", "1-2")[0], 1)
        self.assertEqual(len(self.tts.sent), sent)

    def test_preview_failure_preserves_canonical_audio_and_cached_work(self):
        self.plan()
        self.render_book()
        output = self.audio_dir / "01-first.mp3"
        before = output.read_bytes()
        sent = len(self.tts.sent)
        with mock.patch.object(s, "encode_mp3", side_effect=RuntimeError("failed encoding")):
            self.assertEqual(self.preview("--chunks", "1-2")[0], 1)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(list(self.audio_dir.glob("*.pending.mp3")), [])
        self.assertIsNone(self.record()["previews"]["opening"]["audio"])
        self.assertEqual(self.preview("--chunks", "1-2")[0], 0)
        self.assertEqual(len(self.tts.sent), sent)

    def test_source_change_during_preview_prevents_publication(self):
        self.plan()

        def encode_and_edit(*args):
            REAL_ENCODE(*args)
            self.write("02-second", "# Second\n\nChanged after approval.\n")

        with mock.patch.object(s, "encode_mp3", side_effect=encode_and_edit):
            self.assertEqual(self.preview("--chunks", "1-2")[0], 1)
        self.assertEqual(list(self.audio_dir.glob("*.smoke.mp3")), [])
        self.assertEqual(list(self.audio_dir.glob("*.pending.mp3")), [])

    def test_unrecorded_preview_is_not_overwritten(self):
        record = self.plan()
        spec = w.preview_spec(record["plan"], "opening", "01-first",
                              {"chunks": "1", "from_text": None, "through_text": None})
        output = Path(spec["output"])
        output.write_bytes(b"unverified audio")
        self.assertEqual(self.preview("--chunks", "1")[0], 1)
        self.assertEqual(output.read_bytes(), b"unverified audio")
        self.assertEqual(self.providers, 0)

    def test_approval_requires_rendered_samples_and_explicit_human_declaration(self):
        self.plan()
        flags = ("--listened", "--scope-approved", "--note", "Human approved the sample and selection.")
        self.assertEqual(self.workflow("approve", *flags)[0], 1)
        self.assertEqual(self.preview("--chunks", "1-2", "--dry-run")[0], 0)
        self.assertEqual(self.workflow("approve", *flags)[0], 1)
        self.assertEqual(self.preview("--chunks", "1-2")[0], 0)
        self.assertEqual(self.workflow("approve", "--scope-approved")[0], 2)
        code, output = self.workflow("approve", *flags)
        self.assertEqual(code, 0, output)
        self.assertIn("cannot certify", output)
        self.assertIsNotNone(w.load_record(self.record_path, None, ENDPOINT_A)["approval"])
        self.assertEqual(self.preview("--chunks", "3-5", "--dry-run", name="diagram")[0], 0)
        self.assertIsNone(self.record()["approval"])

    def test_exact_album_full_verification_is_read_only_for_audio_and_needs_no_login(self):
        self.plan()
        self.render_book()
        self.assertEqual(self.preview("--chunks", "1-2")[0], 0)
        protected = {p: s.file_hash(p) for p in self.audio_dir.rglob("*")
                     if p.is_file() and (p.suffix in (".mp3", ".wav")
                                        or p.name.endswith(".manifest.json"))}
        with mock.patch.object(s, "make_token_provider", side_effect=AssertionError("no login")), \
                mock.patch.object(s, "synthesize_file", side_effect=AssertionError("no synthesis")):
            code, output = self.workflow("verify")
        self.assertEqual(code, 0, output)
        self.assertEqual(protected, {p: s.file_hash(p) for p in protected})
        reference = self.record()["verification"]
        report_path = Path(reference["path"])
        self.assertEqual(reference["sha256"], s.file_hash(report_path))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["tracks"]), 2)
        self.assertFalse(report["listening_certified"])
        self.assertFalse(report["approval_recorded"])

    def test_documented_command_sequence_with_simulated_human_declaration(self):
        self.plan("--all")
        self.assertEqual(self.preview("--chunks", "1-4", "--dry-run")[0], 0)
        self.assertEqual(self.preview("--chunks", "1-4")[0], 0)
        self.assertEqual(self.workflow("approve", "--listened", "--scope-approved",
                                       "--note", "Simulated declaration in an offline test.")[0], 0)
        self.render_book()
        code, output = self.workflow("verify")
        self.assertEqual(code, 0, output)
        report = json.loads((self.audio_dir / "test.verification.json").read_text(encoding="utf-8"))
        self.assertTrue(report["approval_recorded"])
        self.assertFalse(report["listening_certified"])

    def test_missing_extra_and_orphan_outputs_make_batch_fail(self):
        self.plan()
        self.render_book()
        for filename in ("unexpected.mp3", "orphan.manifest.json"):
            with self.subTest(filename=filename):
                extra = self.audio_dir / filename
                extra.write_bytes(b"unexpected")
                self.assertEqual(self.workflow("verify")[0], 1)
                self.assertEqual(self.record()["verification"]["status"], "failed")
                extra.unlink()
        (self.audio_dir / "02-second.mp3").unlink()
        self.assertEqual(self.workflow("verify")[0], 1)
        report = json.loads((self.audio_dir / "test.verification.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["tracks"]), 1)
        self.assertGreater(len(report["errors"]), 0)

    def test_metadata_checked_even_when_manifest_audio_hash_matches(self):
        from mutagen.id3 import ID3, TIT2

        self.plan()
        self.render_book()
        output = self.audio_dir / "01-first.mp3"
        tags = ID3(output)
        tags["TIT2"] = TIT2(encoding=3, text="Wrong title")
        tags.save(output, v2_version=3)
        self.rewrite_receipt()
        code, message = self.workflow("verify")
        self.assertEqual(code, 1)
        self.assertIn("incorrect TIT2", message)

    def test_corrupt_audio_and_invalid_duration_receipts_fail(self):
        self.plan()
        self.render_book()
        output = self.audio_dir / "01-first.mp3"
        original = output.read_bytes()
        output.write_bytes(b"corrupt")
        self.assertEqual(self.workflow("verify")[0], 1)
        output.write_bytes(original)
        for seconds in (float("nan"), 0, -1, True, 9999):
            with self.subTest(seconds=seconds):
                self.rewrite_receipt(seconds=seconds)
                self.assertEqual(self.workflow("verify")[0], 1)

    def test_decoder_failure_never_reports_completion(self):
        self.plan()
        self.render_book()
        with mock.patch.object(w.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, stdout=b"", stderr=b"decoding failed")):
            code, output = self.workflow("verify")
        self.assertEqual(code, 1)
        self.assertIn("full MP3 decoding failed", output)
        self.assertEqual(self.record()["verification"]["status"], "failed")

    def test_subset_keeps_catalog_track_total_and_checks_exact_output_directory(self):
        record = self.plan("--chapters", "2-2")
        self.assertEqual([t["stem"] for t in record["plan"]["tracks"]], ["02-second"])
        self.assertEqual(record["plan"]["tracks"][0]["request"]["total"], 2)
        self.assertEqual(w.load_record(self.record_path, None, ENDPOINT_A), record)

    def test_literal_glob_characters_in_catalog_paths_round_trip(self):
        destination = self.root / "text [draft]"
        self.text_dir.rename(destination)
        self.config["text_dir"] = destination.name
        self.profile.write_text(json.dumps(self.config), encoding="utf-8")
        record = self.plan("--all")
        self.assertEqual(w.load_record(self.record_path, None, ENDPOINT_A), record)
        self.assertEqual(self.preview("--chunks", "1", "--dry-run")[0], 0)

    def test_shared_source_aliases_round_trip_without_losing_sections_or_order(self):
        self.config["additional_sources"] = [
            {"path": "text/01-first.md", "stem": "03-excerpt", "intro_and_sections": ["Diagram"]},
        ]
        self.profile.write_text(json.dumps(self.config), encoding="utf-8")
        cases = (
            (("03-excerpt",), ["03-excerpt"]),
            (("01-first", "03-excerpt"), ["01-first", "03-excerpt"]),
            (("03-excerpt", "01-first"), ["03-excerpt", "01-first"]),
            (("--chapters", "3-3"), ["03-excerpt"]),
            (("--all",), ["01-first", "02-second", "03-excerpt"]),
        )
        for index, (inputs, expected) in enumerate(cases):
            with self.subTest(inputs=inputs):
                self.record_path = self.audio_dir / f"aliases-{index}.run.json"
                record = self.plan(*inputs)
                tracks = record["plan"]["tracks"]
                self.assertEqual([track["stem"] for track in tracks], expected)
                excerpt = next(track for track in tracks if track["stem"] == "03-excerpt")
                self.assertEqual(excerpt["request"]["track"], 3)
                self.assertEqual(excerpt["request"]["total"], 3)
                self.assertNotIn("Closing passage.", [chunk[0] for chunk in excerpt["chunks"]])
                self.assertEqual(w.load_record(self.record_path, None, ENDPOINT_A), record)
                self.assertEqual(self.workflow("preview", "--stem", "03-excerpt",
                                               "--name", "excerpt", "--chunks", "1", "--dry-run")[0], 0)
        self.assertEqual(self.providers, 0)
        self.render_book()
        self.assertEqual(self.workflow("verify")[0], 0)

    def test_literal_configured_alias_is_not_reinterpreted_as_a_filename_pattern(self):
        self.config["additional_sources"] = [
            {"path": "text/01-first.md", "stem": "03-excerpt [draft].md",
             "intro_and_sections": ["Diagram"]},
        ]
        self.profile.write_text(json.dumps(self.config), encoding="utf-8")
        record = self.plan("--all")
        self.assertEqual(w.load_record(self.record_path, None, ENDPOINT_A), record)

    def test_invalid_recorded_identities_fail_before_network(self):
        original = self.plan("--chapters", "1-2")
        for change in ("duplicate", "path", "missing", "range", "type"):
            with self.subTest(change=change):
                record = json.loads(json.dumps(original))
                plan = record["plan"]
                if change == "duplicate":
                    plan["tracks"][1]["stem"] = plan["tracks"][0]["stem"]
                elif change == "path":
                    plan["selection"]["inputs"][0] = plan["selection"]["inputs"][1]
                elif change == "missing":
                    plan["tracks"].pop()
                elif change == "range":
                    plan["tracks"].pop()
                    plan["selection"]["inputs"].pop()
                else:
                    plan["tracks"][0]["stem"] = []
                record["plan_sha256"] = s.json_hash(plan)
                s.write_json(self.record_path, record)
                code, output = self.preview("--chunks", "1", "--dry-run")
                self.assertEqual(code, 1, output)
        self.assertEqual(self.providers, 0)

    def test_external_essay_path_with_glob_characters_round_trips(self):
        self.config["allow_external_inputs"] = True
        self.profile.write_text(json.dumps(self.config), encoding="utf-8")
        essay = self.root / "essay [draft].md"
        essay.write_text("# Essay\n\nSupplementary passage.\n", encoding="utf-8")
        record = self.plan(glob.escape(str(essay)))
        self.assertEqual([t["stem"] for t in record["plan"]["tracks"]], [essay.stem])
        self.assertEqual(record["plan"]["tracks"][0]["request"]["track"], 3)
        self.assertEqual(record["plan"]["tracks"][0]["request"]["total"], 3)
        self.assertEqual(w.load_record(self.record_path, None, ENDPOINT_A), record)

    def test_nondefault_rate_round_trips_and_verifies(self):
        record = self.plan("--rate=-5%")
        self.assertEqual(record["plan"]["settings"]["rate"], "-5%")
        self.assertEqual(w.load_record(self.record_path, None, ENDPOINT_A), record)
        code, output = self.main("--project", str(self.profile), "--endpoint", ENDPOINT_A,
                                 "--max-chars", "500", "--rate=-5%", "--all")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.workflow("verify")[0], 0)

    def test_preview_cannot_occupy_a_canonical_smoke_named_chapter(self):
        self.write("preview-01-first-opening.smoke", "# Another chapter\n\nNot a preview.\n")
        self.plan("--all")
        code, output = self.preview("--chunks", "1")
        self.assertEqual(code, 1)
        self.assertIn("collides with a configured chapter", output)
        self.assertEqual(self.providers, 0)
        self.render_book()
        self.assertEqual(self.workflow("verify")[0], 0)


if __name__ == "__main__":
    unittest.main()
