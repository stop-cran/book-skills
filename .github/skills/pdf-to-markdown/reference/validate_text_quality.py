"""Reference implementation: text-quality validation for a PDF-extracted book corpus.

Two checks, both read-only (they report, they don't modify anything):

1. Character hygiene -- invisible/control characters and Cyrillic/Latin (or other
   script-pair) homoglyph mixing. Fully language-agnostic already.
2. Spell-check -- pluggable per language. Flat dictionaries work for low-inflection
   languages (English) but produce enormous false-positive rates for morphologically
   rich languages (Russian and most Slavic languages), where a single lemma can have
   dozens of correctly-spelled surface forms. Use a morphological analyzer instead where
   available.

Copy this file into your project, adjust LANGUAGE, STEMS/TEXT_DIR, and the
KNOWN_OK allowlist for the book at hand, then run it directly:

    python validate_text_quality.py

Adapt the file list / directory to your project's layout -- this reference version
assumes one .md file per section in a `text/` directory, matching the layout produced
by the companion pdf-to-markdown workflow.
"""
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

# --- configuration: adjust per project -------------------------------------

LANGUAGE = "ru"  # "ru" (morphological, via pymorphy3) or "en" (flat dictionary)
TEXT_DIR = Path(__file__).resolve().parent / "text"
# All .md stems to validate (narrated + reference-only, if any) -- adjust to your
# project's actual file list.
STEMS = sorted(p.stem for p in TEXT_DIR.glob("*.md")) if TEXT_DIR.exists() else []

# Proper nouns / domain terms that a general-purpose dictionary won't know, gathered
# from the book's own front matter (authors, places, discipline-specific jargon).
# Always book-specific -- start empty and add entries as the spell-check surfaces
# real (non-typo) unknowns you don't want to keep re-triaging.
KNOWN_OK: set[str] = set()

# --- 1. invisible / control character scan (language-agnostic) --------------

INVISIBLE_CHARS = {
    "\u200b": "ZERO WIDTH SPACE", "\u200c": "ZERO WIDTH NON-JOINER",
    "\u200d": "ZERO WIDTH JOINER", "\ufeff": "BOM/ZERO WIDTH NO-BREAK SPACE",
    "\u00ad": "SOFT HYPHEN", "\u2060": "WORD JOINER",
    "\u00a0": "NO-BREAK SPACE", "\u2028": "LINE SEPARATOR",
    "\u2029": "PARAGRAPH SEPARATOR",
}

# --- 2. homoglyph / script-mixing scan --------------------------------------
# Adjust WORD_SCRIPT_RE / OTHER_SCRIPT_LOOKALIKES for the source language's script.
# This default pair (Latin <-> Cyrillic) is the common case for OCR'd Cyrillic-script
# books; swap in a different confusable-script pair (e.g. Latin <-> Greek) as needed.

PRIMARY_SCRIPT_RE = re.compile(r"[а-яА-ЯёЁ]")  # Cyrillic, for LANGUAGE == "ru"
# Latin letters that are visually identical (or near-identical) to a Cyrillic
# counterpart -- the classic OCR/copy-paste homoglyph mix-up.
OTHER_SCRIPT_LOOKALIKES = set("aAeEoOpPcCxXyYkKmMhHtTiIbBdDgG")


def scan_char_hygiene(text: str) -> list[str]:
    issues = []
    for ch, name in INVISIBLE_CHARS.items():
        count = text.count(ch)
        if count:
            issues.append(f"  {count}x {name} (U+{ord(ch):04X})")
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        if cat == "Cc" and ch not in "\n\t\r":
            context = text[max(0, i - 20):i + 20].replace("\n", "\\n")
            issues.append(f"  control char U+{ord(ch):04X} at offset {i}: ...{context}...")
    # A "word" (run of letters) containing BOTH the primary script and a lookalike
    # Latin letter -- and no *other* (non-lookalike) Latin letter -- is almost
    # certainly an OCR error: genuine text has no reason to mix scripts inside one
    # token. Words that also contain an unambiguous Latin letter are left alone --
    # those are more likely a deliberate abbreviation/citation, not silent corruption.
    for m in re.finditer(r"[A-Za-zА-Яа-яёЁ]+", text):
        word = m.group()
        has_primary = bool(PRIMARY_SCRIPT_RE.search(word))
        has_lookalike = any(c in OTHER_SCRIPT_LOOKALIKES for c in word)
        has_unambiguous_latin = any(
            c.isascii() and c.isalpha() and c not in OTHER_SCRIPT_LOOKALIKES for c in word
        )
        if has_primary and has_lookalike and not has_unambiguous_latin:
            context = text[max(0, m.start() - 20):m.end() + 20].replace("\n", "\\n")
            issues.append(f"  mixed-script word '{word}': ...{context}...")
    return issues


