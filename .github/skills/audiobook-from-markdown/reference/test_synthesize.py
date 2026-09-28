"""Offline tests for synthesize.py: no network, no Azure login, no ffmpeg.

Run from this folder:  python test_synthesize.py

A fake HTTP session stands in for the TTS endpoint, so each test decides
which request lengths pass and how the others fail. Every test works in a
throwaway project folder (text/, audio/, tts-limits.json).
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.sax import saxutils

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import requests  # noqa: E402
from urllib3.exceptions import ReadTimeoutError  # noqa: E402

import synthesize as s  # noqa: E402

ENDPOINT_A = "https://example-a.cognitiveservices.azure.com/tts/cognitiveservices/v1"
ENDPOINT_B = "https://example-b.cognitiveservices.azure.com/tts/cognitiveservices/v1"
LENGTHS = "400,500,600,700,800"
WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()


def paragraph(n_words: int, offset: int = 0) -> str:
    words = [WORDS[(i + offset) % len(WORDS)] for i in range(n_words)]
    return " ".join(" ".join(words[i:i + 8]).capitalize() + "." for i in range(0, n_words, 8))


def chapter(title: str = "Chapter one", paragraphs: int = 6, words: int = 60) -> str:
    return f"# {title}\n\n" + "\n\n".join(paragraph(words, i) for i in range(paragraphs)) + "\n"


class Response:
    def __init__(self, status: int = 200, content: bytes = b"", text: str = "", headers: dict | None = None):
        self.status_code, self.content, self.text, self.headers = status, content, text, headers or {}


def audio(frames: int = 240) -> Response:
    return Response(200, s._wrap_wav(b"\x00\x00" * frames))


def read_timeout():
    raise requests.ReadTimeout("read timed out")


def body_read_timeout():
    # What requests raises when the headers arrive but the body stalls.
    raise requests.ConnectionError(ReadTimeoutError(None, None, "Read timed out."))


def dropped_response():
    raise requests.exceptions.ChunkedEncodingError("connection reset mid-body")


def connect_timeout():
    raise requests.ConnectTimeout("connect timed out")


def up_to(limit: int, over=lambda: Response(502, text="upstream connect error")):
    """Endpoint that accepts requests up to `limit` chars and fails longer ones via `over()`."""
    def reply(text: str) -> Response:
        return audio() if len(text) <= limit else over()
    return reply


class FakeSession:
    """Stands in for requests.Session: `reply(text)` returns a Response or raises."""

    def __init__(self, reply):
        self.reply = reply
        self.sent: list[str] = []

    def __call__(self):  # patched in as the requests.Session factory
        return self

    def post(self, url, headers=None, data=None, timeout=None):
        text = saxutils.unescape(re.sub(r"<[^>]+>", "", data.decode("utf-8")))
        self.sent.append(text)
        return self.reply(text)


class Project(unittest.TestCase):
    """A throwaway project folder, a scripted endpoint, and a main() runner."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.text_dir = self.root / "text"
        self.text_dir.mkdir()
        self.audio_dir = self.root / "audio"
        self.limits = self.root / "tts-limits.json"
        self.stems: list[str] = []
        self.providers = 0

        def provider():
            self.providers += 1
            return lambda: "test-token"

        def encode(wav_bytes, out_path, bitrate="128k"):
            out_path.write_bytes(b"mp3")

        self.tagged = mock.Mock()
        for name, value in (
            ("TEXT_DIR", self.text_dir), ("AUDIO_DIR", self.audio_dir), ("LIMITS_PATH", self.limits),
            ("NARRATED_STEMS", self.stems), ("VOICE", s.DEFAULT_VOICE), ("ENDPOINT", None),
            ("make_token_provider", provider), ("encode_mp3", encode), ("tag_mp3", self.tagged),
        ):
            patcher = mock.patch.object(s, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ("MAX_CHARS", "ENDPOINT", "RESOURCE", "VOICE"):
            os.environ.pop(f"{s.ENV_PREFIX}_{var}", None)
        sleep = mock.patch.object(s.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.tts = FakeSession(lambda text: audio())
        session = mock.patch.object(s.requests, "Session", self.tts)
        session.start()
        self.addCleanup(session.stop)

    def endpoint(self, reply):
        self.tts.reply = reply

    def write(self, stem: str, markdown: str) -> None:
        (self.text_dir / f"{stem}.md").write_text(markdown, encoding="utf-8")
        self.stems[:] = sorted(set(self.stems) | {stem})

    def main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["synthesize.py", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = s.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def saved(self) -> dict:
        return json.loads(self.limits.read_text(encoding="utf-8"))

    def key(self, endpoint: str = ENDPOINT_A, rate: str | None = None) -> str:
        return s._limits_key(s.DEFAULT_VOICE, endpoint, rate)


class RequestLimit(Project):
    """Where the limit comes from: --max-chars, then $TTS_MAX_CHARS, then the
    saved probe result for this voice, endpoint and rate, then the default."""

    def test_precedence(self):
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 500, 560)
        self.assertEqual(s.resolve_max_chars(None, s.DEFAULT_VOICE, ENDPOINT_A, None)[0], 500)
        os.environ[f"{s.ENV_PREFIX}_MAX_CHARS"] = "700"
        self.assertEqual(s.resolve_max_chars(None, s.DEFAULT_VOICE, ENDPOINT_A, None), (700, "$TTS_MAX_CHARS"))
        self.assertEqual(s.resolve_max_chars(300, s.DEFAULT_VOICE, ENDPOINT_A, None), (300, "--max-chars"))

    def test_saved_entry_names_its_probe_date(self):
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 500, 560)
        value, source = s.resolve_max_chars(None, s.DEFAULT_VOICE, ENDPOINT_A, None)
        self.assertEqual(value, 500)
        self.assertRegex(source, r"^tts-limits\.json, probed \d{4}-\d{2}-\d{2}$")

    def test_entry_applies_only_to_its_voice_endpoint_and_rate(self):
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 500, 560)
        default = (s.DEFAULT_BUDGET, "default")
        self.assertEqual(s.resolve_max_chars(None, s.DEFAULT_VOICE, ENDPOINT_B, None), default)
        self.assertEqual(s.resolve_max_chars(None, s.DEFAULT_VOICE, ENDPOINT_A, "0.9"), default)
        self.assertEqual(s.resolve_max_chars(None, "other-voice", ENDPOINT_A, None), default)
        same_host = ENDPOINT_A.replace("https://example-a", "HTTPS://EXAMPLE-A")
        self.assertEqual(s.resolve_max_chars(None, s.DEFAULT_VOICE, f" {same_host} ", None)[0], 500)
        for other in (ENDPOINT_A.replace("/v1", "/V1"), ENDPOINT_A + "/", ENDPOINT_A + "?region=x"):
            with self.subTest(other=other):
                self.assertEqual(s.resolve_max_chars(None, s.DEFAULT_VOICE, other, None), default)
        self.assertNotEqual(s.endpoint_id("https://User@example-a"), s.endpoint_id("https://user@example-a"))

    def test_no_endpoint_means_the_file_is_not_read(self):
        self.limits.write_text("not json", encoding="utf-8")
        value, source = s.resolve_max_chars(None, s.DEFAULT_VOICE, None, None)
        self.assertEqual(value, s.DEFAULT_BUDGET)
        self.assertIn("wasn't checked", source)

    def test_bad_values_fail_closed(self):
        for text in ("not json", "[1, 2]", json.dumps({self.key(): {"max_chars": "500"}}),
                     json.dumps({self.key(): {"max_chars": 0}}), json.dumps({self.key(): 500})):
            self.limits.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                s.resolve_max_chars(None, s.DEFAULT_VOICE, ENDPOINT_A, None)
        for env in ("abc", "0", "-5"):
            os.environ[f"{s.ENV_PREFIX}_MAX_CHARS"] = env
            with self.subTest(env=env), self.assertRaises(ValueError):
                s.resolve_max_chars(None, s.DEFAULT_VOICE, None, None)


