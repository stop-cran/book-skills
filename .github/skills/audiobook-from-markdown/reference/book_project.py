"""Load book-owned JSON configuration without copying or editing the engine."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BookProject:
    path: Path
    data: dict
    sources: dict[str, Path]
    sections: dict[str, list[str]]
    tracks: dict[str, int]


def _object(value, name: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name}: unknown field(s): {', '.join(sorted(unknown))}")
    return value


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def chapter_number(stem: str) -> int | None:
    match = re.match(r"^(\d+)-", stem)
    return int(match[1]) if match else None


def read_project(path: Path) -> BookProject:
    path = path.resolve()
    data = _object(json.loads(path.read_text(encoding="utf-8")), "project", {
        "schema_version", "text_dir", "pattern", "chapters", "out_dir", "language",
        "xml_lang", "voice", "env_prefix", "metadata", "narration", "additional_sources",
        "allow_external_inputs",
    })
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("project.schema_version must be 1")
    for name in ("text_dir", "out_dir", "language", "xml_lang", "voice"):
        _text(data.get(name), name)
    if data["language"] not in ("ru", "en"):
        raise ValueError("Supported languages are ru and en")
    if not re.fullmatch(data["language"] + r"-[A-Z]{2}", data["xml_lang"]):
        raise ValueError("xml_lang must match language, e.g. ru-RU for ru")
    if not data["voice"].startswith(data["xml_lang"] + "-"):
        raise ValueError("voice locale must match xml_lang")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", _text(data.get("env_prefix", "TTS"), "env_prefix")):
        raise ValueError("env_prefix must be an uppercase environment-variable prefix")
    if type(data.get("allow_external_inputs", False)) is not bool:
        raise ValueError("allow_external_inputs must be a boolean")
    if data.get("allow_external_inputs") and "chapters" in data:
        raise ValueError("A chapter-bounded project cannot allow external inputs")
    metadata = _object(data.get("metadata"), "metadata", {"artist", "album", "year", "comment"})
    for name in ("artist", "album", "year"):
        value = _text(metadata.get(name), f"metadata.{name}")
        if "CHANGE ME" in value:
            raise ValueError(f"metadata.{name} still contains a placeholder")
    if not re.fullmatch(r"\d{4}", metadata["year"]):
        raise ValueError("metadata.year must be a four-digit year")
    if "comment" in metadata:
        _text(metadata["comment"], "metadata.comment")
    narration = _object(data.get("narration", {}), "narration", {
        "notation", "tables", "strip_section_numbers", "spoken_track_prefix", "opening", "fenced_blocks",
        "roman_references",
        "reference_cases",
    })
    if narration.get("notation", "plain") not in ("plain", "scientific"):
        raise ValueError("narration.notation must be plain or scientific")
    if narration.get("tables", "error") not in ("error", "skip"):
        raise ValueError("narration.tables must be error or skip")
    if narration.get("fenced_blocks", "error") not in ("error", "diagram"):
        raise ValueError("narration.fenced_blocks must be error or diagram")
    if narration.get("roman_references", "section") not in ("section", "part"):
        raise ValueError("narration.roman_references must be section or part")
    cases = narration.get("reference_cases", {})
    if not isinstance(cases, dict):
        raise ValueError("narration.reference_cases must map preceding phrases to Russian grammatical cases")
    if cases and data["language"] != "ru":
        raise ValueError("narration.reference_cases is Russian-only")
    for phrase, case in cases.items():
        _text(phrase, "reference_cases phrase")
        if case not in ("nominative", "genitive", "dative", "accusative", "instrumental", "prepositional"):
            raise ValueError(f"Invalid Russian reference case: {case!r}")
    if type(narration.get("strip_section_numbers", False)) is not bool:
        raise ValueError("narration.strip_section_numbers must be a boolean")
    if "spoken_track_prefix" in narration:
        prefix = _text(narration["spoken_track_prefix"], "narration.spoken_track_prefix")
        if re.search(r"[{}]", prefix.replace("{number}", "")):
            raise ValueError("spoken_track_prefix supports only the {number} placeholder")
    if "opening" in narration:
        _text(narration["opening"], "narration.opening")

    text_dir = (path.parent / data["text_dir"]).resolve()
    if not text_dir.is_dir():
        raise ValueError(f"text_dir does not exist: {text_dir}")
    pattern = _text(data.get("pattern", "*.md"), "pattern")
    sources: dict[str, Path] = {}
    sections: dict[str, list[str]] = {}

    def add(source: Path, stem: str) -> None:
        if not re.fullmatch(r"[^/\\:]+", stem) or stem in (".", ".."):
            raise ValueError(f"Invalid output stem: {stem!r}")
        if stem in sources:
            raise ValueError(f"Duplicate output stem: {stem}")
        if source.suffix.lower() != ".md" or not source.is_file():
            raise ValueError(f"Markdown source not found: {source}")
        sources[stem] = source.resolve()

    for source in sorted(text_dir.glob(pattern)):
        add(source, source.stem)
    extra = data.get("additional_sources", [])
    if not isinstance(extra, list):
        raise ValueError("additional_sources must be an array")
    for entry in extra:
        _object(entry, "additional source", {"path", "stem", "intro_and_sections"})
        source = path.parent / _text(entry.get("path"), "additional source.path")
        stem = _text(entry.get("stem", source.stem), "additional source.stem")
        add(source, stem)
        if "intro_and_sections" in entry:
            names = entry["intro_and_sections"]
            if not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names):
                raise ValueError("intro_and_sections must be an array of heading names")
            sections[stem] = names
    sources = dict(sorted(sources.items()))
    tracks = {}
    for index, stem in enumerate(sources, 1):
        number = chapter_number(stem)
        tracks[stem] = number if number is not None else index
    if len(set(tracks.values())) != len(tracks):
        raise ValueError("Duplicate track numbers; use unique numeric filename prefixes")
    if "chapters" in data:
        bounds = data["chapters"]
        if (not isinstance(bounds, list) or len(bounds) != 2
                or any(type(n) is not int for n in bounds) or not 0 <= bounds[0] <= bounds[1]):
            raise ValueError("chapters must be [first, last], inclusive, with nonnegative integers")
        required = set(range(bounds[0], bounds[1] + 1))
        numbered = {s: n for s in sources if (n := chapter_number(s)) is not None}
        missing = required - set(numbered.values())
        if missing:
            raise ValueError(f"Missing chapter number(s): {sorted(missing)}")
        sources = {s: p for s, p in sources.items() if numbered.get(s) in required}
        tracks = {s: tracks[s] for s in sources}
    if not sources:
        raise ValueError("Project selected no Markdown sources")
    return BookProject(path, data, sources, sections, tracks)


def select_intro_and_sections(md: str, names: list[str]) -> str:
    """Keep the H1/intro and explicitly named sections, not a book's TOC."""
    out, found = [], set()
    keep = True
    for line in md.splitlines():
        match = re.match(r"^#{2,6}\s+(.+)$", line)
        if match:
            title = match[1].strip()
            keep = title in names
            if keep:
                found.add(title)
        if keep:
            out.append(line)
    missing = set(names) - found
    if missing:
        raise ValueError(f"Selected section heading(s) not found: {sorted(missing)}")
    return "\n".join(out)
