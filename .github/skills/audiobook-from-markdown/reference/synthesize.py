"""Generate audiobook MP3s from a folder of clean Markdown files using an Azure
neural TTS voice (e.g. MAI-Voice-2) over the real-time Speech endpoint.

Pipeline per file: parse Markdown -> chunk under the TTS request limit ->
synthesize each chunk as PCM (Microsoft Entra / AAD auth) -> stitch the PCM
losslessly with controlled silences -> encode one MP3 -> tag it with ID3
metadata (title, artist, album, year, track number).

Synthesizing PCM and encoding a single MP3 at the end (rather than
concatenating per-chunk MP3s) avoids MP3 frame-boundary gaps, so the result is
seamless.

Authentication uses your Azure login (`az login` / Azure CLI credential); no
keys are used or stored. Configure the book-specific values in the CONFIG
section below, then override the endpoint/voice/resource via environment
variables or CLI flags (never hardcode a resource name -- it's user-specific).

Examples
--------
    python synthesize.py --probe-max-chars            # measure this voice's request-size limit
    python synthesize.py --dry-run --all              # inspect text prep, no API calls
    python synthesize.py --all                        # every file in text/
    python synthesize.py 03-chapter-3 --limit-chunks 2 # smoke test one file

--probe-max-chars saves a recommended limit per voice, endpoint and --rate to
tts-limits.json in the project root (the folder above this script's), and every
later run with the same voice, endpoint and --rate uses it; --max-chars N or
$TTS_MAX_CHARS overrides it. Don't edit chunk_text.DEFAULT_BUDGET instead.
Offline tests: python test_synthesize.py
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import wave
import xml.sax.saxutils as saxutils
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

import requests
from urllib3.exceptions import ReadTimeoutError

from chunk_text import DEFAULT_BUDGET, DEFAULT_PAUSES, Chunk, chunk_segments, split_sentences
from clean_text import parse_markdown

REPO_ROOT = Path(__file__).resolve().parent.parent
TEXT_DIR = REPO_ROOT / "text"
AUDIO_DIR = REPO_ROOT / "audio"

# =============================================================================
# CONFIG -- fill in for your book. Everything below this section is generic.
# =============================================================================

LANGUAGE = "ru"  # must match a key in clean_text.HEADING_PATTERNS / chunk_text.LANGUAGE_ABBREVIATIONS
XML_LANG = "ru-RU"  # SSML xml:lang

ID3_ARTIST = "CHANGE ME"
ID3_ALBUM = "CHANGE ME (book title)"
ID3_YEAR = "CHANGE ME"
ID3_GENRE = "Audiobook"

DEFAULT_VOICE = "ru-RU-Lev:MAI-Voice-2"
# Environment variable / CLI-flag prefix for endpoint resolution -- rename per
# project if you like, just keep it consistent with your README/SKILL usage.
ENV_PREFIX = "TTS"

# Every file to narrate, in album order -- the position in this list doubles
# as the ID3 track number (see the ID3 tagging convention in SKILL.md: 00 is
# reserved for preface/introduction, continuing sequentially through every
# chapter and appendix). Defaults to every .md file in TEXT_DIR sorted by
# name (matching a "00-preface, 01-chapter-1, ..." naming convention) --
# override with an explicit list if your files aren't named that way, or to
# exclude reference/bibliography files that shouldn't be narrated.
NARRATED_STEMS = sorted(p.stem for p in TEXT_DIR.glob("*.md")) if TEXT_DIR.exists() else []

# =============================================================================
# Track title derivation (ID3 TIT2) -- generic Roman-numeral-to-Arabic-digit
# rewriting, since ID3 text is read on a screen (plain digits read better in a
# player's UI) while the *spoken* audio uses the ordinal-word form via
# clean_text.spoken_heading() (e.g. "Глава третья"). The two intentionally
# differ -- see the ID3 tagging convention table in SKILL.md.
# =============================================================================

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_ROMAN_RE = re.compile(r"^[IVXLCDM]+$")


def _roman_to_arabic(roman: str) -> int | None:
    if not _ROMAN_RE.match(roman):
        return None
    total = 0
    prev = 0
    for ch in reversed(roman):
        value = _ROMAN_VALUES[ch]
        total += -value if value < prev else value
        prev = max(prev, value)
    return total


def track_title(stem: str) -> str:
    """Derive the ID3 track title from the .md file's own H1 heading (single
    source of truth -- never hand-duplicated), rewriting a bare Roman-numeral
    label into Arabic digits.

    Reuses clean_text.HEADING_PATTERNS -- the same table spoken_heading() uses
    to recognize a numbered heading -- so the heading FORMAT only needs to be
    described once. Only the rendering differs: plain Arabic digits here
    (ID3 text is read on a screen), vs. an ordinal word in spoken_heading()
    (the audio is heard, not read). If these used two independently-guessed
    formats, a heading convention change would silently desync the tag from
    the narration.
    """
    from clean_text import HEADING_PATTERNS

    first_line = (TEXT_DIR / f"{stem}.md").read_text(encoding="utf-8").splitlines()[0]
    heading = first_line.lstrip("#").strip()
    for pattern, lead_word, _ordinals in HEADING_PATTERNS.get(LANGUAGE, []):
        m = pattern.match(heading)
        if m:
            numeral, rest = m.group(1), m.group(2)
            arabic = _roman_to_arabic(numeral)
            if arabic is not None:
                rest = f" {rest}" if rest else ""
                return f"{lead_word} {arabic}.{rest}"
    return heading  # no numbered heading pattern matched; use as-is (e.g. Preface/Conclusion)


# =============================================================================
# Endpoint resolution -- never hardcode a resource; it is always user/project
# specific. Resolved, in order, from an explicit URL/resource CLI flag, then
# $TTS_ENDPOINT / $TTS_RESOURCE (or your chosen ENV_PREFIX).
# =============================================================================

ENDPOINT_TEMPLATE = "https://{resource}.cognitiveservices.azure.com/tts/cognitiveservices/v1"


def resolve_endpoint(resource: str | None = None, endpoint: str | None = None) -> str | None:
    endpoint = endpoint or os.environ.get(f"{ENV_PREFIX}_ENDPOINT")
    if endpoint:
        return endpoint
    resource = resource or os.environ.get(f"{ENV_PREFIX}_RESOURCE")
    if resource:
        return ENDPOINT_TEMPLATE.format(resource=resource)
    return None


ENDPOINT = resolve_endpoint()
VOICE = os.environ.get(f"{ENV_PREFIX}_VOICE", DEFAULT_VOICE)


# Per-request size limits measured by --probe-max-chars, keyed by voice,
# endpoint and --rate. Holds voice names, an 8-hex-digit hash of the endpoint
# URL (never the URL itself) and numbers -- no secrets. Run one probe at a
# time: two probes saving at once can drop one of the entries.
LIMITS_PATH = REPO_ROOT / "tts-limits.json"
# Near the limit, failures are intermittent, so a length can pass both probe
# attempts by chance; the saved limit keeps this margin under the longest pass.
PROBE_MARGIN = 0.9


def endpoint_id(endpoint: str) -> str:
    # Only the scheme and host are case-insensitive; the path and query are
    # kept as given, so two different endpoints never share a saved limit.
    parts = urlsplit(endpoint.strip())
    userinfo, at, host = parts.netloc.rpartition("@")
    canonical = urlunsplit((parts.scheme.lower(), userinfo + at + host.lower(), parts.path, parts.query,
                            parts.fragment))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def _limits_key(voice: str, endpoint: str, rate: str | None) -> str:
    key = f"{voice} @{endpoint_id(endpoint)}"
    return f"{key} rate={rate}" if rate else key


def load_limits() -> dict:
    if not LIMITS_PATH.exists():
        return {}
    try:
        data = json.loads(LIMITS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{LIMITS_PATH} is unreadable: {exc}; fix or delete it, then re-probe") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{LIMITS_PATH} must hold a JSON object; fix or delete it, then re-probe")
    return data


def save_limit(voice: str, endpoint: str, rate: str | None, max_chars: int, longest_passed: int) -> dict | None:
    """Save the entry and return the one it replaced, if any."""
    data = load_limits()
    key = _limits_key(voice, endpoint, rate)
    previous = data.get(key)
    data[key] = {
        "max_chars": max_chars,
        "longest_passed": longest_passed,
        "probed": time.strftime("%Y-%m-%d", time.gmtime()),
    }
    fd, tmp_name = tempfile.mkstemp(dir=LIMITS_PATH.parent, prefix=LIMITS_PATH.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        os.replace(tmp_name, LIMITS_PATH)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return previous if isinstance(previous, dict) else None


def saved_limit(voice: str, endpoint: str, rate: str | None) -> tuple[int, str] | None:
    """The limit --probe-max-chars saved for this voice, endpoint and rate,
    with its source label; None if there is none. Raises ValueError if the
    file or the entry is bad."""
    limits = load_limits()
    key = _limits_key(voice, endpoint, rate)
    if key not in limits:
        return None
    entry = limits[key]
    value = entry.get("max_chars") if isinstance(entry, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{LIMITS_PATH}: bad entry for {key!r}: {entry!r}; fix or delete that entry, then re-probe")
    probed = entry.get("probed")
    return value, (f"{LIMITS_PATH.name}, probed {probed}" if isinstance(probed, str) else LIMITS_PATH.name)


def resolve_max_chars(cli_value: int | None, voice: str, endpoint: str | None, rate: str | None) -> tuple[int, str]:
    """Longest text sent in one request, and where that value came from:
    --max-chars, then $TTS_MAX_CHARS (or your ENV_PREFIX), then the limit
    --probe-max-chars saved for this voice, endpoint and rate in
    tts-limits.json, then chunk_text.DEFAULT_BUDGET. The real limit depends on
    the voice/model and endpoint, so it is measured and passed in, never
    edited into the code. With no endpoint (a dry run), the file can't be
    looked up."""
    env_name = f"{ENV_PREFIX}_MAX_CHARS"
    env = os.environ.get(env_name)
    if cli_value is not None:
        value, source = cli_value, "--max-chars"
    elif env not in (None, ""):
        try:
            value = int(env)
        except ValueError:
            raise ValueError(f"${env_name} must be an integer, got {env!r}") from None
        source = f"${env_name}"
    elif endpoint is None:
        value, source = DEFAULT_BUDGET, f"default; no endpoint set, so {LIMITS_PATH.name} wasn't checked"
    else:
        value, source = saved_limit(voice, endpoint, rate) or (DEFAULT_BUDGET, "default")
    if value <= 0:
        raise ValueError(f"{source}: max chars must be positive, got {value}")
    return value, source


SCOPE = "https://cognitiveservices.azure.com/.default"

PCM_FORMAT = "riff-24khz-16bit-mono-pcm"
SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2  # bytes (16-bit)
HEAD_SILENCE_MS = 300
TAIL_SILENCE_MS = 600


# --- authentication ----------------------------------------------------------

def make_token_provider():
    """Return a callable that yields a cached, auto-refreshing AAD token."""
    from azure.identity import AzureCliCredential, DefaultAzureCredential

    try:
        # process_timeout=30: the default 10s can expire when `az` is cold,
        # especially on Windows.
        credential = AzureCliCredential(process_timeout=30)
        credential.get_token(SCOPE)  # validate up front
    except Exception:
        credential = DefaultAzureCredential(process_timeout=30)

    cache = {"token": None, "expires": 0.0}

    def get_token() -> str:
        if cache["token"] is None or cache["expires"] - time.time() < 300:
            result = credential.get_token(SCOPE)
            cache["token"] = result.token
            cache["expires"] = result.expires_on
        return cache["token"]

    return get_token


# --- SSML + synthesis ---------------------------------------------------------

def build_ssml(text: str, voice: str, rate: str | None) -> str:
    inner = saxutils.escape(text)
    if rate:
        inner = f"<prosody rate={saxutils.quoteattr(rate)}>{inner}</prosody>"
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="{XML_LANG}">'
        f"<voice name={saxutils.quoteattr(voice)}>{inner}</voice></speak>"
    )


class PermanentSynthesisError(RuntimeError):
    """A failure that retrying -- or splitting the text and retrying the
    pieces -- cannot fix: bad credentials/permissions, a malformed request,
    an unreachable/misconfigured endpoint, or a response in an unexpected
    format. synth_pcm_resilient() re-raises these immediately (except a 413,
    which is about length) instead of recursively splitting the text:
    splitting a 401 into smaller 401s just burns API calls and delays the
    real error surfacing to the user.
    `status` is the HTTP status, or None for a network or format failure."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TransientExhaustionError(RuntimeError):
    """Raised when synth_pcm()'s own retry-with-backoff loop exhausts every
    attempt on a transient HTTP response (408/429/500/502/503/504), or on a
    read timeout or dropped response from a *reachable* endpoint. This is the failure
    synth_pcm_resilient() treats as worth splitting the text and retrying the
    halves -- see its docstring for why (empirically, a different/smaller
    request often lands on a healthy backend instance behind the same
    endpoint, which is not true of a genuinely broken endpoint or bad
    credentials). `status` is the last HTTP status, or None for a read
    timeout or dropped response."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _pcm_from_wav(data: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (
                SAMPLE_RATE, 1, SAMPLE_WIDTH,
            ):
                raise PermanentSynthesisError(
                    f"unexpected PCM format: {wav.getframerate()}Hz "
                    f"{wav.getnchannels()}ch {wav.getsampwidth() * 8}bit"
                )
            return wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise PermanentSynthesisError(f"response is not a WAV file: {exc}") from exc


def _failed_mid_response(exc: requests.RequestException) -> bool:
    """Connected, but no full response: a read timeout (requests raises one
    while reading the body as a ConnectionError wrapping urllib3's
    ReadTimeoutError) or a connection dropped mid-body."""
    if isinstance(exc, (requests.ReadTimeout, requests.exceptions.ChunkedEncodingError)):
        return True
    return isinstance(exc, requests.ConnectionError) and any(isinstance(a, ReadTimeoutError) for a in exc.args)


def synth_pcm(
    ssml: str,
    get_token,
    session: requests.Session,
    max_retries: int = 8,
) -> bytes:
    """Synthesize one SSML chunk to raw PCM, with backoff on transient errors.

    Preview TTS endpoints can return transient 502/503 gateway errors
    ("upstream connect error ... protocol error"). Retries use capped
    exponential backoff with jitter, but stay short (a few attempts, seconds
    not minutes) -- if a failure persists at this size, the caller
    (synth_pcm_resilient) falls back to splitting the text instead of waiting
    here indefinitely, since that has proven the more effective fix (see
    SKILL.md, "Why split-and-retry, not just more retries").

    Raises TransientExhaustionError only when every attempt failed with a
    transient HTTP status (408/429/500/502/503/504), a read timeout or a
    dropped response -- the case worth splitting the text for (a 413 is the
    other; synth_pcm_resilient splits it). Everything else (a non-transient
    HTTP status such as 401/403/400/404, or a network/connectivity failure
    such as a bad hostname) raises PermanentSynthesisError instead: a broken
    endpoint or bad credentials fails identically no matter how small the
    request is, so splitting the text cannot help and would only multiply
    doomed calls.
    """
    headers = {
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": PCM_FORMAT,
        "User-Agent": "audiobook-from-markdown",
    }
    body = ssml.encode("utf-8")
    for attempt in range(max_retries):
        headers["Authorization"] = "Bearer " + get_token()
        try:
            resp = session.post(ENDPOINT, headers=headers, data=body, timeout=180)
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                if _failed_mid_response(exc):
                    # Like a 504 from a reachable endpoint: transient, and
                    # worth splitting.
                    raise TransientExhaustionError(
                        f"TTS request failed after {max_retries} attempts: read timeout or dropped response: {exc}"
                    ) from exc
                # Connectivity-level failure (DNS, refused connection, TLS,
                # connect timeout). A couple of retries absorb a brief blip,
                # but persistent failure here means the endpoint itself is
                # unreachable/misconfigured, not a single unhealthy backend
                # instance -- so exhaustion is PermanentSynthesisError, not the
                # splittable TransientExhaustionError.
                raise PermanentSynthesisError(
                    f"TTS request failed after {max_retries} attempts: network error: {exc}"
                ) from exc
            time.sleep(min(2 ** attempt, 12) * (0.8 + 0.4 * random.random()))
            continue
        if resp.status_code == 200:
            return _pcm_from_wav(resp.content)
        detail = f"{resp.status_code} {resp.text[:300]}"
        if resp.status_code not in (408, 429, 500, 502, 503, 504):
            raise PermanentSynthesisError(f"TTS request failed: {detail}", resp.status_code)
        if attempt == max_retries - 1:
            raise TransientExhaustionError(
                f"TTS request failed after {max_retries} attempts: {detail}", resp.status_code
            )
        retry_after = resp.headers.get("Retry-After")
        base = float(retry_after) if retry_after else min(2 ** attempt, 12)
        time.sleep(base * (0.8 + 0.4 * random.random()))
    raise PermanentSynthesisError("exhausted retries")  # unreachable: loop always returns or raises above


# Below this length, give up on the split-and-retry fallback and surface the
# error -- splitting single words apart would be pointless and could loop
# forever on a two-word chunk.
MIN_SPLIT_CHARS = 60


def synth_pcm_resilient(
    text: str,
    get_token,
    session: requests.Session,
    voice: str,
    rate: str | None,
    max_retries: int = 3,
) -> bytes:
    """Synthesize `text`, and if it keeps failing even after retries, fall
    back to recursively splitting it in half (at a sentence boundary, or a
    word boundary if it is a single sentence) and stitching the pieces.

    This is the key resilience pattern for a flaky endpoint (see SKILL.md).
    In the first production run, transient failures did not correlate
    cleanly with chunk length or specific content -- verified by bisecting a
    reliably-failing ~1100-char paragraph down to sub-100-char fragments that
    synthesized fine individually, while some unrelated short chunks failed
    just as persistently. That looks like backend-instance flakiness rather
    than a deterministic client-side trigger: retrying the exact same request
    just re-hits the same bad state, but a *different* (smaller) request often
    succeeds, so splitting is a pragmatic, effective workaround regardless of
    the exact root cause. A later run on the same voice did show a length
    threshold, though; that is handled up front by the measured request limit
    (--probe-max-chars saves it, --max-chars overrides it), and splitting
    stays the backstop.

    Only catches TransientExhaustionError, and a 413 (payload too large,
    which a shorter request does fix) -- deliberately NOT a bare
    `except Exception`. Any other permanent failure (bad credentials, an
    unreachable/misconfigured endpoint, a malformed request, an unexpected
    response format -- all raised as PermanentSynthesisError) fails exactly
    the same way no matter how small the request is, so splitting it would
    just multiply doomed API calls before the real error finally surfaces.
    PermanentSynthesisError (and anything else unexpected) propagates
    immediately instead.
    """
    ssml = build_ssml(text, voice, rate)
    try:
        return synth_pcm(ssml, get_token, session, max_retries=max_retries)
    except (TransientExhaustionError, PermanentSynthesisError) as exc:
        if isinstance(exc, PermanentSynthesisError) and exc.status != 413:
            raise
        if len(text) <= MIN_SPLIT_CHARS:
            raise
        sentences = split_sentences(text, LANGUAGE)
        if len(sentences) > 1:
            mid = len(sentences) // 2
            left = " ".join(sentences[:mid])
            right = " ".join(sentences[mid:])
        else:
            cut = text.rfind(" ", 0, len(text) // 2 + 1)
            if cut <= 0:
                raise
            left, right = text[:cut].strip(), text[cut:].strip()
        print(
            f"    retry-split ({len(text)} chars -> {len(left)}+{len(right)}) "
            "after repeated failures",
            flush=True,
        )
        left_pcm = synth_pcm_resilient(left, get_token, session, voice, rate, max_retries)
        right_pcm = synth_pcm_resilient(right, get_token, session, voice, rate, max_retries)
        return left_pcm + _silence(DEFAULT_PAUSES["cont"]) + right_pcm


def _silence(ms: int) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * ms / 1000)


def _wrap_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _pcm_seconds(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)


# --- encoding ------------------------------------------------------------------

def _ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def encode_mp3(wav_bytes: bytes, out_path: Path, bitrate: str = "128k") -> None:
    # Encode to a temp file and atomically rename on success, so an
    # interrupted encode never leaves a truncated MP3 at the resumable output
    # path (which the skip-if-exists resume logic would then treat as done).
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    cmd = [
        _ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "wav", "-i", "pipe:0",
        "-codec:a", "libmp3lame", "-b:a", bitrate,
        "-f", "mp3", str(tmp_path),
    ]
    proc = subprocess.run(cmd, input=wav_bytes, capture_output=True)
    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:400]}")
    os.replace(tmp_path, out_path)


# --- ID3 tagging ---------------------------------------------------------------

def tag_mp3(path: Path, stem: str, track: int, total: int) -> None:
    """Apply ID3v2.3 tags: title (per-file), artist/album/year/genre (fixed
    book-level values from CONFIG), and a zero-padded track number (see the
    ID3 tagging convention in SKILL.md)."""
    from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    for frame in ("TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TDRC", "TCON"):
        tags.delall(frame)
    tags.add(TIT2(encoding=3, text=track_title(stem)))
    tags.add(TPE1(encoding=3, text=ID3_ARTIST))
    tags.add(TPE2(encoding=3, text=ID3_ARTIST))
    tags.add(TALB(encoding=3, text=ID3_ALBUM))
    tags.add(TRCK(encoding=3, text=f"{track:02d}/{total}"))
    tags.add(TDRC(encoding=3, text=ID3_YEAR))
    tags.add(TCON(encoding=3, text=ID3_GENRE))
    tags.save(path, v2_version=3)


# --- per-file driver ------------------------------------------------------------

def load_chunks(stem: str, max_chars: int = DEFAULT_BUDGET) -> list[Chunk]:
    md_path = TEXT_DIR / f"{stem}.md"
    segments = parse_markdown(md_path.read_text(encoding="utf-8"), LANGUAGE)
    return chunk_segments(segments, budget=max_chars, language=LANGUAGE)


def oversized_chunks(planned: list[tuple[str, list[Chunk]]], max_chars: int) -> list[str]:
    """Chunks longer than max_chars. Paragraphs are always split to fit, but
    titles never are, so only an over-long title can show up here."""
    return [
        f"{stem} chunk {index} ({chunk.kind}, {len(chunk.text)} chars)"
        for stem, chunks in planned
        for index, chunk in enumerate(chunks, 1)
        if len(chunk.text) > max_chars
    ]


def synthesize_file(
    chunks: list[Chunk],
    out_path: Path,
    get_token,
    voice: str,
    rate: str | None,
) -> float:
    session = requests.Session()
    pcm = bytearray(_silence(HEAD_SILENCE_MS))
    for index, chunk in enumerate(chunks, 1):
        if chunk.pre_pause_ms:
            pcm += _silence(chunk.pre_pause_ms)
        pcm += synth_pcm_resilient(chunk.text, get_token, session, voice, rate)
        print(f"    chunk {index}/{len(chunks)} ({chunk.kind}, {len(chunk.text)} chars) ok", flush=True)
    pcm += _silence(TAIL_SILENCE_MS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encode_mp3(_wrap_wav(bytes(pcm)), out_path)
    return _pcm_seconds(bytes(pcm))


def dry_run_file(stem: str, out_path: Path, chunks: list[Chunk]) -> tuple[int, int]:
    total_chars = sum(len(c.text) for c in chunks)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path = out_path.with_suffix(".txt")
    with txt_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            prefix = "## " if chunk.kind == "title" else ("> " if chunk.kind == "quote" else "")
            handle.write(prefix + chunk.text + "\n\n")
    print(f"  {stem}: {len(chunks)} chunks, {total_chars} chars  ->  {txt_path.name}")
    return len(chunks), total_chars


DEFAULT_PROBE_LENGTHS = "400,500,600,700,800,1000,1200,1500,1800"


def probe_texts(stems: list[str], lengths: list[int]) -> tuple[list[str], list[str]]:
    """One probe text per requested length, shortest first: the longest
    word-aligned prefix of the selected files' narrated text (titles
    excluded) that fits. A length the text can't grow into is dropped, so no
    text is sent twice. Also returns the stems the text came from: files are
    read in order only until there is enough text for the longest length."""
    words: list[str] = []
    used: list[str] = []
    size = 0
    for stem in stems:
        used.append(stem)
        for seg in parse_markdown((TEXT_DIR / f"{stem}.md").read_text(encoding="utf-8"), LANGUAGE):
            if seg.kind != "title":
                for word in seg.text.split():
                    words.append(word)
                    size += len(word) + 1
        if size > max(lengths):
            break
    texts: list[str] = []
    for target in sorted(set(lengths)):
        text = ""
        for word in words:
            candidate = f"{text} {word}" if text else word
            if len(candidate) > target:
                break
            text = candidate
        if text and (not texts or len(text) > len(texts[-1])):
            texts.append(text)
    return texts, used


class ProbeResult(NamedTuple):
    passed: int | None  # longest length that passed every attempt
    failed: int | None  # the length that failed, or None if nothing did
    recheck_failed: bool = False  # the longest passing length failed when re-sent


def probe_max_chars(texts: list[str], get_token, voice: str, rate: str | None, attempts: int = 2) -> ProbeResult:
    """Measure the longest request this voice/endpoint accepts every time.

    The limit depends on the voice/model and endpoint (one preview voice
    started failing from ~600 chars, far under DEFAULT_BUDGET), so it is
    measured, not assumed. Sends each text, shortest first, `attempts` times
    as a single request -- no retries, no splitting -- and stops at the first
    failure at a length: a 408/500/502/503/504, a read timeout or dropped
    response, a 200 whose WAV holds no audio, a 413, or a 400 once a shorter
    length has passed (at the shortest
    length a 400 more likely means bad SSML or a wrong voice name). It then
    re-sends the longest passing text once: if that fails too, failures aren't
    tied to length right now and the result says so. Any other error (a 429
    throttle, credentials, endpoint, network, response format) is re-raised,
    at any length, because it says nothing about length. A longer text also
    adds words, so a failure could in principle be content in the added words;
    if another file passes that length and this one fails again, suspect the
    content (one pass alone could be the intermittent edge)."""
    session = requests.Session()
    passed_text: str | None = None

    def send(text: str) -> str | None:
        """None if the request passed, else why it failed at this length."""
        try:
            pcm = synth_pcm(build_ssml(text, voice, rate), get_token, session, max_retries=1)
        except TransientExhaustionError as exc:
            if exc.status == 429:
                raise  # throttled: says nothing about length
            return f"FAILED ({exc})"
        except PermanentSynthesisError as exc:
            if exc.status == 413 or (exc.status == 400 and passed_text is not None):
                return f"REJECTED ({exc})"
            raise  # credentials, endpoint, network or response format: not a size limit
        return None if pcm else "FAILED (200 with no audio)"

    for text in texts:
        for attempt in range(1, attempts + 1):
            failure = send(text)
            print(f"  probe {len(text)} chars, attempt {attempt}/{attempts}: {failure or 'ok'}", flush=True)
            if failure:
                if passed_text is None:
                    return ProbeResult(None, len(text))
                recheck = send(passed_text)
                print(f"  re-check {len(passed_text)} chars: {recheck or 'ok'}", flush=True)
                return ProbeResult(len(passed_text), len(text), recheck is not None)
        passed_text = text
    return ProbeResult(len(passed_text) if passed_text else None, None)


# --- input selection -------------------------------------------------------------

def _normalize_input(pattern: str) -> str:
    """Be forgiving of a path-shaped argument (a natural mistake, since files live under
    TEXT_DIR with a .md extension) where a bare stem/substring is actually expected --
    strip a leading TEXT_DIR-name path prefix and a trailing .md suffix if present, so
    e.g. "text\\03-chapter-3.md" resolves the same as the documented "03-chapter-3"
    instead of silently building a doubled, nonexistent path (TEXT_DIR/text\\...md.md)."""
    p = pattern.replace("\\", "/")
    prefix = f"{TEXT_DIR.name}/"
    if p.startswith(prefix):
        p = p[len(prefix):]
    if p.endswith(".md"):
        p = p[: -len(".md")]
    return p


def resolve_inputs(args) -> list[tuple[str, Path, int]]:
    """Return (stem, output_mp3, track_number) triples to process."""
    out_dir = Path(args.out_dir)
    if args.all or not args.inputs:
        stems = NARRATED_STEMS
    else:
        stems = []
        for raw_pattern in args.inputs:
            pattern = _normalize_input(raw_pattern)
            matches = [s for s in NARRATED_STEMS if pattern in s] or [pattern]
            stems.extend(matches)
    return [(s, out_dir / f"{s}.mp3", NARRATED_STEMS.index(s) if s in NARRATED_STEMS else -1) for s in stems]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", help="stems or substrings, e.g. '03-chapter-3' (default: --all)")
    parser.add_argument("--all", action="store_true", help="every file in NARRATED_STEMS")
    parser.add_argument("--out-dir", default=str(AUDIO_DIR))
    parser.add_argument("--voice", default=VOICE,
                        help=f"TTS voice (default: ${ENV_PREFIX}_VOICE if set, else {DEFAULT_VOICE})")
    parser.add_argument("--resource", default=None, help=f"Foundry custom-domain resource name; or set ${ENV_PREFIX}_RESOURCE")
    parser.add_argument("--endpoint", default=None, help=f"full TTS endpoint URL; or set ${ENV_PREFIX}_ENDPOINT")
    parser.add_argument("--rate", default=None, help='prosody rate; pass with = to avoid argparse, e.g. --rate=-5%% or --rate=0.95')
    parser.add_argument("--dry-run", action="store_true", help="chunk only; write .txt, no API calls")
    parser.add_argument("--limit-chunks", type=int, default=None, help="synthesize only the first N chunks (smoke test)")
    parser.add_argument("--force", action="store_true", help="re-render even if the output MP3 already exists")
    parser.add_argument("--tag-only", action="store_true", help="skip synthesis; just (re-)apply ID3 tags to existing MP3s")
    parser.add_argument("--max-chars", type=int, default=None,
                        help=f"longest text per TTS request (default: ${ENV_PREFIX}_MAX_CHARS, else the limit "
                             f"--probe-max-chars saved in {LIMITS_PATH.name} for this voice, endpoint and rate, "
                             f"else {DEFAULT_BUDGET})")
    parser.add_argument("--probe-max-chars", action="store_true",
                        help="measure the longest request this voice/endpoint accepts, using text from the "
                             "selected files in order, save a recommended limit to "
                             f"{LIMITS_PATH.name}, then exit")
    parser.add_argument("--probe-lengths", default=None, metavar="LENGTHS",
                        help=f"comma-separated request lengths for --probe-max-chars (default {DEFAULT_PROBE_LENGTHS})")
    args = parser.parse_args()

    if not NARRATED_STEMS:
        parser.error(f"No .md files found under {TEXT_DIR} -- run pdf-to-markdown first, "
                      f"or set NARRATED_STEMS explicitly for your project.")
    if args.probe_lengths is not None and not args.probe_max_chars:
        parser.error("--probe-lengths only applies with --probe-max-chars")
    if args.probe_max_chars:
        ignored = [flag for flag, given in (
            ("--dry-run", args.dry_run), ("--tag-only", args.tag_only), ("--force", args.force),
            ("--limit-chunks", args.limit_chunks is not None), ("--max-chars", args.max_chars is not None),
        ) if given]
        if ignored:
            parser.error(f"--probe-max-chars can't be combined with {', '.join(ignored)}; run the probe on its own")

    global ENDPOINT
    ENDPOINT = resolve_endpoint(args.resource, args.endpoint)
    jobs = resolve_inputs(args)
    total = len(NARRATED_STEMS)

    def request_limit() -> tuple[int, str]:
        try:
            return resolve_max_chars(args.max_chars, args.voice, ENDPOINT, args.rate)
        except ValueError as exc:
            parser.error(str(exc))

    if args.dry_run:
        max_chars, max_source = request_limit()
        print(f"Dry run -- {len(jobs)} file(s), endpoint {ENDPOINT or f'(unset -- set ${ENV_PREFIX}_RESOURCE for a real run)'}, voice {args.voice}, max {max_chars} chars ({max_source})")
        n_files = t_chunks = t_chars = 0
        loaded: list[tuple[str, list[Chunk]]] = []
        for stem, out_path, _track in jobs:
            chunks = load_chunks(stem, max_chars)
            nc, nch = dry_run_file(stem, out_path, chunks)
            loaded.append((stem, chunks))
            n_files += 1
            t_chunks += nc
            t_chars += nch
        print(f"\nTotals: {n_files} files, {t_chunks} chunks, {t_chars} chars")
        for line in oversized_chunks(loaded, max_chars):
            print(f"  OVER the {max_chars}-char limit: {line}")
        return 0

    if args.tag_only:
        tagged = 0
        for stem, out_path, track in jobs:
            if out_path.exists():
                tag_mp3(out_path, stem, track, total)
                print(f"  tagged: {out_path.name}")
                tagged += 1
            else:
                print(f"  MISSING (skip tag): {out_path.name}")
        print(f"\nTagged {tagged} file(s).")
        return 0

    if not ENDPOINT:
        parser.error(
            f"No TTS endpoint configured. Set the environment variable {ENV_PREFIX}_RESOURCE to your "
            f"Azure AI Foundry custom-domain name, or {ENV_PREFIX}_ENDPOINT to the full URL, or pass "
            "--resource/--endpoint."
        )

    if args.probe_max_chars:
        raw_lengths = args.probe_lengths or DEFAULT_PROBE_LENGTHS
        try:
            lengths = [int(n) for n in raw_lengths.split(",") if n.strip()]
        except ValueError:
            lengths = []
        if not lengths or min(lengths) <= 0:
            parser.error(f"--probe-lengths: expected comma-separated positive integers, got {raw_lengths!r}")
        stems = [stem for stem, _o, _t in jobs]
        missing = [stem for stem in stems if not (TEXT_DIR / f"{stem}.md").exists()]
        if missing:
            parser.error(f"--probe-max-chars: no such file(s) under {TEXT_DIR}: {', '.join(missing)}")
        texts, used = probe_texts(stems, lengths)
        if not texts:
            parser.error("--probe-max-chars: nothing to send -- the selected files have no narrated text, "
                         "or every length is shorter than their first word")
        try:
            load_limits()  # the result is saved there: fail before sending anything, not after
        except ValueError as exc:
            parser.error(str(exc))
        get_token = make_token_provider()
        at_rate = f" at rate {args.rate}" if args.rate else ""
        print(f"Probing {args.voice}{at_rate}\n  endpoint {ENDPOINT}\n  lengths {[len(t) for t in texts]}"
              f"\n  text from {', '.join(used)}")
        if len(texts) < len(set(lengths)):
            print(f"  (the selected files only reach {len(texts[-1])} chars; longer lengths aren't tested)")
        try:
            result = probe_max_chars(texts, get_token, args.voice, args.rate)
        except (PermanentSynthesisError, TransientExhaustionError) as exc:
            hint = "Throttled (429): wait a few minutes, then run the probe again.\n" if exc.status == 429 else ""
            print(f"\nThe probe stopped on an error that isn't about length: {exc}\n{hint}"
                  "See SKILL.md, \"Troubleshooting\". Nothing saved.")
            return 1
        if result.passed is None:
            print(f"\nThe shortest probe ({result.failed} chars) failed. Either the limit is lower -- probe shorter "
                  "lengths, e.g. --probe-max-chars --probe-lengths 100,200,300 -- or requests are failing at any "
                  "length right now; see SKILL.md, \"Troubleshooting\". Nothing saved.")
            return 1
        if result.failed is None:
            try:
                saved = saved_limit(args.voice, ENDPOINT, args.rate)
            except ValueError as exc:
                kept = f"runs stop on the saved limit: {exc}"
            else:
                kept = (f"the limit saved earlier, {saved[0]} ({saved[1]}), stays in effect" if saved else
                        f"runs use the default {DEFAULT_BUDGET} and warn that no limit is saved")
            print(f"\nEvery probe passed, up to {result.passed} chars: no limit found. Nothing saved; for "
                  f"{args.voice}{at_rate} on this endpoint {kept} (--max-chars / ${ENV_PREFIX}_MAX_CHARS "
                  "override it). To look further, probe more files or longer --probe-lengths.")
            return 0
        if result.recheck_failed:
            print(f"\n{result.failed} chars failed, and then so did {result.passed} chars, which had passed before: "
                  "failures aren't tied to length right now. Nothing saved; probe again later (see SKILL.md, "
                  "\"Troubleshooting\").")
            return 1
        recommended = max(1, int(result.passed * PROBE_MARGIN))
        print(f"\nLongest request that passed every attempt: {result.passed} chars ({result.failed} failed). "
              f"Near the limit, failures are intermittent, so the recommended limit is "
              f"{PROBE_MARGIN:.0%} of that: {recommended} chars.")
        try:
            previous = save_limit(args.voice, ENDPOINT, args.rate, recommended, result.passed)
        except (OSError, ValueError) as exc:
            print(f"Couldn't save it to {LIMITS_PATH}: {exc}\n"
                  f"Pass --max-chars {recommended} (or set ${ENV_PREFIX}_MAX_CHARS) until it's saved.")
            return 1
        replaced = (f" (replacing {previous.get('max_chars')}, probed {previous.get('probed')})"
                    if previous else "")
        print(f"Saved {recommended} to {LIMITS_PATH}{replaced}. Every later run with {args.voice}{at_rate} on "
              f"this endpoint uses it; --max-chars / ${ENV_PREFIX}_MAX_CHARS override it.")
        env_value = os.environ.get(f"{ENV_PREFIX}_MAX_CHARS")
        if env_value:
            print(f"Note: ${ENV_PREFIX}_MAX_CHARS={env_value} is set in this shell, so runs here use that instead.")
        return 0

    max_chars, max_source = request_limit()
    # Plan every file before authenticating: skip finished files, load and
    # (for a smoke test) cut the chunks, and check exactly those chunks
    # against the limit. The loop below sends these same lists, never a re-read.
    plan: list[tuple[str, Path, int, list[Chunk] | None, Exception | None]] = []
    for stem, out_path, track in jobs:
        # A --limit-chunks smoke test NEVER writes to the canonical out_path:
        # it writes a distinct *.smoke.mp3 file instead, and is never tagged.
        # Otherwise a partial recording would land at the exact path the next
        # full (un-limited) run checks for "already done" and skips -- silently
        # shipping a truncated file tagged as if it were the complete chapter.
        target_path = out_path.with_name(f"{out_path.stem}.smoke{out_path.suffix}") if args.limit_chunks else out_path
        if target_path.exists() and not args.force and not args.limit_chunks:
            plan.append((stem, target_path, track, None, None))
            continue
        try:
            chunks = load_chunks(stem, max_chars)
        except Exception as exc:  # reported in order below, like any other per-file failure
            plan.append((stem, target_path, track, None, exc))
            continue
        if args.limit_chunks:
            chunks = chunks[:args.limit_chunks]
        plan.append((stem, target_path, track, chunks, None))

    too_long = oversized_chunks([(stem, chunks) for stem, _p, _t, chunks, _e in plan if chunks], max_chars)
    if too_long:
        parser.error(f"{len(too_long)} chunk(s) exceed the {max_chars}-char request limit ({max_source}); "
                     "titles are never split -- see SKILL.md, \"Troubleshooting\": " + "; ".join(too_long))

    get_token = make_token_provider()
    print(f"Synthesizing {len(jobs)} file(s) with {args.voice}, max {max_chars} chars ({max_source})\n  endpoint {ENDPOINT}")
    if max_source == "default":
        print(f"  WARNING: no saved request limit for {args.voice}"
              f"{f' at rate {args.rate}' if args.rate else ''} on this endpoint -- using the default {max_chars}. "
              "Before a full batch, run --probe-max-chars with the same --voice/--rate (SKILL.md, \"Measuring "
              "the request limit\").")
    failures: list[str] = []
    for stem, target_path, track, chunks, error in plan:
        if chunks is None and error is None:
            print(f"  skip (exists): {target_path.name}")
            tag_mp3(target_path, stem, track, total)
            continue
        print(f"  {stem} -> {target_path.name}")
        try:
            if error is not None:
                raise error
            seconds = synthesize_file(chunks, target_path, get_token, args.voice, args.rate)
            if args.limit_chunks:
                print(f"  smoke-test output only ({target_path.name}) -- not tagged, "
                      "not the final file; delete it before a full run if you like")
            else:
                tag_mp3(target_path, stem, track, total)
            print(f"  done: {target_path.name} ({seconds / 60:.1f} min)")
        except Exception as exc:  # keep going; report at the end
            failures.append(f"{stem}: {exc}")
            print(f"  FAILED: {stem}: {exc}")
    if failures:
        print(f"\n{len(failures)} file(s) failed:")
        for line in failures:
            print(f"  {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