class SaveLimit(Project):
    def test_merges_and_returns_the_replaced_entry(self):
        self.assertIsNone(s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 500, 560))
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_B, None, 800, 900)
        previous = s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 450, 510)
        self.assertEqual(previous["max_chars"], 500)
        data = self.saved()
        self.assertEqual(data[self.key(ENDPOINT_A)]["max_chars"], 450)
        self.assertEqual(data[self.key(ENDPOINT_B)]["max_chars"], 800)
        self.assertNotIn("example-", self.limits.read_text(encoding="utf-8"))  # a hash, never the URL
        self.assertEqual([p.name for p in self.root.iterdir() if p.is_file()], ["tts-limits.json"])

    def test_failed_write_leaves_no_temp_file_and_keeps_the_old_file(self):
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 500, 560)
        before = self.limits.read_text(encoding="utf-8")
        with mock.patch.object(s.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 450, 510)
        self.assertEqual(self.limits.read_text(encoding="utf-8"), before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.is_file()], ["tts-limits.json"])


class ProbeTexts(Project):
    def test_word_aligned_prefixes_without_titles(self):
        self.write("01-a", chapter("Title words here"))
        texts, used = s.probe_texts(["01-a"], [400, 500, 600])
        self.assertEqual(used, ["01-a"])
        self.assertEqual(len(texts), 3)
        body = " ".join(paragraph(60, i) for i in range(6))
        for text, target in zip(texts, (400, 500, 600)):
            self.assertLessEqual(len(text), target)
            self.assertTrue(body.startswith(text))
            self.assertIn(body[len(text):len(text) + 1], ("", " "))

    def test_reads_files_in_order_only_as_far_as_needed(self):
        self.write("01-a", chapter(paragraphs=6))
        self.write("02-b", chapter(paragraphs=6))
        self.assertEqual(s.probe_texts(["01-a", "02-b"], [400, 800])[1], ["01-a"])
        self.assertEqual(s.probe_texts(["01-a", "02-b"], [400, 3000])[1], ["01-a", "02-b"])

    def test_short_text_is_not_sent_twice(self):
        self.write("01-a", chapter(paragraphs=1, words=40))
        texts, _used = s.probe_texts(["01-a"], [400, 500, 600])
        self.assertEqual(len(texts), 1)


