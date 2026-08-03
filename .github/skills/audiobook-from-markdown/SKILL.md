---
name: audiobook-from-markdown
description: >-
    Generate a chaptered, metadata-tagged MP3 audiobook from clean Markdown text using
    Azure neural TTS (MAI-Voice-2 or similar). Use when the user wants to narrate a book,
    split narration into per-chapter/section audio files, add ID3 tags for audio-player
    navigation (artist/album/year/track number/title), handle a flaky preview TTS
    endpoint reliably, or add spoken navigation cues (chapter headings, AI-summary
    boundaries) so listeners can navigate by ear.
user-invocable: true
---

# Markdown → audiobook

Turns a folder of clean, per-section Markdown files into a folder of matching MP3s: one
file per preface/chapter/appendix, gapless within each file, fully ID3-tagged for sane
ordering and display in any audio player, synthesized via Azure's real-time TTS endpoint
with Microsoft Entra (AAD) auth.

Worked example this was generalized from: a 15-file, 6.5-hour Russian audiobook (Propp,
*Морфология волшебной сказки*) synthesized via `ru-RU-Lev:MAI-Voice-2` against a
preview Azure AI Foundry endpoint that turned out to be significantly flakier than a
production endpoint — the resilient split-and-retry pattern below exists because of that,
and is worth keeping even against a reliable endpoint (it costs nothing when unused).

## What it does

- **Cleans Markdown for narration**: strips formatting a TTS engine shouldn't read aloud
  literally (emphasis markers, raw heading `#`s, bullet/link syntax, horizontal rules) and
  renders anything a TTS engine would otherwise mangle or mispronounce (symbols, spaced-out
  digit groups, Roman numerals used as inline labels) — parsed into typed `Segment`s
  (`title` / `para` / `quote` / ...) so structural pauses and per-kind handling (e.g. a
  slightly longer pause before an epigraph) are driven by segment kind, not guessed from
  formatting after the fact.
- **Speaks each file's own heading at the very start**, and — for chapters headed by
  Roman numerals in print — speaks it as an ordinal word (e.g. "Глава третья" /
  "Chapter three"), not a spelled-out Roman numeral or bare digit, since that's how a human
  reader would say it aloud. This is also the primary audible navigation cue: a listener
  skipping between tracks by ear should always hear which section they just landed on
  within the first second or two of audio.
- **Marks the boundary of an AI-generated summary audibly**, if your workflow inserts one
  at the top of a chapter (see "AI-authored front matter" below) — e.g. spoken
  "Краткое содержание." ... [summary] ... "Конец краткого содержания." — so a listener can
  tell by ear where the summary ends and the book's actual original text begins, without
  needing to look at the player. Do **not** rely on a pause alone for this; a TTS pause and
  a chapter/paragraph pause sound alike, so an explicit spoken marker is what actually
  removes the ambiguity.
- **Chunks text to fit the endpoint's per-request limits** (character count and audio
  duration), splitting **only at sentence boundaries** — never mid-sentence, which corrupts
  intonation — using a per-language abbreviation/label list so `"см. гл. VII"` or
  `"Dr. Smith"` isn't mistaken for three sentence ends.
- **Synthesizes losslessly and gaplessly**: each chunk becomes PCM (not compressed audio),
  chunks are concatenated with deliberate silence gaps sized by structural context (longer
  before a new title, shorter between the split halves of one long paragraph), and the
  whole file is encoded to MP3 **once at the end** — so there's exactly one lossy encode
  per file, not one per chunk, and no audible seams at chunk boundaries.
- **Retries flaky requests, then splits and retries the halves** (`synth_pcm_resilient`):
  if a chunk fails even after a short bounded retry budget, split it in half at a sentence
  boundary (or a word boundary as a last resort) and retry each half independently,
  recursing further if needed down to a small floor size. This is the key resilience
  pattern for a flaky preview endpoint — see "Why split-and-retry, not just more retries"
  below for why this works when naive retry-with-backoff alone does not.
- **Tags every file with full ID3v2** metadata: artist, album (= book title), year, genre,
  a zero-padded `NN/total` track number for correct ordering in any player (`00` reserved
  for preface/introduction, continuing the same sequence through appendices), and a
  human-readable title derived from the file's own heading.
