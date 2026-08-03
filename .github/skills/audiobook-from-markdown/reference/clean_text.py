"""Clean Markdown into narration-ready text segments.

Parses Markdown into typed Segments (title / para / quote) with Markdown syntax
stripped (formatting markers a TTS engine shouldn't read literally) and symbols
spelled out where a TTS engine would otherwise mangle or mispronounce them.

Two audible-navigation conventions live here, both driven by real listening
feedback on a finished audiobook (see SKILL.md):

1. `spoken_heading()` renders a file's own H1 as the *first* narrated segment,
   using the language's natural ordinal-word form for a chapter/appendix number
   ("Chapter three" / "Глава третья"), not a spelled-out Roman numeral or bare
   digit -- this is the primary audible cue for navigating between tracks by
   ear, so it needs to actually be spoken clearly at the very start, not just
   present as a heading that happens to get narrated along with everything else.
2. `wrap_ai_summary()` brackets an AI-generated chapter summary with a spoken
   begin/end marker, so a listener can tell by ear where the summary stops and
   the book's original text begins. Do not rely on a pause alone for this -- a
   TTS pause sounds the same whether it's between-summary-and-text or just
   between two ordinary paragraphs, so the marker needs to be actual spoken
   words, not silence.

A book with its own domain-specific notation (mathematical formulas, a bespoke
symbolic scheme, extensive footnote/citation apparatus) will need additional
symbol-spelling rules beyond SYMBOL_WORDS below -- add them following the same
pattern (a dict/regex plus a render function), scoped to the files that
actually need them rather than applied globally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- language-specific ordinal/heading tables ---------------------------------
# Extend per language and per book's own numbering scheme (chapters, parts,
# appendices, books/volumes, ...). Keys are the Roman/Arabic numeral as it
# appears in the heading; values are the natural spoken ordinal word.

CHAPTER_ORDINALS = {
    "ru": {
        "I": "первая", "II": "вторая", "III": "третья", "IV": "четвёртая",
        "V": "пятая", "VI": "шестая", "VII": "седьмая", "VIII": "восьмая",
        "IX": "девятая", "X": "десятая",
    },
    "en": {
        "I": "one", "II": "two", "III": "three", "IV": "four", "V": "five",
        "VI": "six", "VII": "seven", "VIII": "eight", "IX": "nine", "X": "ten",
    },
}

APPENDIX_ORDINALS = {
    "ru": {"I": "первое", "II": "второе", "III": "третье", "IV": "четвёртое"},
    "en": {"I": "one", "II": "two", "III": "three", "IV": "four"},
}

# (heading_regex, word_before_number, ordinal_table) -- matched in order
# against a file's H1 text; first match wins. `word_before_number` is what
# gets spoken before the ordinal ("Глава" / "Chapter" / "Приложение" /
# "Appendix"). Adjust the regexes to match your book's actual heading text.
HEADING_PATTERNS = {
    "ru": [
        (re.compile(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s+(.+)$"), "Глава", CHAPTER_ORDINALS["ru"]),
        (re.compile(r"^Приложение\s+(I|II|III|IV)\.\s+(.+)$"), "Приложение", APPENDIX_ORDINALS["ru"]),
    ],
    "en": [
        (re.compile(r"^Chapter\s+(I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s*(.*)$"), "Chapter", CHAPTER_ORDINALS["en"]),
        (re.compile(r"^Appendix\s+(I|II|III|IV)\.\s*(.*)$"), "Appendix", APPENDIX_ORDINALS["en"]),
    ],
}


def spoken_heading(h1_text: str, language: str = "ru") -> str:
    """Render a file's H1 for NARRATION (ordinal word, e.g. "Глава третья"),
    as opposed to the Arabic-digit form used in the ID3 TIT2 tag (see the
    audiobook-from-markdown SKILL.md ID3 convention table) -- the two
    representations intentionally differ because one is heard and the other
    is read on a screen."""
    for pattern, lead_word, ordinals in HEADING_PATTERNS.get(language, []):
        m = pattern.match(h1_text.strip())
        if m:
            numeral, rest = m.group(1), m.group(2)
            ordinal = ordinals.get(numeral, numeral)
            rest = f" {rest}" if rest else ""
            return f"{lead_word} {ordinal}.{rest}" if rest else f"{lead_word} {ordinal}."
    return h1_text  # no numbered-heading pattern matched; narrate as-is


# --- AI-summary audible boundary markers -------------------------------------

SUMMARY_MARKERS = {
    "ru": ("Краткое содержание.", "Конец краткого содержания."),
    "en": ("Summary.", "End of summary."),
}


@dataclass
class Segment:
    kind: str  # "title" | "para" | "quote"
    text: str


def wrap_ai_summary(summary_paragraphs: list[str], language: str = "ru") -> list[Segment]:
    """Wrap an AI-generated summary's paragraphs with spoken begin/end markers,
    as their own segments so the chunker gives them the normal inter-paragraph
    pause on both sides. Insert the result immediately after the chapter's
    title segment and before its first original-text segment."""
    begin, end = SUMMARY_MARKERS.get(language, SUMMARY_MARKERS["en"])
    return (
        [Segment("para", begin)]
        + [Segment("para", p) for p in summary_paragraphs]
        + [Segment("para", end)]
    )


# --- Markdown decoration stripping (language-agnostic) ------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_BOLD_ITALIC_RE = re.compile(r"(\*\*\*|___)(.+?)\1")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC_RE = re.compile(r"(\*|_)(.+?)\1")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")

# A minimal starting set of symbols that read badly if left literal. Extend
# for your book's actual symbol usage (audit the corpus first, as you would
# for any clean_text.py rule, rather than adding rules speculatively).
SYMBOL_WORDS = {
    "ru": {"%": "процентов", "&": "и", "§": "параграф", "→": "к"},
    "en": {"%": "percent", "&": "and", "§": "section", "→": "to"},
}


def _spell_symbols(text: str, language: str) -> str:
    for sym, word in SYMBOL_WORDS.get(language, {}).items():
        text = text.replace(sym, f" {word} ")
    return re.sub(r"\s{2,}", " ", text).strip()


def clean_inline(text: str, language: str = "ru") -> str:
    """Strip Markdown emphasis/link/code syntax and spell out basic symbols."""
    text = _LINK_RE.sub(r"\1", text)
    text = _CODE_RE.sub(r"\1", text)
    text = _BOLD_ITALIC_RE.sub(r"\2", text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _ITALIC_RE.sub(r"\2", text)
    text = _spell_symbols(text, language)
    return text


def parse_markdown(md: str, language: str = "ru") -> list[Segment]:
    """Parse a Markdown file's body into narration Segments.

    The first H1 becomes a "title" segment, rendered via spoken_heading() so
    the file's own heading is what listeners hear first. Blockquotes become
    "quote" segments (a distinct pause/tone before an epigraph). Everything
    else becomes "para" segments, one per blank-line-delimited paragraph.
    """
    segments: list[Segment] = []
    paragraph_lines: list[str] = []
    seen_title = False

    def flush_paragraph():
        if paragraph_lines:
            text = clean_inline(" ".join(paragraph_lines).strip(), language)
            if text:
                segments.append(Segment("para", text))
            paragraph_lines.clear()

    for raw_line in md.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush_paragraph()
            continue
        if _HR_RE.match(line):
            flush_paragraph()
            continue
        heading_m = _HEADING_RE.match(line)
        if heading_m:
            flush_paragraph()
            level, text = heading_m.groups()
            text = clean_inline(text, language)
            if level == "#" and not seen_title:
                segments.append(Segment("title", spoken_heading(text, language)))
                seen_title = True
            else:
                segments.append(Segment("title", text))
            continue
        quote_m = _QUOTE_RE.match(line)
        if quote_m:
            flush_paragraph()
            segments.append(Segment("quote", clean_inline(quote_m.group(1), language)))
            continue
        bullet_m = _BULLET_RE.match(line) or _ORDERED_RE.match(line)
        if bullet_m:
            flush_paragraph()
            segments.append(Segment("para", clean_inline(bullet_m.group(1), language)))
            continue
        paragraph_lines.append(line.strip())
    flush_paragraph()
    return segments


if __name__ == "__main__":
    demo_md = (
        "# III. Некий заголовок\n\n"
        "Это первый абзац с **жирным** текстом.\n\n"
        "> Эпиграф в виде цитаты.\n\n"
        "Второй абзац, 50% готовности.\n"
    )
    for seg in parse_markdown(demo_md, "ru"):
        print(seg.kind, "->", seg.text)
    print()
    print("AI summary wrap demo:")
    for seg in wrap_ai_summary(["Краткое содержание главы в двух предложениях."], "ru"):
        print(seg.kind, "->", seg.text)
