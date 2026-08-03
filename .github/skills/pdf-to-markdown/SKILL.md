---
name: pdf-to-markdown
description: >-
    Extract a scanned/PDF book into clean, validated, per-section Markdown files suitable
    for narration or any other reuse. Use when the user wants to turn a PDF (especially an
    OCR'd or older/scanned book) into trustworthy .md text, fix extraction artifacts
    (broken glyphs, misplaced footnotes, mixed-up columns, decorative-font title soup),
    or validate text quality (invisible characters, script-mixing/homoglyphs, spelling)
    before it feeds into a downstream pipeline such as audiobook-from-markdown.
user-invocable: true
---

# PDF → clean Markdown extraction

A repeatable workflow (not a single black-box script — every book's defects are different)
for turning a PDF into **clean, verified, per-section Markdown** files, with a
patch-and-verify loop that makes hand fixes auditable instead of silent, and validation
passes that catch defects a visual proofread tends to miss.

Worked example this was generalized from: a 143-page scanned Russian book (Propp,
*Морфология волшебной сказки*) extracted into 17 section files (a preface + 9 chapters + 4
appendices + 2 reference/bibliography sections), each hand-verified against rendered page
images where the PDF's text layer was untrustworthy.

## What it does

- **Extracts per-section, not one giant file.** Split the PDF into one `.md` file per
  logical unit (preface/intro, each chapter, each appendix, bibliography/notes) using the
  book's own table of contents or heading pattern to find section boundaries — this maps
  directly onto the per-file granularity `audiobook-from-markdown` expects later.
- **Treats the PDF's text layer as a hypothesis, not ground truth.** Decorative fonts (title
  pages, pull quotes, epigraphs, formulas) frequently have a broken `ToUnicode` CMap and
  extract as glyph soup that *looks* plausible in isolation but is wrong. Cross-reference
  anything suspicious against a **rendered page image** (e.g. via PyMuPDF/`fitz`,
  `page.get_pixmap()` → PNG) rather than trusting the extracted string.
- **Fixes defects through an auditable patch-and-verify loop** (see below) instead of
  editing the generated `.md` files by hand — hand edits get silently clobbered the next
  time the extractor re-runs, and leave no record of *why* a change was made.
- **Validates the result** with two automated passes that are cheap to run and catch classes
  of defect a human proofreader tends to skim past: character hygiene (invisible/control
  characters, script-mixing/homoglyphs) and morphological spell-checking.
- **Keeps titles canonical.** When a heading itself is in a broken decorative font, pull its
  wording from the book's table of contents (which is almost always set in ordinary body
  type and extracts cleanly) rather than trying to repair the glyph soup.

## What it does NOT do

- Does **not** produce a single generic `extract.py` you run unmodified on any PDF. Each
  book has its own layout quirks (multi-column tables, footnote placement, a formal notation
  system, dialect quotations) — this skill is a *procedure and a set of reusable mechanisms*
  (see `reference/`), not a turn-key script.
- Does **not** try to be a general OCR engine. It assumes a PDF with an extractable text
  layer (native or already-OCR'd) — `fitz`/`pdfplumber`/etc. for text extraction, not
  image-to-text OCR itself.
- Does **not** guess silently. Every hand-authored fix either visibly fires (tracked hit
  count > 0) or the pipeline warns loudly; every `PARAGRAPH_OVERRIDES`-style structural
  patch raises an exception if its anchor text isn't found, rather than doing nothing.
- Does **not** replace human judgment on genuinely ambiguous glyphs — when a rendered page
  image is itself unclear (poor scan quality), say so and ask, rather than guessing.

## Workflow

### 1. Slice the PDF into sections

Find each section's start/end page (or start/end marker string once the whole document is
extracted as one continuous paragraph stream — cutting on marker text is more robust than
hardcoding page numbers, since a title might land mid-page). Use the book's table of
contents to get the canonical (file stem, display title) list up front.

### 2. Extract paragraphs, fix mechanical layout artifacts

Common mechanical fixes, roughly in order of how often they're needed:

- **De-hyphenate line-wrap breaks** (`граждан-\nство` → `гражданство`) — but only when the
  break coincides with a line-wrap, not a genuine compound hyphen.
- **Rejoin footnotes split across a page boundary.** A footnote's marker (`*`, `¹`) sits at
  the point of reference, but its body text sits at the *bottom of the printed page* —
  which can land it mid-word inside the sentence it annotates, once page breaks are removed
  by naive concatenation. Detect the pattern (marker, then unrelated short text, then the
  word being annotated resuming) and relocate the footnote body to immediately follow the
  now-whole sentence.
