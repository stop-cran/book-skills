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
_BOLD_ITALIC_RE = re.compile(r"(\*\*\*)(.+?)\1")
_BOLD_RE = re.compile(r"(\*\*)(.+?)\1")
_ITALIC_RE = re.compile(r"(\*)(.+?)\1")
_UNDERSCORE_RE = re.compile(r"(?<!\w)(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_OPEN_RE = re.compile(r"(!?)\[([^\]\n]*)\]\(")
_TABLE_SEPARATOR_RE = re.compile(r"\s*\|?[\s:|-]+\|?[\s:|-]*\s*")

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


def roman_to_int(text: str) -> int | None:
    values = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
              (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
              (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
    remainder, result = text, 0
    for number, numeral in values:
        while remainder.startswith(numeral):
            result += number
            remainder = remainder[len(numeral):]
    if remainder or not 0 < result < 4000:
        return None
    n, canonical = result, ""
    for number, numeral in values:
        count, n = divmod(n, number)
        canonical += numeral * count
    return result if canonical == text else None


def scientific_notation(text: str, language: str, roman_references: str = "section",
                        reference_cases: dict[str, str] | None = None) -> str:
    words = {
        "en": ("section", "to", "part", " squared", " cubed",
               "times", "is proportional to", "equals", "plus", "minus"),
        "ru": ("параграф", "по", "часть", " в квадрате", " в кубе",
               "умножить на", "пропорционально", "равно", "плюс", "минус"),
    }
    section, to, part, squared, cubed, times, proportional, equals, plus, minus = words[language]
    if roman_references not in ("section", "part"):
        raise ValueError("roman_references must be 'section' or 'part'")

    def number(value: str) -> str:
        return str(roman_to_int(value) or value)

    previous_end, previous_case = -1, "nominative"

    def reference(match: re.Match) -> str:
        nonlocal previous_end, previous_case
        marker, first, last, *subdivision = match.groups()
        subfirst, sublast = subdivision if subdivision else (None, None)
        kind = roman_references if first.isalpha() else "section"
        plural = len(marker) == 2 or last is not None
        if language == "en":
            label = kind + ("s" if plural else "")
            result = label + " " + number(first) + (f" to {number(last)}" if last else "")
            if subfirst:
                result += f", part{'s' if sublast else ''} {number(subfirst)}"
                if sublast:
                    result += " to " + number(sublast)
            return result
        prefix = match.string[:match.start()]
        case = "nominative"
        prep = re.search(r"\b(в|во|из|к|ко|о|об|при|с|со|на|для|от|до|после|без|у|по|перед|за)\s+$",
                         prefix, re.I)
        override = next((reference_cases[key] for key in sorted(reference_cases or {}, key=len, reverse=True)
                         if re.search(r"(?:^|\W)" + re.escape(key) + r"\s+$", prefix, re.I)), None)
        if override:
            case = override
        elif prep:
            word = prep[1].lower()
            if word in ("из", "для", "от", "до", "после", "без", "у"):
                case = "genitive"
            elif word in ("к", "ко", "по"):
                case = "dative"
            elif word in ("в", "во", "о", "об", "при"):
                case = "prepositional"
            elif word == "перед":
                case = "instrumental"
            else:
                raise ValueError(f"Ambiguous Russian reference context: {prefix[-60:] + match[0]!r}; "
                                 "approve its case in narration.reference_cases before synthesis")
        elif previous_end >= 0 and re.fullmatch(r"\s*(?:,|и|или)\s*", match.string[previous_end:match.start()]):
            case = previous_case
        else:
            word = re.search(r"\b([А-Яа-яЁё]+)\s+$", prefix)
            if word and word[1].lower() not in {"и", "или", "а", "что", "как", "будто", "однако", "наконец"}:
                raise ValueError(f"Ambiguous Russian reference context: {prefix[-60:] + match[0]!r}; "
                                 "approve its case in narration.reference_cases before synthesis")
        forms = {
            "section": {
                "nominative": ("параграф", "параграфы"), "genitive": ("параграфа", "параграфов"),
                "dative": ("параграфу", "параграфам"), "instrumental": ("параграфом", "параграфами"),
                "prepositional": ("параграфе", "параграфах"),
                "accusative": ("параграф", "параграфы"),
            },
            "part": {
                "nominative": ("часть", "части"), "genitive": ("части", "частей"),
                "dative": ("части", "частям"), "instrumental": ("частью", "частями"),
                "prepositional": ("части", "частях"),
                "accusative": ("часть", "части"),
            },
        }
        previous_end, previous_case = match.end(), case
        label = forms[kind][case][int(plural)]
        numbers = f"с {number(first)} по {number(last)}" if last else number(first)
        result = f"{label} {numbers}"
        if subfirst:
            sublabel = forms["part"][case][int(sublast is not None)]
            subnumbers = f"с {number(subfirst)} по {number(sublast)}" if sublast else number(subfirst)
            result += f", {sublabel} {subnumbers}"
        return result

    numeral = r"(\d+|[IVXLCDM]+)\b"
    reference_pattern = r"(§{1,2})\s*" + numeral + r"(?:\s*[–—-]\s*§?\s*" + numeral + r")?"
    if roman_references == "part":
        reference_pattern += r"(?:,\s*([IVXLCDM]+)\b(?:\s*[–—-]\s*([IVXLCDM]+)\b)?)?"
    text = re.sub(reference_pattern, reference, text)
    text = re.sub(r"§{1,2}", section + " ", text)
    text = re.sub(r"\(([IVXLCDM]+)\)",
                  lambda m: f"{part} {roman_to_int(m[1])}" if roman_to_int(m[1]) else m[0], text)
    text = re.sub(r"([²³])(?=\w)", r"\1 ", text)
    text = text.replace("²", squared).replace("³", cubed)
    text = text.replace("·", f" {times} ").replace("×", f" {times} ")
    arrow = "к" if language == "ru" else "to"
    text = text.replace("∝", f" {proportional} ").replace("→", f" {arrow} ")
    text = re.sub(r"\s=\s", f" {equals} ", text)
    text = re.sub(r"(?<![A-Za-z0-9])\+(?=[A-Za-z])", plus + " ", text)
    text = re.sub(r"(?<![A-Za-z0-9])[−–-](?=[A-Za-z]\b)", minus + " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_tables(md: str, policy: str) -> str:
    """Only skip tables when the book explicitly declares them redundant."""
    if policy not in ("error", "skip"):
        raise ValueError("tables must be 'error' or 'skip'")
    lines = md.splitlines()
    drop: set[int] = set()
    for i, line in enumerate(lines):
        if "|" not in line or "---" not in line or not _TABLE_SEPARATOR_RE.fullmatch(line):
            continue
        if policy == "error":
            raise ValueError(f"Markdown table at line {i + 1}: provide an approved spoken rendering "
                             "or explicitly configure tables='skip' for redundant recap tables")
        drop.add(i)
        for direction in (-1, 1):
            j = i + direction
            while 0 <= j < len(lines) and "|" in lines[j] and lines[j].strip():
                drop.add(j)
                j += direction
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


def render_fenced_blocks(md: str, policy: str) -> str:
    if policy not in ("error", "diagram"):
        raise ValueError("fenced_blocks must be 'error' or 'diagram'")

    def render(match: re.Match) -> str:
        body = match[2]
        if policy != "diagram" or not re.search(r"[\u2500-\u257f]", body):
            raise ValueError("Fenced block needs an approved spoken rendering; "
                             "fenced_blocks='diagram' supports box-drawing recaps only")
        return re.sub(r"[\u2500-\u257f▼]", " ", body)

    md = re.sub(r"^(`{3,}|~{3,})[^\n]*\n(.*?)^\1[ \t]*$", render, md, flags=re.M | re.S)
    if re.search(r"^\s*(`{3,}|~{3,})", md, re.M):
        raise ValueError("Unclosed or unsupported fenced block")
    return md


def strip_inline_links(text: str) -> str:
    pieces, position = [], 0
    while match := _LINK_OPEN_RE.search(text, position):
        cursor, depth = match.end(), 1
        while cursor < len(text) and depth:
            if text[cursor] == "\\" and cursor + 1 < len(text):
                cursor += 2
                continue
            if text[cursor] == "(":
                depth += 1
            elif text[cursor] == ")":
                depth -= 1
            cursor += 1
        if depth:
            raise ValueError("Unclosed inline Markdown link")
        pieces.extend((text[position:match.start()], "" if match[1] else match[2]))
        position = cursor
    pieces.append(text[position:])
    return "".join(pieces)


def strip_inline_decoration(text: str) -> str:
    text = strip_inline_links(text)
    text = _CODE_RE.sub(r"\1", text)
    text = _BOLD_ITALIC_RE.sub(r"\2", text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _ITALIC_RE.sub(r"\2", text)
    text = _UNDERSCORE_RE.sub(r"\2", text)
    return text


def document_heading(md: str) -> str:
    for line in md.splitlines():
        if not line.strip():
            continue
        match = _HEADING_RE.fullmatch(line)
        if match and match[1] == "#" and strip_inline_decoration(match[2]).strip():
            return match[2]
        break
    raise ValueError("Narration must start with a nonempty H1 heading")


def clean_inline(text: str, language: str = "ru", notation: str = "plain",
                 roman_references: str = "section", reference_cases: dict[str, str] | None = None) -> str:
    """Strip Markdown emphasis/link/code syntax and spell out basic symbols."""
    text = strip_inline_decoration(text)
    if notation == "scientific":
        text = scientific_notation(text, language, roman_references, reference_cases)
    elif notation == "plain":
        text = _spell_symbols(text, language)
    else:
        raise ValueError(f"Unknown notation: {notation}")
    return text


def parse_markdown(md: str, language: str = "ru", *, notation: str = "plain",
                   tables: str = "error", strip_section_numbers: bool = False,
                   fenced_blocks: str = "error", roman_references: str = "section",
                   reference_cases: dict[str, str] | None = None) -> list[Segment]:
    """Parse a Markdown file's body into narration Segments.

    The first H1 becomes a "title" segment, rendered via spoken_heading() so
    the file's own heading is what listeners hear first. Blockquotes become
    "quote" segments (a distinct pause/tone before an epigraph). Everything
    else becomes "para" segments, one per blank-line-delimited paragraph.
    """
    if language not in SYMBOL_WORDS:
        raise ValueError(f"Unsupported narration language: {language}")
    md = strip_tables(md, tables)
    md = render_fenced_blocks(md, fenced_blocks)
    segments: list[Segment] = []
    paragraph_lines: list[str] = []
    seen_title = False

    def flush_paragraph():
        if paragraph_lines:
            text = clean_inline(" ".join(paragraph_lines).strip(), language, notation,
                                roman_references, reference_cases)
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
            if level != "#" and strip_section_numbers:
                text = re.sub(r"^([IVXLCDM]+)\.\s+",
                              lambda m: "" if roman_to_int(m[1]) else m[0], text)
            text = clean_inline(text, language, notation, roman_references, reference_cases)
            if level == "#" and not seen_title:
                segments.append(Segment("title", spoken_heading(text, language)))
                seen_title = True
            else:
                segments.append(Segment("title", text))
            continue
        quote_m = _QUOTE_RE.match(line)
        if quote_m:
            flush_paragraph()
            segments.append(Segment("quote", clean_inline(quote_m.group(1), language, notation,
                                                         roman_references, reference_cases)))
            continue
        bullet_m = _BULLET_RE.match(line) or _ORDERED_RE.match(line)
        if bullet_m:
            flush_paragraph()
            segments.append(Segment("para", clean_inline(bullet_m.group(1), language, notation,
                                                        roman_references, reference_cases)))
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