class Probe(Project):
    """probe_max_chars: which failures count as length, and which stop the probe."""

    def setUp(self):
        super().setUp()
        self.write("01-a", chapter())
        self.texts, _used = s.probe_texts(["01-a"], [400, 500, 600, 700, 800])
        self.lengths = [len(t) for t in self.texts]

    def probe(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = s.probe_max_chars(self.texts, lambda: "test-token", s.DEFAULT_VOICE, None)
        return result, out.getvalue()

    def test_limit_found_and_rechecked(self):
        self.endpoint(up_to(650))
        result, out = self.probe()
        self.assertEqual(result, s.ProbeResult(self.lengths[2], self.lengths[3], False))
        self.assertEqual(len(self.tts.sent), 2 + 2 + 2 + 1 + 1)  # 2 attempts per length, then one re-check
        self.assertEqual(self.tts.sent[-1], self.texts[2])
        self.assertIn("FAILED", out)
        self.sleep.assert_not_called()  # the probe never retries or backs off

    def test_size_rejections_count_as_length(self):
        for status in (413, 400):
            with self.subTest(status=status):
                self.tts.sent.clear()
                self.endpoint(up_to(650, lambda: Response(status, text="too long")))
                result, out = self.probe()
                self.assertEqual((result.passed, result.failed), (self.lengths[2], self.lengths[3]))
                self.assertIn("REJECTED", out)

    def test_other_length_failures(self):
        for name, over in (("read timeout", read_timeout), ("body read timeout", body_read_timeout),
                           ("dropped response", dropped_response), ("empty audio", lambda: audio(0)),
                           ("gateway timeout", lambda: Response(504))):
            with self.subTest(name):
                self.endpoint(up_to(650, over))
                result, _out = self.probe()
                self.assertEqual((result.passed, result.failed), (self.lengths[2], self.lengths[3]))

    def test_errors_that_are_not_about_length_stop_the_probe(self):
        def unreachable():
            raise requests.ConnectionError("name not resolved")
        for name, over, error in (
            ("throttled", lambda: Response(429), s.TransientExhaustionError),
            ("unauthorized", lambda: Response(401), s.PermanentSynthesisError),
            ("unreachable", unreachable, s.PermanentSynthesisError),
            ("connect timeout", connect_timeout, s.PermanentSynthesisError),
            ("not audio", lambda: Response(200, b"<html>"), s.PermanentSynthesisError),
        ):
            with self.subTest(name):
                self.endpoint(up_to(650, over))
                with self.assertRaises(error):
                    self.probe()

    def test_400_at_the_shortest_length_is_not_a_size_limit(self):
        self.endpoint(lambda text: Response(400, text="bad voice name"))
        with self.assertRaises(s.PermanentSynthesisError):
            self.probe()

    def test_second_attempt_failure_counts(self):
        calls = {self.texts[3]: 0}

        def reply(text):
            if text == self.texts[3]:
                calls[text] += 1
                return audio() if calls[text] == 1 else Response(502)
            return audio() if len(text) <= self.lengths[3] else Response(502)
        self.endpoint(reply)
        result, _out = self.probe()
        self.assertEqual((result.passed, result.failed), (self.lengths[2], self.lengths[3]))

    def test_recheck_failure_is_reported(self):
        self.endpoint(lambda text: audio() if len(self.tts.sent) <= 6 else Response(502))
        result, _out = self.probe()
        self.assertEqual(result, s.ProbeResult(self.lengths[2], self.lengths[3], True))

    def test_everything_passes(self):
        result, _out = self.probe()
        self.assertEqual(result, s.ProbeResult(self.lengths[-1], None))

    def test_shortest_fails(self):
        self.endpoint(up_to(100))
        result, _out = self.probe()
        self.assertEqual(result, s.ProbeResult(None, self.lengths[0]))


class ProbeCommand(Project):
    """synthesize.py --probe-max-chars: what gets saved, and when nothing is."""

    def setUp(self):
        super().setUp()
        self.write("01-a", chapter())

    def probe(self, *extra: str) -> tuple[int, str]:
        return self.main("--probe-max-chars", "--endpoint", ENDPOINT_A, "--probe-lengths", LENGTHS, *extra)

    def test_saves_margin_under_the_longest_pass(self):
        self.endpoint(up_to(650))
        code, out = self.probe()
        self.assertEqual(code, 0, out)
        entry = self.saved()[self.key()]
        self.assertLessEqual(entry["longest_passed"], 650)
        self.assertEqual(entry["max_chars"], int(entry["longest_passed"] * s.PROBE_MARGIN))
        self.assertIn(str(self.limits), out)
        self.assertIn("text from 01-a", out)

    def test_reprobe_reports_the_replaced_value(self):
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 1234, 1400)
        self.endpoint(up_to(650))
        code, out = self.probe()
        self.assertEqual(code, 0, out)
        self.assertIn("replacing 1234", out)

    def test_rate_is_part_of_the_key(self):
        self.endpoint(up_to(650))
        self.assertEqual(self.probe("--rate=0.9")[0], 0)
        self.assertEqual(list(self.saved()), [self.key(rate="0.9")])

    def test_no_limit_found_saves_nothing(self):
        code, out = self.probe()
        self.assertEqual(code, 0, out)
        self.assertIn("no limit found", out)
        self.assertIn(f"default {s.DEFAULT_BUDGET}", out)
        self.assertFalse(self.limits.exists())
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 1234, 1400)
        before = self.limits.read_text(encoding="utf-8")
        code, out = self.probe()
        self.assertEqual(code, 0, out)
        self.assertIn("1234", out)
        self.assertIn("stays in effect", out)
        self.assertEqual(self.limits.read_text(encoding="utf-8"), before)
        self.assertEqual(s.resolve_max_chars(None, s.DEFAULT_VOICE, ENDPOINT_A, None)[0], 1234)
        for name, content, message in (
            ("scalar entry", json.dumps({self.key(): 500}), "fix or delete that entry"),
            ("zero limit", json.dumps({self.key(): {"max_chars": 0, "probed": "2026-01-01"}}),
             "fix or delete that entry"),
        ):
            with self.subTest(name):
                self.limits.write_text(content, encoding="utf-8")
                code, out = self.probe()
                self.assertEqual(code, 0, out)
                self.assertIn("runs stop on the saved limit", out)
                self.assertIn(message, out)
                self.assertNotIn("stays in effect", out)
                self.assertEqual(self.limits.read_text(encoding="utf-8"), content)

    def test_says_when_the_env_overrides_the_saved_limit(self):
        os.environ[f"{s.ENV_PREFIX}_MAX_CHARS"] = "500"
        self.endpoint(up_to(650))
        code, out = self.probe()
        self.assertEqual(code, 0, out)
        self.assertIn(f"${s.ENV_PREFIX}_MAX_CHARS=500 is set", out)

    def test_nothing_saved_on_failure(self):
        cases = (
            ("shortest fails", up_to(100), "--probe-lengths 100,200,300"),
            ("re-check fails", lambda text: audio() if len(self.tts.sent) <= 6 else Response(502),
             "aren't tied to length"),
            ("unauthorized", lambda text: Response(401), "isn't about length"),
            ("throttled", up_to(650, lambda: Response(429)), "wait a few minutes"),
        )
        for name, reply, message in cases:
            with self.subTest(name):
                self.endpoint(reply)
                self.tts.sent.clear()
                code, out = self.probe()
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)
                self.assertFalse(self.limits.exists())

    def test_save_failure_says_how_to_pass_the_limit(self):
        self.endpoint(up_to(650))
        with mock.patch.object(s, "save_limit", side_effect=OSError("read-only")):
            code, out = self.probe()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"Pass --max-chars \d+")

    def test_refuses_flags_it_would_ignore(self):
        for flags in (["--dry-run"], ["--tag-only"], ["--force"], ["--limit-chunks", "1"], ["--max-chars", "500"]):
            with self.subTest(flags=flags):
                code, _out = self.probe(*flags)
                self.assertEqual(code, 2)
        self.assertEqual(self.main("--probe-lengths", LENGTHS)[0], 2)
        self.assertEqual(self.main("--probe-max-chars")[0], 2)  # no endpoint
        self.assertEqual(self.tts.sent, [])

    def test_unreadable_limits_file_stops_before_sending(self):
        self.limits.write_text("{", encoding="utf-8")
        code, _out = self.probe()
        self.assertEqual(code, 2)
        self.assertEqual(self.tts.sent, [])

    def test_bad_limit_does_not_block_probe_or_tagging(self):
        self.endpoint(up_to(650))
        os.environ[f"{s.ENV_PREFIX}_MAX_CHARS"] = "abc"
        self.assertEqual(self.probe()[0], 0)
        self.assertEqual(self.main("--tag-only")[0], 0)
        self.assertEqual(self.main("--dry-run")[0], 2)
        del os.environ[f"{s.ENV_PREFIX}_MAX_CHARS"]
        self.limits.write_text(json.dumps({self.key(): 500}), encoding="utf-8")
        self.assertEqual(self.main("--tag-only")[0], 0)
        code, out = self.main("--dry-run", "--endpoint", ENDPOINT_A)
        self.assertEqual(code, 2)
        self.assertIn("fix or delete that entry", out)
        self.assertEqual(self.probe()[0], 0)  # the probe replaces the bad entry
        self.assertIsInstance(self.saved()[self.key()]["max_chars"], int)