- **Collapse spurious single-letter runs.** Letter-spaced/emphasised words (e.g. small-caps
  or spaced-out emphasis in the original typesetting) can extract as a run of single-letter
  tokens (`м ы` instead of `мы`). Collapse runs of ≥4 single-letter tokens automatically;
  leave shorter runs alone and fix them individually via `STRING_FIXES` — a blanket
  threshold that's too low will wrongly merge genuine short separate words.
- **Detect multi-column layouts.** A worked table or side-by-side text block (e.g. a tale
  transcript next to its structural analysis) extracts with lines interleaved from both
  columns in reading order, not left-column-then-right-column. This is usually visually
  obvious once you look at the raw extracted paragraph — flag it and handle with a
  `PARAGRAPH_OVERRIDES` structural replacement (see below), not a string fix.

### 3. The patch-and-verify loop (the core reusable mechanism)

See `reference/extraction_patches.py` for working code. Two complementary mechanisms, both
designed so a fix that stops applying is **loud, not silent**:

- **`STRING_FIXES`** — a list of `(old_exact_substring, new_text)` pairs applied via literal
  substring replacement, for small, localized defects (a single wrong glyph, a misplaced
  footnote, a wrong word). Literal substring matching (not regex) makes each fix trivially
  auditable — you can see exactly what text it targets. The catch: `str.replace` silently
  does nothing if `old` no longer appears (e.g. because you already re-ran a fix, or an
  earlier pipeline stage changed) — so every entry's hit count is tracked, and the pipeline
  prints a loud warning listing any entry that fired zero times across a full run. Comment
  each entry with *why* (which page, what kind of defect) for future maintainers.
- **`GLOBAL_REGEX_FIXES`** — a list of `(compiled_pattern, replacement)` pairs for
  systematic issues that recur throughout the book (e.g. a middle-dot `·` used as a
  sentence-ending period by the original typesetting, or spaced-out digit groups like
  `1 9 2 8` that should collapse to `1928`).
- **`PARAGRAPH_OVERRIDES`** — for defects spanning multiple paragraphs (the interleaved
  multi-column case above, or an epigraph that needs to be inserted from a hand transcript).
  Identify the affected range by **marker text** (a substring found in the first and last
  affected paragraph) rather than a paragraph index, since indices shift as earlier fixes
  change the paragraph count. If either marker isn't found, **raise immediately** — a
  structural fix silently not applying is much more dangerous than a small string fix not
  applying, since it can leave garbled multi-paragraph text in the final output.
- **Regen-consistency check.** After *any* change to the extraction logic (not the fix
  tables themselves — the code that walks the PDF), re-run the full extraction and diff the
  new output against the last-known-good `.md` files. An unexplained diff means the code
  change altered something you didn't intend; re-running should be a no-op except for the
  specific fix you just added. This is the single highest-leverage regression check in the
  whole pipeline, and it's nearly free to run.

### 4. Validate the result

Run `reference/validate_text_quality.py` (works out of the box for the character-hygiene
pass; needs a one-line language selection for the spell-check pass — see below) over every
extracted file:

- **Invisible/control character scan.** Zero-width spaces (U+200B), BOM (U+FEFF), soft
  hyphen (U+00AD), other zero-width joiners, and raw control characters are all invisible on
  screen but can make a downstream TTS model stammer or mispronounce. A clean extraction
  should have **zero** hits; treat any as a bug in the extraction step that introduced them.