# --- 3. spell-check (pluggable backend) --------------------------------------

WORD_RE = re.compile(r"[А-Яа-яёЁ]+(?:-[А-Яа-яёЁ]+)*" if LANGUAGE == "ru"
                      else r"[A-Za-z]+(?:-[A-Za-z]+)*")


def _make_spellcheck_ru():
    """Morphological spell-check for Russian via pymorphy3.

    is_known=True means the lemma is in the dictionary -- never flagged. For
    is_known=False words, pymorphy3's suffix-based guesser still assigns a
    confidence score; a genuine (if rare/archaic) Russian word form typically
    scores much higher than OCR garbage or a typo. Empirically, score < 0.25 is a
    reasonable threshold for "suspicious enough to review" -- tune per corpus.
    """
    import pymorphy3
    morph = pymorphy3.MorphAnalyzer()
    threshold = 0.25

    def check(word: str) -> bool:
        """Return True if `word` should be flagged as suspicious."""
        best = morph.parse(word)[0]
        return not best.is_known and best.score < threshold

    return check


def _make_spellcheck_en():
    """Flat-dictionary spell-check for English via pyspellchecker.

    Adequate for English because inflection is comparatively minimal (a handful of
    surface forms per lemma, not dozens), so a frequency-based word list doesn't
    drown in false positives the way it does for Russian.
    """
    from spellchecker import SpellChecker
    spell = SpellChecker(language="en")

    def check(word: str) -> bool:
        return word in spell.unknown([word])

    return check


SPELLCHECK_BACKENDS = {"ru": _make_spellcheck_ru, "en": _make_spellcheck_en}


def spell_check(text: str, is_suspicious) -> Counter:
    words = [w.lower() for w in WORD_RE.findall(text) if len(w) > 2]
    suspicious = Counter()
    for w in words:
        if w in KNOWN_OK:
            continue
        if is_suspicious(w):
            suspicious[w] += 1
    return suspicious


def main() -> int:
    if not STEMS:
        print(f"No .md files found under {TEXT_DIR} -- adjust TEXT_DIR/STEMS for your project.")
        return 1

    print("=" * 70)
    print("CHARACTER HYGIENE SCAN")
    print("=" * 70)
    any_char_issue = False
    for stem in STEMS:
        text = (TEXT_DIR / f"{stem}.md").read_text(encoding="utf-8")
        issues = scan_char_hygiene(text)
        if issues:
            any_char_issue = True
            print(f"\n{stem}:")
            for line in issues:
                print(line)
    if not any_char_issue:
        print("No invisible/control characters or script-mixing found in any file. Clean.")

    print()
    print("=" * 70)
    print(f"SPELL-CHECK ({LANGUAGE})")
    print("=" * 70)
    if LANGUAGE not in SPELLCHECK_BACKENDS:
        print(f"No spell-check backend configured for language={LANGUAGE!r}; "
              f"add one to SPELLCHECK_BACKENDS.")
        return 0
    is_suspicious = SPELLCHECK_BACKENDS[LANGUAGE]()

    total_counter: Counter = Counter()
    per_file: dict[str, Counter] = {}
    for stem in STEMS:
        text = (TEXT_DIR / f"{stem}.md").read_text(encoding="utf-8")
        c = spell_check(text, is_suspicious)
        per_file[stem] = c
        total_counter.update(c)

    print(f"\nTotal distinct suspicious words: {len(total_counter)}\n")
    for stem in STEMS:
        flagged = sorted(per_file[stem].items())
        if flagged:
            print(f"{stem}:")
            for w, n in flagged:
                total = total_counter[w]
                print(f"    {w!r} (x{n} in file, x{total} total in corpus)")
    if not total_counter:
        print("No suspicious words found.")
    print(
        "\nReminder: triage every hit before fixing anything -- proper nouns, quoted "
        "dialect/archaic material, and a book's own symbolic notation will all show up "
        "here legitimately. See SKILL.md step 5."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
