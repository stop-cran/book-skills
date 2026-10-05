# book-skills

General-purpose [Copilot CLI](https://github.com/github/copilot-cli) skills for preparing
book text, consulting edition-specific sources, and producing chaptered, metadata-tagged
MP3 audiobooks via Azure neural TTS.

The PDF and audiobook skills were extracted from a real, complete project: producing a ~6.5-hour
Russian-language audiobook of Vladimir Propp's *Морфология волшебной сказки* (Morphology of
the Folktale) from a scanned PDF — see each `SKILL.md` for the concrete numbers, gotchas, and
decisions that came out of that run. The audiobook engine now also consumes the English
and Russian Hegel synopsis configurations without being copied into either book. English
and Russian are supported; other languages require grounded cleaner/abbreviation rules
and listening checks, not merely a changed language flag.
The source-consultation skill grew from edition-specific Hegel reviews and a separate
retrieval workbench; it does not treat the audiobook workflow as its source evidence.

## Skills

- **[`source-consultation`](.github/skills/source-consultation/SKILL.md)** — discover
  and inspect edition-specific evidence, compare earlier/later passages, and preserve
  the difference between retrieved leads, consulted transcriptions and checked editions.
  Uses a separate `source-workbench` checkout for deterministic retrieval; the skill
  retains scope/rights judgment, independent textual inspection and the repair loop.
  It is independently useful and does not require PDF conversion or audiobook generation.
- **[`pdf-to-markdown`](.github/skills/pdf-to-markdown/SKILL.md)** — extract a PDF book into
  clean, per-section Markdown files suitable for narration or any other reuse, with a
  targeted-patch-and-verify workflow for fixing OCR/extraction artifacts, plus validation
  passes (invisible/control characters, script-mixing/homoglyphs, morphological spell-check)
  that catch defects a purely visual proofread misses.
- **[`audiobook-from-markdown`](.github/skills/audiobook-from-markdown/SKILL.md)** — turn
  clean Markdown into a chaptered MP3 audiobook: sentence-safe chunking, gapless PCM
  synthesis against Azure real-time TTS with AAD auth, a resilient split-and-retry pattern for
  flaky preview endpoints, full ID3 tagging (artist/album/year/track-number/title) for
  audio-player navigation, and audible in-recording navigation conventions (spoken
  chapter/section heading at the start of each track, a clear spoken marker separating an
  AI-generated summary from the original text).
  Book-owned JSON configuration, exact chapter ranges, per-chunk checkpoints, and
  source/settings/audio-bound completion manifests prevent stale or partial recordings
  from being treated as complete. The companion `reference\workflow.py` adds durable
  narration plans, passage-selected listening previews, approval records and complete
  album verification without changing the renderer's cache identity.

## Typical workflow

1. Run `pdf-to-markdown` on the source PDF to get validated `.md` files in a `text/` folder
   (for a born-digital HTML/e-text source, see that skill's "Born-digital sources" section —
   the per-section split and the validation passes still apply; the PDF repair steps mostly
   don't).
2. (Optional) Have Copilot CLI draft a short preface and/or per-chapter summaries, reviewed by
   two independent-model rubber-duck subagents (different vendors, to reduce single-model
   bias) before they're inserted into the narrated text.
3. Keep one engine checkout, set `BOOK_SKILLS_ROOT`, and create a book-owned JSON
   configuration using the audiobook skill's worked example. Call its shared
   `reference\synthesize.py --project <book.json>`; no script copying or CONFIG editing
   is needed. Dry-run the exact selection and probe the voice/endpoint/rate. The probe
   is empirical, not a guarantee of the service limit.
4. Before batch generation, use `reference\workflow.py` to retain the prepared plan and
   representative previews in the book's ignored output directory. Inspect the selected
   text, render and listen to the samples, then record the scope/preview approval.
5. Render tagged MP3s and completion manifests with the plan's exact settings, then use
   `workflow.py verify` to check the exact album, tags, duration and full decoding.
   Mechanical verification never certifies human listening.

## Agent discovery

In Copilot CLI, `/add-dir <book-skills-checkout>` loads the trusted `.github` skills;
use `/skills` to inspect availability. Referencing this README alone does not install
a skill. For other agents, follow the linked `SKILL.md` explicitly.

For source consultation, a book can keep an ignored `.source-workbench.local.json`:

```json
{
  "book_skills_root": "C:\\checkouts\\book-skills",
  "workbench_root": "C:\\checkouts\\source-workbench",
  "config": "C:\\checkouts\\source-workbench\\workbench.local.toml"
}
```

These are placeholders; supply inspected absolute paths, not credentials. This routing
note is read by the agent, not passed to the CLI. If unavailable, supply
`BOOK_SKILLS_ROOT`, `SOURCE_WORKBENCH_ROOT` and `SOURCE_WORKBENCH_CONFIG` explicitly.
The runtime repository may require separate access; a public skill does not grant it.
Keep source-containing evidence in an ignored/private consultation directory.

Each skill is independently useful — you don't need the PDF source to run
`audiobook-from-markdown` against Markdown you already have, and you don't need to make an
audiobook to use `pdf-to-markdown` for some other reuse of a book's text.

## License

MIT — see [LICENSE](LICENSE).