- **Script-mixing / homoglyph scan.** OCR and copy-paste both commonly substitute a
  visually-identical Latin letter for a Cyrillic one (or vice versa) inside what should be a
  single-script word — invisible to the eye, but wrong to a screen reader or TTS engine, and
  a red flag for silent corruption elsewhere nearby. Flag any word mixing scripts unless it's
  legitimately an abbreviation/formula (a book with its own symbolic notation, like Propp's
  function-formula system, will have real mixed-alphabet content — verify each hit in
  context rather than assuming it's always corruption; see step 5).
- **Morphological spell-check, not a flat dictionary.** A flat word-list spellchecker is
  nearly useless for morphologically rich languages (Russian, and to a lesser extent most
  Slavic/agglutinative languages): every noun/verb/adjective has many inflected surface
  forms, so a small frequency list flags huge numbers of correctly-spelled words. Use a
  **morphological analyzer** instead — for Russian, `pymorphy3` parses a word form and
  reports both whether its lemma `is_known` to its dictionary *and* a confidence `score` for
  words it doesn't recognize (via suffix-based guessing). Flag only `is_known=False` **and**
  low score as a typo candidate; this cuts false positives by roughly two orders of
  magnitude versus a flat dictionary in practice. For English, a flat dictionary
  (`pyspellchecker`, or better, `enchant`/Hunspell if available) is adequate since English
  inflection is comparatively minimal.

### 5. Triage findings — don't assume every hit is a bug

Every flagged word or character needs a quick judgment call, not a blind auto-fix:

- Proper nouns and technical/domain terms (authors cited, place names, a discipline's own
  jargon) will always show up as "unknown" — build a small allowlist from the book's own
  front matter rather than repeatedly re-triaging them.
- A book that quotes dialect, archaic, or regional-language material verbatim (folk tale
  transcripts, historical documents) will contain real words a modern dictionary doesn't
  know — check surrounding context (is it inside quotation marks, attributed to a named
  source?) before treating it as an error.
- A book with its own symbolic/formulaic notation (mathematical, logical, or a bespoke
  scheme like Propp's Latin-letter function codes) will have legitimate mixed-script
  "words" — these are content, not corruption.
- When genuinely unsure, cross-check the disputed word/phrase against an independent public
  copy of the same text (a web search for the surrounding phrase is often enough to confirm
  or refute a suspected OCR error) rather than guessing from context alone.

## Language-specific hooks

Everything above is language-agnostic *except* these, which need a per-language value/table
supplied by the caller (see the `LANGUAGE_*` dicts in `reference/validate_text_quality.py`):

| Hook | Russian (worked example) | Other languages |
| --- | --- | --- |
| Spell-check backend | `pymorphy3` (morphological) | flat dictionary OK for low-inflection languages (English); consider a morphological analyzer for others (e.g. Spacy lemmatizer, Hunspell with affix rules) for richly-inflected ones |
| Sentence-final punctuation / quote styles | `»`/`„…"` (guillemets, low-high quotes) | `"…"`, `“…”`, etc. — affects any downstream sentence-boundary logic too |
| Known-proper-noun allowlist | Пропп, Афанасьев, Веселовский, ... | book-specific every time regardless of language |
| Homoglyph letter set | Latin ⟷ Cyrillic lookalikes (а/a, е/e, о/o, р/p, с/c, х/x, у/y, ...) | Latin ⟷ Greek, Latin ⟷ Cyrillic, or other pairs depending on source-language script |

## Troubleshooting

- **A `STRING_FIXES` entry never fires (hit count stays 0).** Either it already applied in
  a previous run and you're looking at stale output, the target text moved to a different
  pipeline stage (e.g. a global regex now runs before it and already changed the text), or
  there's a typo in the fix's `old` string (check for a stray/missing space, a smart-quote
  vs. straight-quote mismatch, or an em-dash vs. hyphen mismatch — these are the most common
  causes since the source text was itself extracted mechanically).
- **A `PARAGRAPH_OVERRIDES` marker isn't found.** An earlier fix changed the exact text of
  the marker itself — use a shorter, more stable substring as the marker (a proper noun or
  distinctive short phrase near the start/end of the affected range, not a whole sentence).
- **Regen-consistency diff shows unexpected changes after a code-only change.** The
  extraction logic (not the fix tables) is not supposed to change output — investigate
  before proceeding; this usually means a "generic" fix was actually book-specific, or a
  fix order dependency was violated (e.g. a global regex fix now runs before a string fix
  whose `old` text depended on the pre-regex spelling).
- **Homoglyph scanner flags legitimate content.** Check whether the book has its own
  symbolic/formulaic notation (see step 5) before treating every hit as corruption — but
  don't dismiss a hit just because it's inconvenient; verify each one.

## Guidelines for future improvements

- **Image-based verification could be semi-automated**: rendering every page suspected of a
  broken-font title/heading to PNG up front (rather than one at a time as issues surface)
  would shorten the iterate-verify-patch cycle.
- **A confidence-scored diff between two extraction attempts** (e.g. `pdfplumber` vs.
  `fitz`) could auto-flag pages where they disagree, narrowing which pages need manual
  image cross-referencing.
- **Extend the homoglyph/hygiene scanner** with additional confusable-script pairs as
  needed (Greek, Armenian, etc.) if a book's source language uses a different script.
