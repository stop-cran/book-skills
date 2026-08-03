# book-skills

Two general-purpose [Copilot CLI](https://github.com/github/copilot-cli) skills for turning a
scanned/PDF book into a clean, reusable Markdown corpus, and turning that Markdown into a
chaptered, metadata-tagged MP3 audiobook via Azure neural TTS.

They were extracted and generalized from a real, complete project: producing a ~6.5-hour
Russian-language audiobook of Vladimir Propp's *Морфология волшебной сказки* (Morphology of
the Folktale) from a scanned PDF — see each `SKILL.md` for the concrete numbers, gotchas, and
decisions that came out of that run. Everything here is language-agnostic by design, with the
Russian-specific bits factored out into clearly-marked, swappable pieces (abbreviation lists,
spoken-ordinal tables, a morphological spell-checker) so the same pipeline works for English,
Russian, or another language with a bit of per-language configuration.

## Skills

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

## Typical workflow

1. Run `pdf-to-markdown` on the source PDF to get validated `.md` files in a `text/` folder.
2. (Optional) Have Copilot CLI draft a short preface and/or per-chapter summaries, reviewed by
   two independent-model rubber-duck subagents (different vendors, to reduce single-model
   bias) before they're inserted into the narrated text.
3. Run `audiobook-from-markdown` on the `text/` folder to get tagged `.mp3` files in an
   `audio/` folder.

Each skill is independently useful — you don't need the PDF source to run
`audiobook-from-markdown` against Markdown you already have, and you don't need to make an
audiobook to use `pdf-to-markdown` for some other reuse of a book's text.

## License

MIT — see [LICENSE](LICENSE).