- **Is resumable and supports dry-run/smoke-test workflows**: `--dry-run` writes the
  prepared narration text with zero API calls; `--limit-chunks N` synthesizes only the
  first N chunks of one file for a quick listen; a full `--all` run skips files whose MP3
  already exists, so a partial failure just needs a re-run, not a restart from scratch.

## What it does NOT do

- Does **not** hardcode a TTS endpoint, resource name, or voice. These are always
  user/project-specific — resolve from CLI flags first, then environment variables, and
  fail with clear guidance (never guess or silently fall back to a default that might be
  wrong) if neither is set.
- Does **not** use API keys. Auth is AAD (`az login`) only — no secrets committed or
  stored, consistent with treating credentials as something that must never enter source
  control.
- Does **not** assume the endpoint is reliable. Even a production TTS endpoint can have
  transient failures; the split-and-retry pattern is cheap insurance either way.
- Does **not** invent chapter/section boundaries — it narrates exactly the per-file
  structure `pdf-to-markdown` (or however the Markdown was produced) already established.
- Does **not** write AI-generated summaries or prefaces itself as a hidden side effect —
  that's a distinct, human-reviewed step (see below), not something this skill does
  silently as part of synthesis.

## AI-authored front matter (prefaces / chapter summaries)

If part of the ask is to add an AI-written preface and/or short per-chapter summaries
before narrating:

1. Draft each piece separately (don't batch-draft the whole book in one pass — a
   preface needs the finished book's actual content to reference accurately).
2. **Review with two independent subagents, from different model vendors, before
   inserting the text into the narrated corpus.** Give each reviewer a clean context (not
   the conversation that produced the draft) and ask it to rubber-duck: catch factual
   errors, tone mismatches, and anything that reads as AI-generated filler — not to
   copy-edit style. Using two different vendors (not two runs of the same model) is
   specifically to catch a single model's blind spots/biases that a same-vendor second
   opinion would tend to share.
3. Apply feedback with judgment — a reviewer's suggestion that conflicts with the source
   author's actual claims should be held, not blindly applied.
4. Mark the summary's audible boundaries per the "Marks the boundary" point above **before**
   it goes into the file that gets chunked/synthesized — this is much easier to get right
   as an explicit `Segment` kind than to patch in after the fact.

## Chunking (language-agnostic core, per-language label list)

See `reference/chunk_text.py`. The algorithm (keep whole paragraphs when they fit under
budget; otherwise split at sentence boundaries; pack sentences greedily) is
language-agnostic. What's language-specific is exactly which trailing-period tokens do
**not** end a sentence — supply your own list via `LANGUAGE_ABBREVIATIONS`:

- Multi-letter abbreviations ending in a period that don't end a sentence (Russian:
  `см.`, `др.`, `гл.`; English: `Dr.`, `Mr.`, `etc.`, `vs.`).
- Single-letter initials (`А.`, `G.`) — covers author-initial citations and dates.
- Roman-numeral inline labels (`V.`, `XI.`) if the book uses them for cross-references
  inside prose (e.g. numbered functions/clauses/theorems), so `"...see clause V. This..."`
  isn't split after `V.`.
- Trailing-digit references (page/footnote citations like `"с. 42."` / `"p. 42."`).

## Synthesis and the resilient split-and-retry pattern

See `reference/synthesize.py`. Core pieces:

- `build_ssml()` — wraps each chunk in minimal SSML (`xml:lang`, voice selection); keep
  this minimal unless you have a specific reason for expressive styling — plain delivery
  is usually right for narration.
- `synth_pcm()` — a single synthesis attempt with capped-backoff retry (a handful of
  attempts, backoff capped low — seconds, not tens of seconds — so failures escalate to
  the split-fallback quickly rather than waiting them out).
- `synth_pcm_resilient()` — the wrapper that actually matters for a flaky endpoint: after
  `synth_pcm`'s bounded retry budget is exhausted, split the chunk's text in half (at a
  sentence boundary if it has more than one sentence, else a word boundary) and recurse on
  each half independently, stitching the results with a short silence. Give up and surface
  the error only below a minimum split size (a floor like 60 characters), to avoid infinite
  recursion on a stubbornly-failing tiny fragment.

### Why split-and-retry, not just more retries

