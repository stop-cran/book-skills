"""Offline Japanese dialogue adapter tests; synthetic PCM is never a listening approval."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import listening
import synthesize as engine
from chunk_text import split_sentences


class ListeningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.content = self.root / "content"
        self.source = self.content / "listening" / "fixture.json"
        self.document = {
            "schemaVersion": 1, "jlpt": 5, "id": "fixture", "kind": "monologue",
            "speakers": [{"id": "a", "voice": "ja-JP-NanamiNeural", "ratePercent": -10}],
            "turns": [{"id": "one", "speaker": "a", "text": "私は学生です。日本語を勉強しています。"},
                      {"id": "two", "speaker": "a", "text": "明日も学校に行きます。"}],
            "questions": [{"test": "This is an adapter fixture, not a publishable learning package"}],
        }
        engine.write_json(self.source, self.document)
        self.endpoint = engine.resolve_endpoint(resource="example")
        self.plan = listening.create_plan([self.source], self.root / "run", self.endpoint, "test-subscription", ["fixture"])

    def test_japanese_boundaries_preserve_text_and_do_not_split_inside_quotations(self):
        text = "「駅はどこですか。」と聞きました。 近いですか！？はい、近いです。"
        pieces = split_sentences(text, "ja")
        self.assertEqual(["「駅はどこですか。」と聞きました。", " 近いですか！？", "はい、近いです。"], pieces)
        self.assertEqual(text, "".join(pieces))
        with mock.patch.object(engine, "LANGUAGE", "ja"), mock.patch.object(engine, "XML_LANG", "ja-JP"):
            with mock.patch.object(engine, "synth_pcm", side_effect=[
                engine.TransientExhaustionError("fixture", 503), b"\x00\x00", b"\x00\x00",
            ]) as transport:
                first, second = "一つです。" * 100, "二つです。" * 100
                engine.synth_pcm_resilient(first + second, lambda: "fake", mock.Mock(), "ja-JP-NanamiNeural", "-10%")
                self.assertEqual(3, transport.call_count)
                self.assertIn(first, transport.call_args_list[1].args[0])
                self.assertNotIn("二つです。", transport.call_args_list[1].args[0])

    def test_hash_matches_content_and_application_and_questions_invalidate_a_plan(self):
        self.assertEqual("3086a530afd0eb4dfe5523f94d04199542cc3d83f74e3ec4a78ffe6cd63350f1",
                         listening.content_hash({"z": ["日本語", {"b": 2, "a": True}], "a": None, "audio": {}}))
        changed = copy.deepcopy(self.document)
        changed["questions"][0]["test"] = "Changed scored fact"
        engine.write_json(self.source, changed)
        with self.assertRaisesRegex(ValueError, "Learning package changed"):
            listening.load_plan(self.plan)

    def test_preview_must_cover_all_voice_profiles_and_batch_approval_precedes_login(self):
        other = copy.deepcopy(self.document)
        other["id"] = "other"
        other["speakers"][0]["voice"] = "ja-JP-KeitaNeural"
        path = self.source.with_name("other.json")
        engine.write_json(path, other)
        with self.assertRaisesRegex(ValueError, "every selected voice"):
            listening.create_plan([self.source, path], self.root / "other-run", self.endpoint, "subscription", ["fixture"])
        with mock.patch.object(engine, "make_token_provider") as login:
            with self.assertRaises(FileNotFoundError):
                listening.render(self.plan, "example", "test-subscription", preview=False)
            login.assert_not_called()
            with self.assertRaisesRegex(ValueError, "Resource or subscription"):
                listening.render(self.plan, "different", "test-subscription", preview=True)
            login.assert_not_called()

    def test_single_encode_timing_and_full_decode_do_not_bypass_final_approval(self):
        plan = listening.load_plan(self.plan)
        passage = plan["passages"][0]
        with mock.patch.object(engine, "cached_pcm", return_value=b"\x00\x00" * (24_000 * 10)) as pcm:
            record = listening.render_one(self.plan, passage, lambda: "unused", mock.Mock())
            self.assertEqual(2, pcm.call_count)
        self.assertEqual(20_700, record["audio"]["durationMs"])
        self.assertEqual([{"id": "one", "startMs": 150, "endMs": 10_150}, {"id": "two", "startMs": 10_500, "endMs": 20_500}],
                         record["audio"]["turns"])
        with self.assertRaises(FileNotFoundError):
            listening.export(self.plan, self.content)
        self.assertFalse((self.content / "audio").exists())
        listening.approve(self.plan, None, "Synthetic fixture declaration, not a human review.", "2026-10-22")
        with self.assertRaises(FileNotFoundError):
            listening.export(self.plan, self.content)
        listening.approve(self.plan, "fixture", "Synthetic fixture declaration, not a human review.", "2026-10-22")
        listening.export(self.plan, self.content)
        saved = json.loads(self.source.read_text(encoding="utf-8"))
        self.assertEqual(record["audio"], saved["audio"])
        self.assertEqual(saved["audio"]["sha256"], saved["audioReview"]["audioSha256"])
        corrupted = self.plan.parent / "fixture.ogg"
        corrupted.write_bytes(corrupted.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, "Audio or render identity changed"):
            listening.verified_record(self.plan, passage)


if __name__ == "__main__":
    unittest.main()
