"""Reference implementation: the patch-and-verify mechanism for fixing PDF/OCR
extraction defects auditably, instead of hand-editing generated .md output (which
gets silently clobbered the next time the extractor re-runs, and leaves no record of
why a change was made).

Two complementary mechanisms:

- STRING_FIXES / GLOBAL_REGEX_FIXES: small, localized textual defects.
- PARAGRAPH_OVERRIDES: larger structural defects spanning multiple paragraphs.

Both are designed so that a fix which stops applying is LOUD, not silent -- the
single most important property of this pattern. A spell-check or read-aloud pass
might never surface a fix that quietly stopped firing; an explicit hit-count/warning
step will.

This file is a runnable, self-contained demonstration (see `__main__` at the
bottom) -- copy the mechanisms into your own extraction script and populate
STRING_FIXES / GLOBAL_REGEX_FIXES / PARAGRAPH_OVERRIDES with fixes specific to the
book you're extracting.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# STRING_FIXES: (exact_old_substring, new_text) pairs, applied via literal
# str.replace -- NOT regex. Literal substring matching makes each fix trivially
# auditable (you can see exactly what text it targets, with no pattern-matching
# surprises), at the cost of being fragile to any upstream text change.
#
# Comment every entry with WHY it's needed (page number if known, what kind of
# defect) -- six months later, "why is this here" is the first question anyone
# (including a future you) will ask.
# ---------------------------------------------------------------------------

STRING_FIXES: list[tuple[str, str]] = [
    # Example: a single mis-OCR'd glyph inside an otherwise-clean sentence,
    # found via the character-hygiene/homoglyph scan (see validate_text_quality.py).
    # ("окружающие н£с явления", "окружающие нас явления"),

    # Example: a footnote extracted at its page-break position, landing mid-word
    # inside the sentence it annotates (a footnote's body text physically sits at
    # the bottom of the printed page, unrelated to where a sentence happens to
    # wrap) -- relocate the footnote to immediately follow the now-whole sentence.
    # ("текст о ры- * Пример сноски. баке и рыбке", "текст о рыбаке и рыбке\n\n* Пример сноски."),
]

# Systematic issues that recur throughout the book -- a regex is worth the loss
# of per-instance auditability here because there are too many instances to list
# individually (e.g. every mid-word page-break hyphen, or every spaced-out digit
# group).
GLOBAL_REGEX_FIXES: list[tuple[re.Pattern, str]] = [
    # Example: a middle-dot used as a sentence-ending period by the original
    # typesetting.
    # (re.compile(r"·"), "."),

    # Example: spaced-out digit groups ("1 9 2 8" -> "1928").
    # (re.compile(r"\b(\d(?: \d){1,3})\b"), lambda m: m.group(1).replace(" ", "")),
]

# Tracks, across a run, how many times each STRING_FIXES entry actually fired. A
# hand-written fix whose `old` string never matches (typo, paragraph-boundary
# mismatch, or being written against text at the wrong pipeline stage -- e.g.
# before a GLOBAL_REGEX_FIXES entry runs and changes the target spelling) fails
# *silently*: str.replace is a no-op and the original defect stays in the output
# with no error. check_fix_usage() surfaces this.
FIX_HIT_COUNTS = [0] * len(STRING_FIXES)


def apply_fixes(text: str) -> str:
    for i, (old, new) in enumerate(STRING_FIXES):
        if old in text:
            FIX_HIT_COUNTS[i] += 1
            text = text.replace(old, new)
    for pattern, repl in GLOBAL_REGEX_FIXES:
        text = pattern.sub(repl, text)
    return text


def check_fix_usage() -> bool:
    """Call once after a full extraction run. Warns about any STRING_FIXES entry
    that matched zero times -- almost certainly a fix that silently failed to
    apply and needs investigation, not a fix that's simply no longer needed
    (if a fix is genuinely obsolete, delete it; don't leave it at zero hits).

    Returns True iff every entry fired at least once. As with
    `regen_consistency_check()`, the return value (not just the printed
    warning) is what lets a caller turn this into a real hard gate --
    e.g. `if not check_fix_usage(): sys.exit(1)` -- instead of a message a
    human might skim past without acting on it."""
    unused = [i for i, n in enumerate(FIX_HIT_COUNTS) if n == 0]
    if unused:
        print(f"WARNING: {len(unused)} STRING_FIXES entries never matched "
              f"(silently did nothing this run) -- investigate:")
        for i in unused:
            old, _ = STRING_FIXES[i]
            print(f"  [{i}] {old[:100]!r}")
        return False
    print(f"All {len(STRING_FIXES)} STRING_FIXES entries fired at least once. OK.")
    return True


# ---------------------------------------------------------------------------
# PARAGRAPH_OVERRIDES: for defects spanning multiple paragraphs -- e.g. a
# multi-column layout (a worked example printed as two side-by-side columns)
# whose lines interleave in extraction order rather than left-column-then-
# right-column, or an epigraph/formula that needs to be inserted verbatim from
# a hand transcript of the rendered page image.
#
# Keyed by file stem; each entry is (start_marker, end_marker, replacement).
# start_marker/end_marker are SUBSTRINGS (not full paragraph text and not
# indices) found in the first and last paragraph of the affected range --
# substrings survive earlier fixes changing paragraph counts/positions, where a
# hardcoded index would silently point at the wrong place after any edit
# upstream. Any text in the end paragraph *after* the end_marker is preserved
# and appended following the replacement (so a structural override doesn't
# accidentally eat the start of the next, unrelated paragraph).
# ---------------------------------------------------------------------------

PARAGRAPH_OVERRIDES: dict[str, list[tuple[str, str, list[str]]]] = {
    # Example -- a two-column worked-example block in "05-some-chapter" that
    # extracts interleaved; replace paragraphs from the one containing
    # "Первый маркер" through the one containing "Последний маркер" with the
    # hand-transcribed correct paragraph sequence.
    # "05-some-chapter": [
    #     ("Первый маркер", "Последний маркер", [
    #         "Первый маркер ... correctly reconstructed first paragraph.",
    #         "Correctly reconstructed second paragraph.",
    #     ]),
    # ],
}


def apply_paragraph_overrides(stem: str, paragraphs: list[str]) -> list[str]:
    for start_marker, end_marker, replacement in PARAGRAPH_OVERRIDES.get(stem, []):
        start_matches = [i for i, p in enumerate(paragraphs) if start_marker in p]
        end_matches = [i for i, p in enumerate(paragraphs) if end_marker in p]
        # Fail LOUD, not silent: an unapplied (or wrongly-applied) structural
        # override can leave multi-paragraph garbled text in the final
        # output, which is a much worse failure mode than a small string fix
        # silently not firing. Three ways this can go wrong, all rejected:
        #   1. a marker isn't found at all,
        #   2. a marker is ambiguous (matches more than one paragraph) --
        #      silently taking the first match risks patching the wrong
        #      occurrence with no indication anything was wrong,
        #   3. the resolved range is reversed/empty (end before start) --
        #      slicing would still "succeed" but silently duplicate or drop
        #      paragraphs instead of replacing the intended range.
        if not start_matches or not end_matches:
            raise ValueError(
                f"paragraph override markers not found for {stem}: "
                f"start_marker={start_marker!r} (found={len(start_matches)}), "
                f"end_marker={end_marker!r} (found={len(end_matches)})"
            )
        if len(start_matches) > 1 or len(end_matches) > 1:
            raise ValueError(
                f"paragraph override markers are ambiguous for {stem}: "
                f"start_marker={start_marker!r} matched paragraphs {start_matches}, "
                f"end_marker={end_marker!r} matched paragraphs {end_matches} "
                "-- use a longer/more specific substring so each marker matches exactly one paragraph."
            )
        start_i, end_i = start_matches[0], end_matches[0]
        if end_i < start_i:
            raise ValueError(
                f"paragraph override range is reversed for {stem}: "
                f"start_marker={start_marker!r} matched paragraph {start_i}, but "
                f"end_marker={end_marker!r} matched paragraph {end_i}, which comes before it."
            )
        # Preserve any trailing text in the end paragraph that comes after the
        # end_marker -- it belongs to what follows the override, not to it.
        end_para = paragraphs[end_i]
        cut_pos = end_para.find(end_marker)
        tail = end_para[cut_pos + len(end_marker):].strip() if cut_pos != -1 else ""
        tail_paragraphs = [tail] if tail else []
        paragraphs = paragraphs[:start_i] + replacement + tail_paragraphs + paragraphs[end_i + 1:]
    return paragraphs


# ---------------------------------------------------------------------------
# Regen-consistency check: after ANY change to the extraction *logic* (the code
# that walks the PDF -- not the fix tables above), re-run full extraction and
# diff against the last-known-good .md output. An unexplained diff means the
# code change touched something unintended. This is cheap (a full run is
# typically seconds) and is the single highest-leverage regression check in
# the whole pipeline -- run it after every extraction-logic change, not just
# before a final release.
# ---------------------------------------------------------------------------

def regen_consistency_check(stem: str, new_text: str, known_good_path: str) -> bool:
    """Return True iff `new_text` matches the known-good baseline (or no
    baseline existed yet, in which case one is written and this run passes
    trivially). Return False on any diff.

    The return value is what makes this usable as a hard gate, not just a
    printed diff a human might skim past: call sites that want regen
    consistency to actually BLOCK (a pre-commit hook, CI, or an agent
    deciding whether it's safe to overwrite generated output) should check
    it, e.g. `if not all(regen_consistency_check(...) for stem in STEMS): sys.exit(1)`.
    """
    import pathlib
    known_good = pathlib.Path(known_good_path)
    if not known_good.exists():
        print(f"[{stem}] no known-good file at {known_good_path} yet -- writing baseline.")
        known_good.write_text(new_text, encoding="utf-8")
        return True
    old_text = known_good.read_text(encoding="utf-8")
    if old_text != new_text:
        print(f"[{stem}] DIFFERS from known-good output -- review before overwriting:")
        import difflib
        diff = difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile="known-good", tofile="new", lineterm="",
        )
        for line in list(diff)[:40]:
            print(f"    {line}")
        return False
    print(f"[{stem}] unchanged. OK.")
    return True


if __name__ == "__main__":
    # Minimal smoke test of the two mechanisms using inline example data (the
    # commented-out entries above are illustrative only and intentionally not
    # active, since they reference book-specific text that doesn't exist here).
    demo_fixes = [("teh quick", "the quick")]
    demo_hits = [0] * len(demo_fixes)
    text = "teh quick fox"
    for i, (old, new) in enumerate(demo_fixes):
        if old in text:
            demo_hits[i] += 1
            text = text.replace(old, new)
    print("STRING_FIXES demo:", text, "hits:", demo_hits)

    demo_paragraphs = ["intro", "BROKEN start of a bad block", "bad middle", "bad END tail text", "outro"]
    demo_overrides = [("BROKEN start", "bad END", ["fixed single paragraph"])]
    start_i = next(i for i, p in enumerate(demo_paragraphs) if demo_overrides[0][0] in p)
    end_i = next(i for i, p in enumerate(demo_paragraphs) if demo_overrides[0][1] in p)
    cut_pos = demo_paragraphs[end_i].find(demo_overrides[0][1])
    tail = demo_paragraphs[end_i][cut_pos + len(demo_overrides[0][1]):].strip()
    result = demo_paragraphs[:start_i] + demo_overrides[0][2] + ([tail] if tail else []) + demo_paragraphs[end_i + 1:]
    print("PARAGRAPH_OVERRIDES demo:", result)