Against a flaky preview endpoint, failures were empirically **not correlated with text
length or specific content** — the exact same short chunk could fail repeatedly while an
isolated hand-typed short phrase of similar length always succeeded, and bisecting a
reliably-failing paragraph down to under 100 characters didn't reveal a specific triggering
character or construct. That pointed to backend-instance-level flakiness (which request
happens to land on a bad backend instance) rather than a deterministic client-side trigger.
**Splitting doesn't fix the underlying flakiness — it just gives the request more
independent chances to land on a healthy backend instance**, since each half is a separate
request. Empirically this resolved 100% of failures across a full multi-hour production
run (many chunks needed 1-4 rounds of splitting). If your endpoint instead fails
deterministically on specific content (same input always fails, isolated or not), that's a
different problem — a real content/SSML trigger — and needs a targeted fix in the cleaner,
not more retrying.

## ID3 tagging convention

| Tag | Value |
| --- | --- |
| `TPE1`/`TPE2` (artist) | book's author |
| `TALB` (album) | book title |
| `TDRC` (year) | original publication year |
| `TCON` (genre) | `Audiobook` |
| `TRCK` (track) | `NN/total`, zero-padded; `00` = preface/intro, continuing sequentially through every chapter and appendix so player sort order matches reading order |
| `TIT2` (title) | derived from the file's own top heading — single source of truth, so the spoken narration and the displayed title can never drift out of sync; rewrite a heading's Roman numeral to an Arabic digit for the *tag* even if the *spoken* audio says the ordinal word form ("Глава третья"), since tag text is read on a screen, not heard |

## How to use

```powershell
pip install -r requirements.txt      # azure-identity, requests, imageio-ffmpeg, mutagen
az login                              # need "Cognitive Services Speech User" on the resource

$env:TTS_RESOURCE = "<your-foundry-resource-name>"   # or $env:TTS_ENDPOINT for a full URL

python synthesize.py --dry-run --all              # inspect prepared text, zero API calls
python synthesize.py text\03-chapter-3.md --limit-chunks 2   # smoke test one file
python synthesize.py --all                         # full batch -> audio\*.mp3
```

`--dry-run` needs no endpoint. A real run resumes automatically (skips existing MP3s);
add `--force` to re-render. Always smoke-test one small file end-to-end (including a
listen) before committing to a full multi-hour batch run — this is the cheapest point to
catch a wrong voice, wrong language tag, or bad pacing.

## Troubleshooting

- **`No TTS endpoint configured`** — set `$env:TTS_RESOURCE`/`$env:TTS_ENDPOINT` or pass
  `--resource`/`--endpoint`.
- **`AzureCliCredential` timeout** — the default subprocess timeout can expire when `az`
  is cold on Windows; use a longer `process_timeout` (e.g. 30s) and/or run
  `az account get-access-token --resource https://cognitiveservices.azure.com` once first
  to warm the CLI.
- **401/403** — identity lacks the **Cognitive Services Speech User** role on the resource
  (a generic "Cognitive Services User" role is not sufficient for Speech), or the resource
  has no custom domain configured.
- **404** — the AAD real-time TTS path is `/tts/cognitiveservices/v1`, not
  `/cognitiveservices/v1`.
- **Transient 502/503 on varying chunks, not always the same one** — this is the flaky-
  backend-instance case; confirm `synth_pcm_resilient` is wired in (not just `synth_pcm`
  directly) and let it split-and-retry.
- **The exact same chunk 502s every single run, never any other chunk** — a genuine
  content/SSML trigger, not infra flakiness (e.g. a raw pipe-dense Markdown table row
  reaching the endpoint unstripped). Inspect that chunk's dry-run text and add a targeted
  cleaner rule; splitting won't fix a deterministic trigger, only bad luck.
- **Robotic pacing** — a paragraph split badly; confirm the chunker only splits at
  sentence boundaries, or add a `--rate` adjustment.
- **`ffmpeg failed`** — reinstall `imageio-ffmpeg` (bundled binary, no system install
  needed).

## Guidelines for future improvements

- **Batch/long-audio synthesis API**, if the voice supports it, would cut many sequential
  real-time requests down to a handful of async jobs — worth checking before assuming the
  real-time endpoint is the only option.
- **Bounded parallelism** (a handful of concurrent workers with rate-limit handling) would
  shorten a large batch run, quota permitting.
- **Per-language spoken-ordinal tables** (chapter/appendix number → ordinal word, e.g.
  Russian "третья", English "third") should live in one place per language, not be
  re-derived per book.
- **Cross-reference narration** ("see Chapter V") could be verbalized consistently with
  however chapter headings themselves are spoken, rather than left as a bare label.
