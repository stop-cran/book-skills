"""Project integration and failure-path regressions; no external services."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import clean_text
import synthesize as s
from book_project import read_project
from chunk_text import chunk_segments, split_sentences
from test_synthesize import ENDPOINT_A, ENDPOINT_B, Project, Response, chapter


class Resume(Project):
    def setUp(self):
        super().setUp()
        self.write("01-a", chapter())

    def run_book(self, *args):
        return self.main("--endpoint", ENDPOINT_A, *args)

    def test_verified_resume_needs_no_login_or_retagging(self):
        self.assertEqual(self.run_book()[0], 0)
        sent, providers = len(self.tts.sent), self.providers
        self.tagged.reset_mock()
        code, out = self.run_book()
        self.assertEqual(code, 0, out)
        self.assertIn("skip (verified", out)
        self.assertEqual((len(self.tts.sent), self.providers), (sent, providers))
        self.tagged.assert_not_called()

    def test_source_voice_rate_endpoint_and_corruption_invalidate_resume(self):
        self.assertEqual(self.run_book()[0], 0)
        for flags in (("--rate=0.9",), ("--voice", "ru-RU-Masha:MAI-Voice-2"),
                      ("--endpoint", ENDPOINT_B), ("--max-chars", "500")):
            with self.subTest(flags=flags):
                self.assertEqual(self.run_book(*flags)[0], 1)
        self.write("01-a", chapter() + "\nChanged source.\n")
        self.assertEqual(self.run_book()[0], 1)
        self.assertEqual(self.run_book("--force")[0], 0)
        (self.audio_dir / "01-a.mp3").write_bytes(b"damaged")
        self.assertEqual(self.run_book()[0], 1)

    def test_cache_survives_encode_failure_and_reuses_exact_audio(self):
        with mock.patch.object(s, "encode_mp3", side_effect=RuntimeError("encode failed")):
            self.assertEqual(self.run_book()[0], 1)
        sent = len(self.tts.sent)
        self.assertGreater(sent, 0)
        self.assertFalse((self.audio_dir / "01-a.mp3").exists())
        self.assertFalse((self.audio_dir / "01-a.manifest.json").exists())
        self.assertEqual(self.run_book()[0], 0)
        self.assertEqual(len(self.tts.sent), sent)
        self.assertEqual(list(self.audio_dir.glob("*.pending.mp3")), [])

    def test_tag_failure_preserves_previous_complete_output(self):
        self.assertEqual(self.run_book()[0], 0)
        before = (self.audio_dir / "01-a.manifest.json").read_bytes()
        self.tagged.side_effect = RuntimeError("tag failed")
        self.assertEqual(self.run_book("--force")[0], 1)
        self.assertEqual((self.audio_dir / "01-a.manifest.json").read_bytes(), before)
        self.assertEqual((self.audio_dir / "01-a.mp3").read_bytes(), b"mp3")
        self.assertEqual(list(self.audio_dir.glob("*.pending.mp3")), [])

    def test_changed_source_during_synthesis_does_not_publish(self):
        def tag(*args):
            self.write("01-a", chapter() + "\nA concurrent edit.\n")
        self.tagged.side_effect = tag
        self.assertEqual(self.run_book()[0], 1)
        self.assertFalse((self.audio_dir / "01-a.mp3").exists())

    def test_empty_response_does_not_publish(self):
        self.endpoint(lambda text: Response(200, s._wrap_wav(b"")))
        self.assertEqual(self.run_book()[0], 1)
        self.assertFalse((self.audio_dir / "01-a.mp3").exists())

    def test_exhausted_throttling_never_splits(self):
        self.endpoint(lambda text: Response(429))
        code, out = self.run_book()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.tts.sent), 3)
        self.assertNotIn("retry-split", out)

    def test_corrupt_cache_stops_instead_of_certifying_it(self):
        self.assertEqual(self.run_book("--limit-chunks", "1")[0], 0)
        cache = next((self.audio_dir / ".cache").rglob("*.wav"))
        cache.write_bytes(b"broken")
        self.assertEqual(self.run_book()[0], 1)
        self.assertFalse((self.audio_dir / "01-a.mp3").exists())

    def test_smoke_never_publishes_a_completion_receipt(self):
        self.assertEqual(self.run_book("--limit-chunks", "1")[0], 0)
        self.assertTrue((self.audio_dir / "01-a.smoke.mp3").exists())
        self.assertFalse((self.audio_dir / "01-a.mp3").exists())
        self.assertFalse((self.audio_dir / "01-a.manifest.json").exists())
        self.assertEqual(self.run_book()[0], 0)
        self.assertTrue((self.audio_dir / "01-a.manifest.json").exists())

    def test_forced_replacement_resumes_after_encode_failure(self):
        self.assertEqual(self.run_book()[0], 0)
        self.write("01-a", chapter() + "\nA new paragraph.\n")
        with mock.patch.object(s, "encode_mp3", side_effect=RuntimeError("encode failed")):
            self.assertEqual(self.run_book("--force")[0], 1)
        sent = len(self.tts.sent)
        self.assertEqual(self.run_book("--force")[0], 0)
        self.assertEqual(len(self.tts.sent), sent)
        self.assertEqual(self.run_book("--force", "--refresh-cache")[0], 0)
        self.assertGreater(len(self.tts.sent), sent)

    def test_refresh_cache_requires_explicit_replacement_and_real_synthesis(self):
        self.assertEqual(self.run_book("--refresh-cache")[0], 2)
        self.assertEqual(self.run_book("--force", "--refresh-cache", "--dry-run")[0], 2)
        self.assertEqual(self.run_book("--force", "--refresh-cache", "--tag-only")[0], 2)


class Configuration(Project):
    def setUp(self):
        super().setUp()
        self.write("01-first", chapter())
        self.write("02-second", chapter())
        self.write("03-third", chapter())
        self.config = {
            "schema_version": 1, "text_dir": "text", "out_dir": "audio",
            "language": "ru", "xml_lang": "ru-RU", "voice": s.DEFAULT_VOICE,
            "metadata": {"artist": "Test author", "album": "Test book", "year": "2026"},
            "chapters": [1, 2],
        }
        self.path = self.root / "book.json"
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def test_configuration_paths_and_exact_range(self):
        project = read_project(self.path)
        self.assertEqual(list(project.sources), ["01-first", "02-second"])
        self.assertEqual(list(project.tracks.values()), [1, 2])
        code, out = self.main("--project", str(self.path), "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("Totals: 2 files", out)
        self.assertFalse((self.audio_dir / "03-third.txt").exists())

    def test_missing_or_duplicate_chapters_fail(self):
        self.config["chapters"] = [1, 4]
        self.save()
        with self.assertRaisesRegex(ValueError, "Missing chapter"):
            read_project(self.path)
        self.write("01-duplicate", chapter())
        with self.assertRaisesRegex(ValueError, "Duplicate track"):
            read_project(self.path)

    def test_unnumbered_notes_cannot_satisfy_a_numeric_chapter_range(self):
        (self.text_dir / "02-second.md").unlink()
        (self.text_dir / "03-third.md").unlink()
        self.write("notes", chapter())
        with self.assertRaisesRegex(ValueError, "Missing chapter"):
            read_project(self.path)
        with mock.patch.object(s, "NARRATED_STEMS", ["01-first", "notes"]):
            self.assertEqual(self.main("--dry-run", "--chapters", "1-2")[0], 2)
        del self.config["chapters"]
        self.save()
        self.assertEqual(read_project(self.path).tracks, {"01-first": 1, "notes": 2})
        self.assertEqual(self.main("--project", str(self.path), "--dry-run", "--chapters", "1-2")[0], 2)

    def test_metadata_and_narration_share_the_first_nonblank_h1(self):
        self.write("01-first", "\n\n# **Heading**\n\nBody.\n")
        self.assertEqual(s.track_title("01-first"), "Heading")
        self.assertEqual(s.load_segments("01-first")[0].text, "Heading")
        for invalid in ("## Heading\n\nBody.", "# ![image](plot.png)\n\nBody."):
            self.write("01-first", invalid)
            with self.assertRaisesRegex(ValueError, "nonempty H1"):
                s.track_title("01-first")
            self.assertEqual(self.main("--endpoint", ENDPOINT_A, "01-first")[0], 1)

    def test_invalid_configuration_is_not_silently_defaulted(self):
        for key, value in (("schema_version", True), ("xml_lang", "en-US"),
                           ("voice", "en-US-Ethan:MAI-Voice-2"), ("chapters", [True, 3]),
                           ("unsupported_option", "ignored")):
            with self.subTest(key=key):
                original = self.config.copy()
                self.config[key] = value
                self.save()
                with self.assertRaises(ValueError):
                    read_project(self.path)
                self.config = original

    def test_cli_chapter_range_and_duplicate_inputs(self):
        self.assertEqual(self.main("--dry-run", "--chapters", "1-2")[0], 0)
        self.assertEqual(self.main("--dry-run", "--chapters", "1-4")[0], 2)
        self.assertEqual(self.main("--dry-run", "01-first", "01-first")[0], 2)
        self.assertEqual(self.main("--limit-chunks", "0")[0], 2)
        self.assertEqual(self.main("--all", "01-first")[0], 2)

    def test_explicit_resource_beats_environment_endpoint(self):
        with mock.patch.dict("os.environ", {"TTS_ENDPOINT": ENDPOINT_B}):
            self.assertEqual(s.resolve_endpoint("example-a"), ENDPOINT_A)

    def test_auth_tokens_cannot_be_sent_to_arbitrary_urls(self):
        for endpoint in ("http://example-a.cognitiveservices.azure.com/tts/cognitiveservices/v1",
                         "https://unrelated.example/tts/cognitiveservices/v1",
                         ENDPOINT_A + "?secret=value"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                s.resolve_endpoint(endpoint=endpoint)

    def test_spoken_prefix_disclosure_and_comment_are_configuration(self):
        self.config["narration"] = {"spoken_track_prefix": "Выпуск {number}. ", "opening": "Это конспект."}
        self.config["metadata"]["comment"] = "AI-assisted synopsis."
        self.save()
        self.main("--project", str(self.path), "--dry-run")
        text = (self.audio_dir / "01-first.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("## Выпуск 1. Chapter one"))
        self.assertIn("Это конспект.", text)
        self.assertNotIn("Это конспект.", (self.audio_dir / "02-second.txt").read_text(encoding="utf-8"))

    def test_unbounded_opt_in_preserves_explicit_essay_paths(self):
        del self.config["chapters"]
        self.config["allow_external_inputs"] = True
        self.save()
        essay = self.root / "2026-essay.md"
        essay.write_text(chapter(), encoding="utf-8")
        code, out = self.main("--project", str(self.path), "--dry-run", str(essay))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.audio_dir / "2026-essay.txt").exists())
        self.assertEqual(s.PROJECT.tracks["2026-essay"], 4)
        self.assertEqual(self.main("--project", str(self.path), "--dry-run",
                                   "--chapters", "1-3", str(essay))[0], 2)

    def test_bounded_project_cannot_opt_into_external_files(self):
        self.config["allow_external_inputs"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "chapter-bounded"):
            read_project(self.path)

    def test_partial_glob_never_silently_drops_an_unconfigured_file(self):
        self.assertEqual(self.main("--project", str(self.path), "--dry-run",
                                   str(self.text_dir / "*.md"))[0], 2)

    def test_external_stem_collision_fails(self):
        del self.config["chapters"]
        self.config["allow_external_inputs"] = True
        self.save()
        duplicate = self.root / "01-first.md"
        duplicate.write_text(chapter(), encoding="utf-8")
        self.assertEqual(self.main("--project", str(self.path), "--dry-run", str(duplicate))[0], 2)

    def test_ad_hoc_extra_does_not_invalidate_existing_catalog_total(self):
        del self.config["chapters"]
        self.config["allow_external_inputs"] = True
        self.save()
        flags = ("--project", str(self.path), "--endpoint", ENDPOINT_A)
        self.assertEqual(self.main(*flags, "01-first")[0], 0)
        essay = self.root / "essay.md"
        essay.write_text(chapter(), encoding="utf-8")
        code, out = self.main(*flags, "01-first", str(essay))
        self.assertEqual(code, 0, out)
        self.assertIn("skip (verified", out)
        receipt = json.loads((self.audio_dir / "01-first.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["request"]["total"], 3)


class TextPreparation(unittest.TestCase):
    def test_word_governed_russian_references_require_explicit_cases(self):
        readings = [
            ("установленное", "instrumental", "параграфом"),
            ("принадлежит", "dative", "параграфу"),
            ("условия", "genitive", "параграфа"),
            ("в конце", "genitive", "параграфа"),
            ("показал", "nominative", "параграф"),
        ]
        for phrase, case, expected in readings:
            with self.subTest(phrase=phrase):
                text = phrase + " §16"
                with self.assertRaisesRegex(ValueError, "reference_cases"):
                    clean_text.clean_inline(text, "ru", "scientific")
                self.assertEqual(clean_text.clean_inline(text, "ru", "scientific",
                                                        reference_cases={phrase: case}),
                                 phrase + " " + expected + " 16")

    def test_tables_require_an_explicit_book_policy_and_prose_pipes_survive(self):
        md = "# Title\n\nProse A | B.\n\n| Name | Value |\n| --- | --- |\n| A | 1 |\n\nAfter.\n"
        with self.assertRaisesRegex(ValueError, "Markdown table"):
            clean_text.parse_markdown(md, "en")
        segments = clean_text.parse_markdown(md, "en", tables="skip")
        self.assertEqual([s.text for s in segments], ["Title", "Prose A | B.", "After."])

    def test_scientific_notation_is_localized_and_scoped(self):
        text = "§§17–20 (VIII): T² ∝ r³; A = A; +A; −A; x · y."
        ru = clean_text.clean_inline(text, "ru", "scientific")
        self.assertEqual(ru, "параграфы с 17 по 20 часть 8: T в квадрате пропорционально r в кубе; "
                            "A равно A; плюс A; минус A; x умножить на y.")
        self.assertIn("T²", clean_text.clean_inline(text, "ru"))
        self.assertEqual(clean_text.clean_inline("§17–§20", "en", "scientific"), "sections 17 to 20")

    def test_nested_emphasis_and_quotes_keep_the_words(self):
        md = "# Title\n\n*An **important** (*German*) abstract.*\n\n> **Quoted** text.\n"
        segments = clean_text.parse_markdown(md, "en", notation="scientific")
        self.assertEqual([s.text for s in segments], ["Title", "An important (German) abstract.", "Quoted text."])
        self.assertEqual(segments[-1].kind, "quote")

    def test_nonpositive_budget_is_rejected(self):
        for budget in (0, -1):
            with self.assertRaises(ValueError):
                chunk_segments([clean_text.Segment("para", "Text.")], budget=budget)

    def test_noncanonical_roman_is_not_reinterpreted(self):
        self.assertIsNone(clean_text.roman_to_int("IIII"))
        self.assertIsNone(clean_text.roman_to_int("IC"))
        self.assertEqual(clean_text.roman_to_int("XXIX"), 29)

    def test_subscript_underscores_are_not_emphasis(self):
        self.assertEqual(clean_text.clean_inline("G_E and R_C; _word_; __bold__.", "en"),
                         "G_E and R_C; word; bold.")

    def test_russian_arrow_is_not_a_range_preposition(self):
        self.assertEqual(clean_text.clean_inline("A → B", "ru", "scientific"), "A к B")

    def test_roman_section_ranges_do_not_duplicate_the_section_word(self):
        self.assertEqual(clean_text.clean_inline("§§III–IV; §VI.", "en", "scientific"),
                         "sections 3 to 4; section 6.")

    def test_book_specific_roman_references_preserve_the_hierarchy(self):
        self.assertEqual(clean_text.clean_inline("§IV and §04; §§III–IV.", "en", "scientific", "part"),
                         "part 4 and section 04; parts 3 to 4.")
        self.assertEqual(clean_text.clean_inline("в §IV и §V; §04.", "ru", "scientific", "part"),
                         "в части 4 и части 5; параграф 04.")
        self.assertEqual(clean_text.clean_inline("§28, VII; в §29, IV–V", "ru", "scientific", "part"),
                         "параграф 28, часть 7; в параграфе 29, частях с 4 по 5")

    def test_russian_reference_cases_and_plural_ranges(self):
        self.assertEqual(clean_text.clean_inline("в §27 и из §09, в §§1445–1447", "ru", "scientific"),
                         "в параграфе 27 и из параграфа 09, в параграфах с 1445 по 1447")
        self.assertEqual(clean_text.clean_inline("к §28; перед §27; вслед за §09", "ru", "scientific",
                                                reference_cases={"вслед за": "instrumental"}),
                         "к параграфу 28; перед параграфом 27; вслед за параграфом 09")

    def test_russian_ambiguous_prepositions_require_grounded_context(self):
        cases = {"сходство с": "instrumental", "ждёт с": "genitive", "аналогией с": "instrumental",
                 "перенесена назад в": "accusative"}
        self.assertEqual(clean_text.clean_inline("Сходство с §13; ждёт с §01", "ru", "scientific",
                                                reference_cases=cases),
                         "Сходство с параграфом 13; ждёт с параграфа 01")
        self.assertEqual(clean_text.clean_inline("аналогией с §13 или §21", "ru", "scientific",
                                                reference_cases=cases),
                         "аналогией с параграфом 13 или параграфом 21")
        self.assertEqual(clean_text.clean_inline("перенесена назад в §03", "ru", "scientific",
                                                reference_cases=cases), "перенесена назад в параграф 03")
        with self.assertRaisesRegex(ValueError, "Ambiguous Russian reference"):
            clean_text.clean_inline("с §13", "ru", "scientific")

    def test_closed_numeric_citations_and_results_end_sentences(self):
        for language, first, second in (
                ("ru", "Это довод (НЛ, параграф 210).", "Имя фиксирует аргумент."),
                ("en", "This follows (SL, section 210).", "The name records it."),
                ("ru", "Результат равен 42.", "Затем следует вывод.")):
            with self.subTest(language=language, first=first):
                self.assertEqual(split_sentences(first + " " + second, language), [first, second])

    def test_balanced_url_parentheses_never_leak_into_narration(self):
        text = "(Author, [Title](https://example.org/Book_(Author)/01%3A_Chapter))."
        self.assertEqual(clean_text.clean_inline(text, "en"), "(Author, Title).")
        self.assertEqual(clean_text.clean_inline("Before ![plot](images/plot_(a).png) after", "en"),
                         "Before after")
        with self.assertRaisesRegex(ValueError, "Unclosed inline"):
            clean_text.clean_inline("[Title](https://example.org/unterminated", "en")

    def test_adjacent_superscript_does_not_glue_narrated_words(self):
        self.assertEqual(clean_text.clean_inline("d²r", "en", "scientific"), "d squared r")
        self.assertEqual(clean_text.clean_inline("d²r", "ru", "scientific"), "d в квадрате r")

    def test_diagram_keeps_words_but_not_fences_or_box_drawing(self):
        md = "# Title\n\n```\nBeing\n │ becomes\n ▼\nNothing\n```\n"
        with self.assertRaises(ValueError):
            clean_text.parse_markdown(md, "en")
        segs = clean_text.parse_markdown(md, "en", fenced_blocks="diagram", notation="scientific")
        self.assertEqual(" ".join(s.text for s in segs), "Title Being becomes Nothing")
        with self.assertRaises(ValueError):
            clean_text.parse_markdown("# Title\n\n```\nprint('hi')\n```\n", fenced_blocks="diagram")


class RealAudio(unittest.TestCase):
    def test_encode_tag_and_full_decode_with_unicode_metadata(self):
        from mutagen.id3 import ID3
        from mutagen.mp3 import MP3

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "01-test.md").write_text("# Проверка звука\n\nТекст.\n", encoding="utf-8")
            output = root / "01-test.mp3"
            with mock.patch.multiple(s, PROJECT=None, TEXT_DIR=root, LANGUAGE="ru",
                                     ID3_ARTIST="Тест", ID3_ALBUM="Конспект", ID3_YEAR="2026",
                                     ID3_COMMENT="Не текст Гегеля."):
                s.encode_mp3(s._wrap_wav(s._silence(1000)), output)
                s.tag_mp3(output, "01-test", 1, 1)
            tags = ID3(output)
            self.assertEqual(tags.version, (2, 3, 0))
            self.assertEqual(str(tags["TIT2"]), "Проверка звука")
            self.assertEqual(str(tags["TRCK"]), "01/1")
            self.assertEqual(tags.getall("COMM")[0].text, ["Не текст Гегеля."])
            self.assertAlmostEqual(MP3(output).info.length, 1.0, delta=0.2)
            result = subprocess.run([s._ffmpeg_exe(), "-v", "error", "-i", str(output),
                                     "-f", "null", "-"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
