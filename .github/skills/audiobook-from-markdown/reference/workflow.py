"""Prepare durable run records, select listening samples, and verify complete albums.

This companion imports the unchanged renderer; editing it does not invalidate PCM
cache identities. Only the preview command can authenticate or call Azure.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mutagen import MutagenError
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

import synthesize as s


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def object_fields(value, required: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(f"{label}: expected fields {', '.join(sorted(required))}")
    return value


def positive_integer(value, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def prepare_plan(project: Path, inputs: list[str], chapters: str | None,
                 out_dir: Path | None, voice: str | None, rate: str | None,
                 max_chars: int | None, resource: str | None, endpoint: str | None,
                 *, recorded_stems: list[str] | None = None) -> dict:
    project = project.resolve()
    project_sha = s.file_hash(project)
    pipeline_sha = s.pipeline_hash()
    s.configure_project(project)
    s.ENDPOINT = s.resolve_endpoint(resource, endpoint)
    if not s.ENDPOINT:
        raise ValueError("Supply --resource/--endpoint or the project's endpoint environment variable; "
                         "preparation and verification do not log in or contact Azure")
    voice = voice or s.VOICE
    if not voice.startswith(s.XML_LANG + "-"):
        raise ValueError("Voice locale must match the project's xml_lang")
    if rate is not None:
        text(rate, "rate")
    if max_chars is not None:
        positive_integer(max_chars, "max_chars")
    budget, _ = s.resolve_max_chars(max_chars, voice, s.ENDPOINT, rate)
    out_dir = (out_dir or s.AUDIO_DIR).resolve()
    catalog = set(s.NARRATED_STEMS)
    args = argparse.Namespace(inputs=inputs, chapters=chapters, out_dir=str(out_dir), all=not inputs)
    if recorded_stems is None:
        jobs = s.resolve_inputs(args)
    else:
        if len(recorded_stems) != len(inputs) or len(set(recorded_stems)) != len(recorded_stems):
            raise ValueError("Prepared tracks must identify each input once")
        # A source path is not an identity: configured aliases can share a file.
        for stem, source in zip(recorded_stems, inputs, strict=True):
            if stem not in catalog:
                external = s.resolve_inputs(argparse.Namespace(
                    inputs=[glob.escape(source)], chapters=chapters, out_dir=str(out_dir), all=False))
                if len(external) != 1 or external[0][0] != stem:
                    raise ValueError(f"{stem}: prepared source identity changed")
            if s.source_path(stem) != Path(source):
                raise ValueError(f"{stem}: prepared source path changed")
        args.inputs, args.all = [], True
        available = {job[0]: job for job in s.resolve_inputs(args)}
        if chapters and set(recorded_stems) != set(available):
            raise ValueError("Prepared tracks do not match the requested chapter range")
        jobs = [available[stem] for stem in recorded_stems]
    tracks = []
    for stem, output, number in jobs:
        source = s.source_path(stem)
        source_sha = s.file_hash(source)
        chunks = s.load_chunks(stem, budget)
        oversized = s.oversized_chunks([(stem, chunks)], budget)
        if oversized:
            raise ValueError("Over-budget narration: " + "; ".join(oversized))
        total = len(catalog) if stem in catalog else len(s.NARRATED_STEMS)
        request = s.render_request(stem, chunks, voice, rate, budget, number, total, source_sha)
        tracks.append({
            "stem": stem, "source_path": str(source), "output": str(output),
            "request": request, "chunks": [[c.text, c.pre_pause_ms, c.kind] for c in chunks],
        })
    plan = {
        "project": str(project), "project_sha256": project_sha, "pipeline_sha256": pipeline_sha,
        "selection": {"inputs": [t["source_path"] for t in tracks], "chapters": chapters,
                      "out_dir": str(out_dir)},
        "settings": {"voice": voice, "rate": rate, "max_chars": budget,
                     "endpoint_id": s.endpoint_id(s.ENDPOINT)},
        "tracks": tracks,
    }
    check_sources(plan)
    return plan


def check_sources(plan: dict) -> None:
    if s.file_hash(Path(plan["project"])) != plan["project_sha256"]:
        raise ValueError("Project changed after preparation; prepare a new run record")
    if s.pipeline_hash() != plan["pipeline_sha256"]:
        raise ValueError("Renderer changed after preparation; prepare a new run record")
    for track in plan["tracks"]:
        if s.file_hash(Path(track["source_path"])) != track["request"]["source_sha256"]:
            raise ValueError(f"{track['stem']}: source changed after preparation")


def record_location(path: Path, plan: dict) -> Path:
    path = path.resolve()
    if not path.name.endswith(".run.json") or not path.is_relative_to(Path(plan["selection"]["out_dir"])):
        raise ValueError("Run record must be an *.run.json file inside the selected output directory")
    return path


def create_record(path: Path, plan: dict) -> dict:
    path = record_location(path, plan)
    if path.exists():
        raise ValueError(f"Run record already exists: {path}; use a new name, not an overwritten approval")
    record = {
        "version": 1, "created_utc": utc_now(), "workflow_sha256": s.file_hash(Path(__file__)),
        "plan": plan, "plan_sha256": s.json_hash(plan), "previews": {},
        "approval": None, "verification": None,
    }
    s.write_json(path, record)
    return record


def preview_spec(plan: dict, name: str, stem: str, selection: dict) -> dict:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
        raise ValueError("Preview name must be 1-80 ASCII letters, digits, hyphens or underscores")
    track = next((t for t in plan["tracks"] if t["stem"] == stem), None)
    if track is None:
        raise ValueError(f"Preview stem is not in the prepared selection: {stem}")
    chunks = track["chunks"]
    object_fields(selection, {"chunks", "from_text", "through_text"}, "preview selection")
    if selection["chunks"] is not None:
        if selection["from_text"] is not None or selection["through_text"] is not None:
            raise ValueError("Use a chunk range or text anchors, not both")
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", text(selection["chunks"], "chunks"))
        if not match:
            raise ValueError("Chunk selection must be a 1-based inclusive range, e.g. 3-8")
        first, last = int(match[1]), int(match[2] or match[1])
    else:
        def anchor(value: str) -> int:
            value = text(value, "text anchor")
            hits = [(i, c[0].count(value)) for i, c in enumerate(chunks, 1) if value in c[0]]
            if sum(count for _, count in hits) != 1:
                raise ValueError(f"Text anchor must occur exactly once in prepared chunks: {value!r}")
            return hits[0][0]

        first = anchor(selection["from_text"])
        last = anchor(selection["through_text"]) if selection["through_text"] is not None else first
    if not 1 <= first <= last <= len(chunks):
        raise ValueError(f"Preview range must be ordered and within 1-{len(chunks)}")
    output = Path(plan["selection"]["out_dir"]) / f"preview-{stem}-{name}.smoke.mp3"
    if output.stem in s.NARRATED_STEMS:
        raise ValueError("Preview filename collides with a configured chapter; use another preview name")
    return {"stem": stem, "selection": selection, "chunk_numbers": list(range(first, last + 1)),
            "chunks": chunks[first - 1:last], "output": str(output)}


def load_record(path: Path, resource: str | None, endpoint: str | None) -> dict:
    record = object_fields(json.loads(path.read_text(encoding="utf-8")), {
        "version", "created_utc", "workflow_sha256", "plan", "plan_sha256",
        "previews", "approval", "verification",
    }, "run record")
    if type(record["version"]) is not int or record["version"] != 1:
        raise ValueError("Unsupported run record version")
    plan = object_fields(record["plan"], {
        "project", "project_sha256", "pipeline_sha256", "selection", "settings", "tracks",
    }, "plan")
    if s.json_hash(plan) != record["plan_sha256"]:
        raise ValueError("Run plan checksum mismatch")
    selection = object_fields(plan["selection"], {"inputs", "chapters", "out_dir"}, "selection")
    settings = object_fields(plan["settings"], {"voice", "rate", "max_chars", "endpoint_id"}, "settings")
    inputs = selection["inputs"]
    if not isinstance(inputs, list) or not inputs or any(not isinstance(p, str) or not p for p in inputs):
        raise ValueError("Prepared inputs must be a nonempty array of paths")
    tracks = plan["tracks"]
    if not isinstance(tracks, list) or any(not isinstance(track, dict) for track in tracks):
        raise ValueError("Prepared tracks must be an array of objects")
    recorded_stems = [text(track.get("stem"), "track stem") for track in tracks]
    if selection["chapters"] is not None:
        text(selection["chapters"], "chapters")
    current = prepare_plan(
        Path(text(plan["project"], "project")), inputs, selection["chapters"],
        Path(text(selection["out_dir"], "out_dir")), text(settings["voice"], "voice"),
        settings["rate"], positive_integer(settings["max_chars"], "max_chars"), resource, endpoint,
        recorded_stems=recorded_stems)
    if current != plan:
        raise ValueError("Prepared source, settings, selection or renderer changed; prepare a new run record")
    record_location(path, plan)
    if not isinstance(record["previews"], dict):
        raise ValueError("previews must be an object")
    for name, entry in record["previews"].items():
        object_fields(entry, {"stem", "selection", "chunk_numbers", "chunks", "output", "audio"}, "preview")
        expected = preview_spec(plan, name, entry["stem"], entry["selection"])
        if {key: value for key, value in entry.items() if key != "audio"} != expected:
            raise ValueError(f"{name}: preview does not match the prepared passage")
    if record["approval"] is not None:
        approval = object_fields(record["approval"], {
            "recorded_utc", "plan_sha256", "previews_sha256", "human_listened", "scope_approved", "note",
        }, "approval")
        if (not record["previews"] or approval["plan_sha256"] != record["plan_sha256"]
                or approval["previews_sha256"] != s.json_hash(record["previews"])
                or approval["human_listened"] is not True or approval["scope_approved"] is not True):
            raise ValueError("Approval does not match the current plan and previews")
    return record


def check_audio(path: Path, audio: dict) -> float:
    object_fields(audio, {"sha256", "bytes", "seconds"}, "audio receipt")
    positive_integer(audio["bytes"], "audio bytes")
    seconds = audio["seconds"]
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{path.name}: receipt duration must be finite and positive")
    if path.stat().st_size != audio["bytes"] or s.file_hash(path) != audio["sha256"]:
        raise ValueError(f"{path.name}: audio bytes/checksum mismatch")
    measured = MP3(path).info.length
    if not math.isfinite(measured) or measured <= 0 or abs(measured - seconds) >= 0.25:
        raise ValueError(f"{path.name}: MP3 duration differs from the PCM receipt by at least 0.25 seconds")
    decoded = subprocess.run(
        [s._ffmpeg_exe(), "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        capture_output=True)
    if decoded.returncode:
        raise ValueError(f"{path.name}: full MP3 decoding failed: "
                         f"{decoded.stderr.decode(errors='replace')[:400]}")
    if path.stat().st_size != audio["bytes"] or s.file_hash(path) != audio["sha256"]:
        raise ValueError(f"{path.name}: audio changed during verification")
    return measured


def render_preview(path: Path, record: dict, name: str, spec: dict, dry_run: bool) -> None:
    old = record["previews"].get(name)
    if old is not None and {k: v for k, v in old.items() if k != "audio"} != spec:
        raise ValueError(f"Preview name {name!r} already selects a different passage; use a new name")
    output = Path(spec["output"])
    if old is None:
        if output.exists():
            raise ValueError(f"Unrecorded preview already exists: {output}; use a new name")
        record["previews"][name] = {**spec, "audio": None}
        record["approval"] = None
        s.write_json(path, record)
    entry = record["previews"][name]
    print(f"Preview {name}: {spec['stem']}, chunks {spec['chunk_numbers'][0]}-{spec['chunk_numbers'][-1]}")
    for number, chunk in zip(spec["chunk_numbers"], spec["chunks"]):
        print(f"  [{number}; {chunk[2]}; pause {chunk[1]} ms] {chunk[0]}")
    if dry_run:
        print("Prepared preview only; no audio synthesized and no listening claimed.")
        return
    if entry["audio"] is not None:
        check_audio(output, entry["audio"])
        print(f"Verified existing preview: {output.name}; no synthesis.")
        return
    if output.exists():
        raise ValueError(f"Preview has no completion receipt: {output}; use a new name")
    settings = record["plan"]["settings"]
    get_token = s.make_token_provider()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=output.stem + ".", suffix=".pending.mp3")
    os.close(fd)
    pending = Path(temporary)
    try:
        chunks = [s.Chunk(c[0], c[1], c[2]) for c in spec["chunks"]]
        seconds = s.synthesize_file(chunks, pending, get_token, settings["voice"], settings["rate"],
                                    output.parent / ".cache" / spec["stem"])
        audio = {"sha256": s.file_hash(pending), "bytes": pending.stat().st_size, "seconds": seconds}
        check_audio(pending, audio)
        check_sources(record["plan"])
        os.replace(pending, output)
        entry["audio"] = audio
        s.write_json(path, record)
    finally:
        pending.unlink(missing_ok=True)
    print(f"Ready for human listening: {output} ({seconds:.1f} seconds)")


def approve(path: Path, record: dict, note: str) -> None:
    if not record["previews"]:
        raise ValueError("No previews exist to approve")
    for name, entry in record["previews"].items():
        if entry["audio"] is None:
            raise ValueError(f"{name}: preview has not been rendered")
        check_audio(Path(entry["output"]), entry["audio"])
    check_sources(record["plan"])
    record["approval"] = {
        "recorded_utc": utc_now(), "plan_sha256": record["plan_sha256"],
        "previews_sha256": s.json_hash(record["previews"]), "human_listened": True,
        "scope_approved": True, "note": note,
    }
    s.write_json(path, record)
    print("Recorded the operator's human-listening and scope-approval declaration; "
          "software cannot certify that listening took place.")


def check_tags(path: Path, request: dict) -> None:
    tags = ID3(path)
    if tags.version != (2, 3, 0):
        raise ValueError(f"{path.name}: expected ID3v2.3")
    expected = {
        "TIT2": request["title"], "TPE1": request["artist"], "TPE2": request["artist"],
        "TALB": request["album"], "TDRC": request["year"], "TCON": s.ID3_GENRE,
        "TRCK": f"{request['track']:02d}/{request['total']}",
    }
    for key, value in expected.items():
        if len(tags.getall(key)) != 1 or str(tags[key]) != value:
            raise ValueError(f"{path.name}: incorrect {key} metadata")
    comments = tags.getall("COMM")
    language = "rus" if s.LANGUAGE == "ru" else "eng"
    if request["comment"]:
        if (len(comments) != 1 or comments[0].lang != language
                or comments[0].text != [request["comment"]] or comments[0].desc != ""):
            raise ValueError(f"{path.name}: incorrect disclosure/comment metadata")
    elif comments:
        raise ValueError(f"{path.name}: unexpected comment metadata")


def verify(path: Path, record: dict) -> dict:
    plan = record["plan"]
    directory = Path(plan["selection"]["out_dir"])
    errors, rows = [], []
    expected = {Path(t["output"]).name for t in plan["tracks"]}
    actual = {p.name for p in directory.glob("*.mp3")
              if p.name in expected or not p.name.endswith(".smoke.mp3")}
    expected_receipts = {s.manifest_path(Path(t["output"])).name for t in plan["tracks"]}
    actual_receipts = {p.name for p in directory.glob("*.manifest.json")}
    for label, wanted, found in (("MP3", expected, actual), ("manifest", expected_receipts, actual_receipts)):
        if wanted != found:
            errors.append(f"{label} set mismatch: missing={sorted(wanted - found)}, unexpected={sorted(found - wanted)}")
    for track in plan["tracks"]:
        output, request = Path(track["output"]), track["request"]
        try:
            manifest = s.manifest_path(output)
            manifest_sha = s.file_hash(manifest)
            if not s.verified_existing(output, request):
                raise ValueError(f"{track['stem']}: recording is missing")
            receipt = json.loads(manifest.read_text(encoding="utf-8"))
            check_tags(output, request)
            seconds = check_audio(output, receipt["audio"])
            if s.file_hash(manifest) != manifest_sha:
                raise ValueError(f"{track['stem']}: manifest changed during verification")
            rows.append({"stem": track["stem"], "manifest": str(manifest), "manifest_sha256": manifest_sha,
                         "seconds": seconds, "bytes": receipt["audio"]["bytes"], "full_decode": "pass"})
            print(f"Verified {output.name}: {seconds / 60:.1f} min")
        except (OSError, ValueError, KeyError, MutagenError) as exc:
            errors.append(f"{track['stem']}: {exc}")
    check_sources(plan)
    report = {
        "version": 1, "checked_utc": utc_now(), "plan_sha256": record["plan_sha256"],
        "verifier_sha256": s.file_hash(Path(__file__)), "status": "failed" if errors else "passed",
        "tracks": rows, "errors": errors, "seconds": sum(t["seconds"] for t in rows),
        "bytes": sum(t["bytes"] for t in rows), "listening_certified": False,
        "approval_recorded": record["approval"] is not None,
    }
    report_path = path.with_name(path.name.removesuffix(".run.json") + ".verification.json")
    s.write_json(report_path, report)
    record["verification"] = {"path": str(report_path.resolve()), "sha256": s.file_hash(report_path),
                              "status": report["status"], "checked_utc": report["checked_utc"]}
    s.write_json(path, record)
    for error in errors:
        print(f"FAILED: {error}", file=sys.stderr)
    print(f"Mechanical verification {report['status']}: {len(rows)}/{len(plan['tracks'])} tracks; "
          f"{report['seconds'] / 3600:.2f} hours. No synthesis or listening certification.")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", help="freeze exact prepared narration; no Azure calls")
    plan_parser.add_argument("--project", required=True, type=Path)
    plan_parser.add_argument("--chapters")
    plan_parser.add_argument("--out-dir", type=Path)
    plan_parser.add_argument("--voice")
    plan_parser.add_argument("--rate")
    plan_parser.add_argument("--max-chars", type=int)
    plan_parser.add_argument("--all", action="store_true")
    plan_parser.add_argument("inputs", nargs="*")
    preview_parser = commands.add_parser("preview", help="select complete prepared chunks; only this command can synthesize")
    preview_parser.add_argument("--name", required=True)
    preview_parser.add_argument("--stem", required=True, help="exact stem from the run plan")
    selectors = preview_parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--chunks", help="1-based inclusive range, e.g. 3-8")
    selectors.add_argument("--from-text", help="unique literal substring in a prepared chunk")
    preview_parser.add_argument("--through-text", help="unique literal substring in the last chunk, inclusive")
    preview_parser.add_argument("--dry-run", action="store_true", help="record/print selection without synthesis")
    approval_parser = commands.add_parser("approve", help="record a human listening/scope decision, not a software attestation")
    approval_parser.add_argument("--listened", action="store_true", required=True)
    approval_parser.add_argument("--scope-approved", action="store_true", required=True)
    approval_parser.add_argument("--note", default="", help="short approval note; never include credentials")
    verification_parser = commands.add_parser("verify", help="check exact album, manifests, tags and full decoding; no Azure calls")
    for command in (plan_parser, preview_parser, approval_parser, verification_parser):
        command.add_argument("--record", required=True, type=Path, help="*.run.json inside the output directory")
        endpoint_group = command.add_mutually_exclusive_group()
        endpoint_group.add_argument("--resource")
        endpoint_group.add_argument("--endpoint")
    args = parser.parse_args(argv)
    try:
        path = args.record.resolve()
        if args.command == "plan":
            if args.all and args.inputs:
                raise ValueError("--all cannot be combined with explicit inputs")
            plan = prepare_plan(args.project, args.inputs, args.chapters, args.out_dir,
                                args.voice, args.rate, args.max_chars, args.resource, args.endpoint)
            create_record(path, plan)
            print(f"Prepared {len(plan['tracks'])} tracks, "
                  f"{sum(len(t['chunks']) for t in plan['tracks'])} chunks: {path}")
            return 0
        record = load_record(path, args.resource, args.endpoint)
        if args.command == "preview":
            selection = {"chunks": args.chunks, "from_text": args.from_text, "through_text": args.through_text}
            spec = preview_spec(record["plan"], args.name, args.stem, selection)
            render_preview(path, record, args.name, spec, args.dry_run)
        elif args.command == "approve":
            approve(path, record, args.note)
        else:
            return 0 if verify(path, record)["status"] == "passed" else 1
    except (OSError, ValueError, RuntimeError, KeyError, MutagenError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main())
