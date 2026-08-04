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
    python synthesize.py --dry-run --all              # inspect text prep, no API calls
    python synthesize.py --all                        # every file in text/
    python synthesize.py 03-chapter-3 --limit-chunks 2 # smoke test one file
"""

from __future__ import annotations

import argparse
import io
import os
import random
import re
import subprocess
import sys
import time
import wave
import xml.sax.saxutils as saxutils
from pathlib import Path

import requests

from chunk_text import DEFAULT_PAUSES, Chunk, chunk_segments, split_sentences
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
    format. synth_pcm_resilient() re-raises these immediately instead of
    recursively splitting the text: splitting a 401 into smaller 401s just
    burns API calls and delays the real error surfacing to the user."""


class TransientExhaustionError(RuntimeError):
    """Raised when synth_pcm()'s own retry-with-backoff loop exhausts every
    attempt on a transient (429/408/5xx) HTTP response from a *reachable*
    endpoint. This is the ONLY failure synth_pcm_resilient() treats as worth
    splitting the text and retrying the halves -- see its docstring for why
    (empirically, a different/smaller request often lands on a healthy
    backend instance behind the same endpoint, which is not true of a
    genuinely broken endpoint or bad credentials)."""


def _pcm_from_wav(data: bytes) -> bytes:
    with wave.open(io.BytesIO(data), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (
            SAMPLE_RATE, 1, SAMPLE_WIDTH,
        ):
            raise PermanentSynthesisError(
                f"unexpected PCM format: {wav.getframerate()}Hz "
                f"{wav.getnchannels()}ch {wav.getsampwidth() * 8}bit"
            )
        return wav.readframes(wav.getnframes())


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
    transient HTTP status (408/429/5xx) -- the one case worth splitting the
    text for. Everything else (a non-transient HTTP status such as 401/403/
    400/404, or a network/connectivity failure such as a bad hostname) raises
    PermanentSynthesisError instead: a broken endpoint or bad credentials
    fails identically no matter how small the request is, so splitting the
    text cannot help and would only multiply doomed calls.
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
            # Connectivity-level failure (DNS, refused connection, TLS,
            # timeout before any response). A couple of retries absorb a
            # brief blip, but persistent failure here means the endpoint
            # itself is unreachable/misconfigured, not a single unhealthy
            # backend instance -- so exhaustion is PermanentSynthesisError,
            # not the splittable TransientExhaustionError.
            if attempt == max_retries - 1:
                raise PermanentSynthesisError(
                    f"TTS request failed after {max_retries} attempts: network error: {exc}"
                ) from exc
            time.sleep(min(2 ** attempt, 12) * (0.8 + 0.4 * random.random()))
            continue
        if resp.status_code == 200:
            return _pcm_from_wav(resp.content)
        detail = f"{resp.status_code} {resp.text[:300]}"
        if resp.status_code not in (408, 429, 500, 502, 503, 504):
            raise PermanentSynthesisError(f"TTS request failed: {detail}")
        if attempt == max_retries - 1:
            raise TransientExhaustionError(
                f"TTS request failed after {max_retries} attempts: {detail}"
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
    Empirically, transient failures did not correlate cleanly with chunk
    length or specific content -- verified by bisecting a reliably-failing
    ~1100-char paragraph down to sub-100-char fragments that synthesized fine
    individually, while some unrelated short chunks failed just as
    persistently. That looks like backend-instance flakiness rather than a
    deterministic client-side trigger: retrying the exact same request just
    re-hits the same bad state, but a *different* (smaller) request often
    succeeds, so splitting is a pragmatic, effective workaround regardless of
    the exact root cause.

    Only catches TransientExhaustionError -- deliberately NOT a bare
    `except Exception`. A permanent failure (bad credentials, an
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
    except TransientExhaustionError:
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

def load_chunks(stem: str) -> list[Chunk]:
    md_path = TEXT_DIR / f"{stem}.md"
    segments = parse_markdown(md_path.read_text(encoding="utf-8"), LANGUAGE)
    return chunk_segments(segments, language=LANGUAGE)


def synthesize_file(
    stem: str,
    out_path: Path,
    get_token,
    voice: str,
    rate: str | None,
    limit_chunks: int | None = None,
) -> float:
    chunks = load_chunks(stem)
    if limit_chunks:
        chunks = chunks[:limit_chunks]
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


def dry_run_file(stem: str, out_path: Path) -> tuple[int, int]:
    chunks = load_chunks(stem)
    total_chars = sum(len(c.text) for c in chunks)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path = out_path.with_suffix(".txt")
    with txt_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            prefix = "## " if chunk.kind == "title" else ("> " if chunk.kind == "quote" else "")
            handle.write(prefix + chunk.text + "\n\n")
    print(f"  {stem}: {len(chunks)} chunks, {total_chars} chars  ->  {txt_path.name}")
    return len(chunks), total_chars


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", help="stems or substrings, e.g. '03-chapter-3' (default: --all)")
    parser.add_argument("--all", action="store_true", help="every file in NARRATED_STEMS")
    parser.add_argument("--out-dir", default=str(AUDIO_DIR))
    parser.add_argument("--voice", default=VOICE)
    parser.add_argument("--resource", default=None, help=f"Foundry custom-domain resource name; or set ${ENV_PREFIX}_RESOURCE")
    parser.add_argument("--endpoint", default=None, help=f"full TTS endpoint URL; or set ${ENV_PREFIX}_ENDPOINT")
    parser.add_argument("--rate", default=None, help='prosody rate; pass with = to avoid argparse, e.g. --rate=-5%% or --rate=0.95')
    parser.add_argument("--dry-run", action="store_true", help="chunk only; write .txt, no API calls")
    parser.add_argument("--limit-chunks", type=int, default=None, help="synthesize only the first N chunks (smoke test)")
    parser.add_argument("--force", action="store_true", help="re-render even if the output MP3 already exists")
    parser.add_argument("--tag-only", action="store_true", help="skip synthesis; just (re-)apply ID3 tags to existing MP3s")
    args = parser.parse_args()

    if not NARRATED_STEMS:
        parser.error(f"No .md files found under {TEXT_DIR} -- run pdf-to-markdown first, "
                      f"or set NARRATED_STEMS explicitly for your project.")

    global ENDPOINT
    ENDPOINT = resolve_endpoint(args.resource, args.endpoint)
    jobs = resolve_inputs(args)
    total = len(NARRATED_STEMS)

    if args.dry_run:
        print(f"Dry run -- {len(jobs)} file(s), endpoint {ENDPOINT or f'(unset -- set ${ENV_PREFIX}_RESOURCE for a real run)'}, voice {args.voice}")
        n_files = t_chunks = t_chars = 0
        for stem, out_path, _track in jobs:
            nc, nch = dry_run_file(stem, out_path)
            n_files += 1
            t_chunks += nc
            t_chars += nch
        print(f"\nTotals: {n_files} files, {t_chunks} chunks, {t_chars} chars")
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

    get_token = make_token_provider()
    print(f"Synthesizing {len(jobs)} file(s) with {args.voice}\n  endpoint {ENDPOINT}")
    failures: list[str] = []
    for stem, out_path, track in jobs:
        # A --limit-chunks smoke test NEVER writes to the canonical out_path:
        # it writes a distinct *.smoke.mp3 file instead, and is never tagged.
        # Otherwise a partial recording would land at the exact path the next
        # full (un-limited) run checks for "already done" and skips -- silently
        # shipping a truncated file tagged as if it were the complete chapter.
        target_path = out_path.with_name(f"{out_path.stem}.smoke{out_path.suffix}") if args.limit_chunks else out_path
        if target_path.exists() and not args.force and not args.limit_chunks:
            print(f"  skip (exists): {target_path.name}")
            tag_mp3(target_path, stem, track, total)
            continue
        print(f"  {stem} -> {target_path.name}")
        try:
            seconds = synthesize_file(
                stem, target_path, get_token, args.voice, args.rate,
                limit_chunks=args.limit_chunks,
            )
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