class Run(Project):
    """A real run: the planned chunks are the ones checked and the ones sent."""

    def test_uses_the_saved_limit(self):
        self.write("01-a", chapter(paragraphs=3, words=200))
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 500, 560)
        self.endpoint(up_to(560))
        code, out = self.main("--endpoint", ENDPOINT_A)
        self.assertEqual(code, 0, out)
        self.assertLessEqual(max(len(t) for t in self.tts.sent), 500)
        self.assertIn("max 500 chars (tts-limits.json, probed", out)
        self.assertNotIn("WARNING", out)
        self.assertTrue((self.audio_dir / "01-a.mp3").exists())
        self.tagged.assert_called_once()

    def test_warns_without_a_saved_limit_for_this_endpoint(self):
        self.write("01-a", chapter())
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 500, 560)
        code, out = self.main("--endpoint", ENDPOINT_B)
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: no saved request limit", out)
        self.assertIn("Before a full batch, run --probe-max-chars with the same --voice/--rate", out)

    def test_over_long_title_stops_before_login(self):
        self.write("01-a", chapter(title="A heading that is far too long to fit in one short request"))
        code, out = self.main("--endpoint", ENDPOINT_A, "--max-chars", "50")
        self.assertEqual(code, 2)
        self.assertIn("titles are never split", out)
        self.assertEqual((self.providers, self.tts.sent), (0, []))

    def test_guard_checks_only_what_will_be_sent(self):
        long_heading = "\n\n## A subheading that is far too long to fit in one short request\n\n" + paragraph(8)
        self.write("01-a", chapter(paragraphs=2, words=8) + long_heading)
        self.write("02-b", chapter(title="A heading that is far too long to fit in one short request"))
        (self.audio_dir).mkdir()
        (self.audio_dir / "02-b.mp3").write_bytes(b"done")
        code, out = self.main("--endpoint", ENDPOINT_A, "--max-chars", "50", "--limit-chunks", "2", "01-a")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.tts.sent), 2)
        self.assertTrue((self.audio_dir / "01-a.smoke.mp3").exists())
        self.tagged.assert_not_called()
        code, out = self.main("--endpoint", ENDPOINT_A, "--max-chars", "50", "02-b")
        self.assertEqual(code, 0, out)
        self.assertIn("skip (exists)", out)

    def test_unreadable_file_is_reported_in_order(self):
        self.write("01-a", chapter())
        code, out = self.main("--endpoint", ENDPOINT_A, "01-a", "99-missing")
        self.assertEqual(code, 1)
        self.assertLess(out.index("done: 01-a.mp3"), out.index("FAILED: 99-missing"))

    def test_size_errors_and_read_timeouts_split_the_text(self):
        for name, over in (("413", lambda: Response(413)), ("read timeout", read_timeout),
                           ("body read timeout", body_read_timeout), ("dropped response", dropped_response)):
            with self.subTest(name):
                self.write("01-a", chapter(paragraphs=1, words=60))
                self.tts.sent.clear()
                self.endpoint(up_to(200, over))
                code, out = self.main("--endpoint", ENDPOINT_A, "--force")
                self.assertEqual(code, 0, out)
                self.assertIn("retry-split", out)

    def test_other_errors_do_not_split(self):
        self.write("01-a", chapter(paragraphs=1, words=60))
        for name, reply, requests_sent in (("401", lambda text: Response(401), 1),
                                           ("connect timeout", lambda text: connect_timeout(), 3)):
            with self.subTest(name):
                self.tts.sent.clear()
                self.endpoint(reply)
                code, out = self.main("--endpoint", ENDPOINT_A)
                self.assertEqual(code, 1)
                self.assertNotIn("retry-split", out)
                self.assertEqual(len(self.tts.sent), requests_sent)  # retries, but never a shorter text


