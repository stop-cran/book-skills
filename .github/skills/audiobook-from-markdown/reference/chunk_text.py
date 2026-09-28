"""Split cleaned narration segments into TTS-sized chunks.

Real-time TTS endpoints cap each request (see DEFAULT_BUDGET below), so long
paragraphs must be split -- at sentence boundaries, since a cut mid-sentence
corrupts intonation. This module keeps whole paragraphs together when they fit
under the budget, and otherwise splits them at sentence boundaries, packing
sentences greedily; only a single sentence longer than the budget is cut, at a
word boundary (mid-word only for a word longer than the budget).

Each Chunk carries a pre_pause_ms hint so the synthesizer can insert a natural
silence before it (longest before a section title, medium before an
epigraph/new paragraph, shortest between the sub-parts of one split paragraph).

The splitting algorithm itself is language-agnostic. What's language-specific is
the set of trailing-period tokens that do NOT end a sentence -- supplied per
language in LANGUAGE_ABBREVIATIONS below. Add an entry for your language before
using this on non-Russian/non-English text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clean_text import Segment

# Default only: the real per-request limit depends on the voice/model and
# endpoint (on one preview voice no chunk under 600 chars needed a split, and
# most chunks of 600+ did). `synthesize.py --probe-max-chars` measures it and
# saves it to tts-limits.json, which every later run uses (`--max-chars`
# overrides it) -- don't edit this constant instead.
DEFAULT_BUDGET = 1800

DEFAULT_PAUSES = {
    "title": 700,  # before a section/subsection heading
    "quote": 500,  # before an epigraph (stylistic shift out of prose)
    "para": 350,   # before a new paragraph
    "cont": 140,   # between the sub-parts of one split paragraph
}

# --- language-specific abbreviation / label sets -----------------------------
# Each entry: multi-letter abbreviations that end in a period but don't end a
# sentence. Single-letter initials and trailing-digit references (page/citation
# numbers) are handled generically below and don't need per-language listing.

LANGUAGE_ABBREVIATIONS = {
    "ru": {
        "др", "см", "ср", "пр", "гл", "проф", "акад", "им", "рис",
        "изд", "отд", "вып", "геогр",
    },
    "en": {
        "mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e",
        "fig", "vol", "no", "ch", "sec", "pp",
    },
}

# Quote-closing characters that can sit between a sentence-final mark and the
# following whitespace (e.g. `...»  Однако` / `..."  However`). Extend this per
# language/quote-style: this default covers French guillemets, German-style
# low/high quotes, and standard straight/curly quotes.
_QUOTE_CLOSERS = "\"'\u2019\u201d\u00bb"

_ROMAN_CHARS_RE = re.compile(r"[IVXLCDM]+")


def _make_sentence_end_re() -> re.Pattern:
    # A sentence boundary is whitespace after terminal punctuation, optionally
    # followed by a closing quote/paren/bracket. Python's re has no
    # variable-width lookbehind, so two fixed-width lookbehinds are alternated
    # -- this catches boundaries where the char immediately before the space is
    # a closing quote, not the period itself.
    closers = re.escape(_QUOTE_CLOSERS + ")]")
    return re.compile(rf"(?:(?<=[.!?])|(?<=[.!?][{closers}]))\s+")


_SENTENCE_END_RE = _make_sentence_end_re()


def _looks_like_nonfinal(fragment: str, abbrevs: set[str]) -> bool:
    """True if `fragment` ends in a period that is not a sentence end."""
    if fragment.endswith(("!", "?")):
        return False
    tokens = fragment.split()
    if not tokens:
        return False
    last = tokens[-1]
    core = last.rstrip(".!?\"'\u2019\u201d\u00bb)")
    lower = core.lower()
    if lower in abbrevs:
        return True
    # Single-letter initial ("A.", "G.", "А.", "Н.") -- covers author-initial
    # citations and the two readings of a bare unit-like abbreviation.
    if re.fullmatch(r"[A-Za-zА-Яа-яЁё]", core):
        return True
    # Bare Roman-numeral inline label ("V.", "XI.") -- some academic texts use
    # these as cross-reference labels inside prose; the sentence continues past
    # the label rather than ending there. Only relevant if your source uses
    # this convention -- harmless no-op otherwise.
    if core and _ROMAN_CHARS_RE.fullmatch(core):
        return True
    # Trailing digit (page/citation reference, e.g. "p. 42." / "с. 42.").
    if re.search(r"\d$", core):
        return True
    return False


def split_sentences(text: str, language: str = "ru") -> list[str]:
    """Split text into sentences, rejoining abbreviation/initial false splits."""
    abbrevs = LANGUAGE_ABBREVIATIONS.get(language, set())
    fragments = _SENTENCE_END_RE.split(text.strip())
    out: list[str] = []
    for frag in fragments:
        if not frag:
            continue
        if out and _looks_like_nonfinal(out[-1], abbrevs):
            out[-1] = out[-1] + " " + frag
        else:
            out.append(frag)
    return out


def _hard_split(text: str, budget: int) -> list[str]:
    """Last-resort split of an over-long sentence at a word boundary."""
    pieces: list[str] = []
    while len(text) > budget:
        cut = text.rfind(" ", 0, budget)
        if cut <= 0:
            cut = budget
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def split_paragraph(text: str, budget: int = DEFAULT_BUDGET, language: str = "ru") -> list[str]:
    """Split one paragraph into <=budget parts at sentence boundaries; a single
    sentence over budget is cut at a word boundary, or mid-word for a word over
    budget (_hard_split)."""
    if len(text) <= budget:
        return [text]
    parts: list[str] = []
    current = ""
    for sentence in split_sentences(text, language):
        for piece in (_hard_split(sentence, budget) if len(sentence) > budget else [sentence]):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= budget:
                current += " " + piece
            else:
                parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return parts


@dataclass
class Chunk:
    text: str
    pre_pause_ms: int
    kind: str  # "title" | "quote" | "para" | ...


def chunk_segments(
    segments: list[Segment],
    budget: int = DEFAULT_BUDGET,
    pauses: dict | None = None,
    language: str = "ru",
) -> list[Chunk]:
    """Turn segments into synthesis chunks with structural pause hints."""
    pauses = pauses or DEFAULT_PAUSES
    chunks: list[Chunk] = []
    first = True
    for seg in segments:
        if seg.kind == "title":
            chunks.append(Chunk(seg.text, 0 if first else pauses["title"], "title"))
        else:
            base_pause = pauses.get(seg.kind, pauses["para"])
            parts = split_paragraph(seg.text, budget, language)
            for idx, part in enumerate(parts):
                pre = (0 if first else base_pause) if idx == 0 else pauses["cont"]
                chunks.append(Chunk(part, pre, seg.kind))
        first = False
    return chunks