class DryRun(Project):
    def test_reads_the_saved_limit_only_with_an_endpoint(self):
        self.write("01-a", chapter(paragraphs=3, words=200))
        s.save_limit(s.DEFAULT_VOICE, ENDPOINT_A, None, 500, 560)
        code, out = self.main("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("no endpoint set, so tts-limits.json wasn't checked", out)
        code, out = self.main("--dry-run", "--endpoint", ENDPOINT_A)
        self.assertEqual(code, 0, out)
        self.assertIn("max 500 chars (tts-limits.json, probed", out)
        prepared = (self.audio_dir / "01-a.txt").read_text(encoding="utf-8")
        self.assertLessEqual(max(len(line) for line in prepared.splitlines()), 500)
        self.assertEqual(self.tts.sent, [])

    def test_flags_an_over_long_title(self):
        self.write("01-a", chapter(title="A heading that is far too long to fit in one short request"))
        code, out = self.main("--dry-run", "--max-chars", "50")
        self.assertEqual(code, 0)
        self.assertIn("OVER the 50-char limit: 01-a chunk 1 (title", out)


SKILL_MD = HERE.parent / "SKILL.md"


@unittest.skipUnless(SKILL_MD.exists(), "SKILL.md isn't next to this folder")
class WorkedExample(Project):
    """SKILL.md's worked example is real output: re-run it and compare."""

    def test_matches_skill_md(self):
        section = SKILL_MD.read_text(encoding="utf-8").split("## Worked example walkthrough", 1)[1]
        source, printed, prepared = re.findall(r"```[a-z]*\n(.*?)```", section, re.S)[:3]
        self.write("01-glava-1", source)
        code, out = self.main("--dry-run", "01-glava-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), printed.strip())
        self.assertEqual((self.audio_dir / "01-glava-1.txt").read_text(encoding="utf-8").strip(), prepared.strip())


if __name__ == "__main__":
    unittest.main()
