"""Frozen N5 dialogue plans using the shared Entra/PCM engine; no Markdown-book language expansion."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

import requests
from mutagen.oggopus import OggOpus

import synthesize as engine


def content_hash(document: dict) -> str:
    package = {key: value for key, value in document.items() if key not in ("audio", "audioReview")}
    return hashlib.sha256(json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def renderer_hash() -> str:
    return engine.json_hash({"shared": engine.pipeline_hash(), "listening": engine.file_hash(Path(__file__))})


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def prepare(document: dict) -> list[dict]:
    if (type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1
            or type(document.get("jlpt")) is not int or document["jlpt"] != 5
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", document.get("id", ""))):
        raise ValueError("Only filename-matched N5 listening schema 1 is supported")
    speakers = document.get("speakers")
    turns = document.get("turns")
    if not isinstance(speakers, list) or not 1 <= len(speakers) <= 2 or not isinstance(turns, list) or not 1 <= len(turns) <= 16:
        raise ValueError("Expected one or two speakers and 1-16 turns")
    voices = {}
    for speaker in speakers:
        voice, rate, identifier = speaker["voice"], speaker["ratePercent"], speaker["id"]
        if (not re.fullmatch(r"ja-JP-[A-Za-z]+Neural", voice) or type(rate) is not int or not -15 <= rate <= 0
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier) or identifier in voices):
            raise ValueError("Invalid or duplicate Japanese speaker/voice/rate")
        voices[identifier] = speaker
    prepared = []
    ids = set()
    for turn in turns:
        identifier, speaker = turn["id"], voices[turn["speaker"]]
        text = turn.get("spokenText", turn["text"])
        if (not isinstance(text, str) or not text.strip() or len(text) > 500 or identifier in ids
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier)):
            raise ValueError("Invalid or duplicate spoken turn")
        ids.add(identifier)
        prepared.append({"id": identifier, "text": text, "voice": speaker["voice"], "rate": f'{speaker["ratePercent"]}%', "pauseMs": 350})
    return prepared


def create_plan(paths: list[Path], output: Path, endpoint: str, subscription: str, preview: list[str]) -> Path:
    if not 1 <= len(paths) <= 20:
        raise ValueError("Select 1-20 original N5 passages")
    if (output / "plan.json").exists():
        raise ValueError("Plan already exists; use a new run directory")
    passages = []
    for path in paths:
        document = read_json(path)
        turns = prepare(document)
        if path.stem != document["id"]:
            raise ValueError(f"Filename does not match ID: {path}")
        passages.append({"source": str(path.resolve()), "document": document, "contentSha256": content_hash(document), "turns": turns})
    ids = [p["document"]["id"] for p in passages]
    if len(ids) != len(set(ids)) or not preview or len(preview) != len(set(preview)) or not set(preview) <= set(ids):
        raise ValueError("Duplicate passage or invalid preview selection")
    profiles = {(turn["voice"], turn["rate"]) for passage in passages for turn in passage["turns"]}
    preview_profiles = {(turn["voice"], turn["rate"]) for passage in passages if passage["document"]["id"] in preview
                        for turn in passage["turns"]}
    if not profiles <= preview_profiles:
        raise ValueError("The representative preview must cover every selected voice/rate profile")
    plan = {"version": 1, "rendererSha256": renderer_hash(), "endpointId": engine.endpoint_id(endpoint),
            "subscription": subscription, "previewIds": preview, "passages": passages}
    path = output / "plan.json"
    engine.write_json(path, plan)
    return path


def load_plan(path: Path) -> dict:
    plan = read_json(path)
    if plan.get("version") != 1 or plan.get("rendererSha256") != renderer_hash():
        raise ValueError("Plan version or renderer changed; prepare a new plan and preview")
    for passage in plan["passages"]:
        if content_hash(passage["document"]) != passage["contentSha256"] or prepare(passage["document"]) != passage["turns"]:
            raise ValueError("Frozen passage or spoken turns changed")
        if content_hash(read_json(Path(passage["source"]))) != passage["contentSha256"]:
            raise ValueError(f'Learning package changed: {passage["source"]}')
    return plan


def verify_audio(path: Path, expected_ms: int) -> int:
    if not 0 < path.stat().st_size <= 512 * 1024:
        raise ValueError("Opus export exceeds the 512 KiB media budget")
    if OggOpus(path).info.channels != 1:
        raise ValueError("Listening export must be mono Opus")
    decoded = subprocess.run(
        [engine._ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-xerror", "-i", str(path),
         "-map", "0:a:0", "-ac", "1", "-ar", "24000", "-f", "s16le", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    duration = round(len(decoded) * 1000 / (engine.SAMPLE_RATE * engine.SAMPLE_WIDTH))
    if not 15_000 <= duration <= 60_000 or abs(duration - expected_ms) > 100:
        raise ValueError(f"Unexpected decoded duration: {duration} ms (expected {expected_ms}); revise the script, do not pad it")
    return duration


def request(path: Path, passage: dict) -> dict:
    return {"planSha256": engine.file_hash(path), "contentSha256": passage["contentSha256"],
            "id": passage["document"]["id"], "rendererSha256": renderer_hash()}


def verified_record(path: Path, passage: dict) -> dict:
    root, identifier = path.parent, passage["document"]["id"]
    record = read_json(root / f"{identifier}.render.json")
    audio = root / f"{identifier}.ogg"
    if record["request"] != request(path, passage) or engine.file_hash(audio) != record["audio"]["sha256"]:
        raise ValueError(f"Audio or render identity changed: {identifier}")
    verify_audio(audio, record["audio"]["durationMs"])
    return record


def render_one(path: Path, passage: dict, token, session: requests.Session) -> dict:
    root, identifier = path.parent, passage["document"]["id"]
    audio, receipt = root / f"{identifier}.ogg", root / f"{identifier}.render.json"
    if audio.exists() or receipt.exists():
        return verified_record(path, passage)
    pcm = bytearray(b"\x00\x00" * (engine.SAMPLE_RATE * 150 // 1000))
    timings = []
    for number, turn in enumerate(passage["turns"]):
        if number:
            pcm.extend(b"\x00\x00" * (engine.SAMPLE_RATE * turn["pauseMs"] // 1000))
        start = round(len(pcm) * 1000 / (engine.SAMPLE_RATE * engine.SAMPLE_WIDTH))
        pcm.extend(engine.cached_pcm(turn["text"], token, session, turn["voice"], turn["rate"], root / ".pcm", False))
        end = round(len(pcm) * 1000 / (engine.SAMPLE_RATE * engine.SAMPLE_WIDTH))
        timings.append({"id": turn["id"], "startMs": start, "endMs": end})
    pcm.extend(b"\x00\x00" * (engine.SAMPLE_RATE * 200 // 1000))
    expected = round(len(pcm) * 1000 / (engine.SAMPLE_RATE * engine.SAMPLE_WIDTH))
    if not 15_000 <= expected <= 60_000:
        raise ValueError(f"{identifier}: {expected} ms is outside 15-60 seconds; revise the script/rate, not the padding")
    with tempfile.TemporaryDirectory(prefix="opus-", dir=root) as temporary:
        pending = Path(temporary) / "pending.ogg"
        subprocess.run(
            [engine._ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "wav", "-i", "pipe:0",
             "-map_metadata", "-1", "-codec:a", "libopus", "-b:a", "32k", "-vbr", "on", "-application", "voip",
             "-ac", "1", "-ar", "24000", str(pending)],
            input=engine._wrap_wav(bytes(pcm)), capture_output=True, check=True,
        )
        duration = verify_audio(pending, expected)
        load_plan(path)
        record = {"request": request(path, passage), "audio": {
            "path": f"audio/{identifier}.ogg", "sha256": engine.file_hash(pending), "contentSha256": passage["contentSha256"],
            "durationMs": duration, "turns": timings,
        }}
        pending.replace(audio)
        engine.write_json(receipt, record)
    return record


def preview_binding(path: Path, plan: dict) -> dict:
    return {p["document"]["id"]: verified_record(path, p)["audio"]["sha256"]
            for p in plan["passages"] if p["document"]["id"] in plan["previewIds"]}


def render(path: Path, resource: str, subscription: str, preview: bool) -> None:
    plan = load_plan(path)
    endpoint = engine.resolve_endpoint(resource=resource)
    if engine.endpoint_id(endpoint) != plan["endpointId"] or subscription != plan["subscription"]:
        raise ValueError("Resource or subscription differs from the frozen plan")
    if not preview:
        approval = read_json(path.parent / "preview-approval.json")
        if approval.get("planSha256") != engine.file_hash(path) or approval.get("audio") != preview_binding(path, plan):
            raise ValueError("The exact representative preview has not been approved")
    selected = [p for p in plan["passages"] if not preview or p["document"]["id"] in plan["previewIds"]]
    for passage in selected:
        identifier = passage["document"]["id"]
        if (path.parent / f"{identifier}.ogg").exists() or (path.parent / f"{identifier}.render.json").exists():
            verified_record(path, passage)
    engine.LANGUAGE, engine.XML_LANG, engine.ENDPOINT = "ja", "ja-JP", endpoint
    token = engine.make_token_provider(subscription=subscription)
    with requests.Session() as session:
        for passage in selected:
            record = render_one(path, passage, token, session)
            print(f'{passage["document"]["id"]}: {record["audio"]["durationMs"] / 1000:.2f}s; pending final listening approval')


def approve(path: Path, identifier: str | None, note: str, reviewed_on: str) -> None:
    plan = load_plan(path)
    if not note.strip() or date.fromisoformat(reviewed_on).isoformat() != reviewed_on:
        raise ValueError("A valid review date and nonempty actual-listening note are required")
    if identifier is None:
        engine.write_json(path.parent / "preview-approval.json", {
            "planSha256": engine.file_hash(path), "audio": preview_binding(path, plan), "reviewedOn": reviewed_on, "note": note,
        })
    else:
        selected = [p for p in plan["passages"] if p["document"]["id"] == identifier]
        if len(selected) != 1:
            raise ValueError(f"Unknown passage: {identifier}")
        audio = verified_record(path, selected[0])["audio"]
        engine.write_json(path.parent / f"{identifier}.review.json", {
            "status": "approved", "audioSha256": audio["sha256"], "contentSha256": audio["contentSha256"],
            "reviewedOn": reviewed_on, "note": note,
        })


def export(path: Path, content_root: Path) -> None:
    plan = load_plan(path)
    content_root = content_root.resolve()
    prepared = []
    for passage in plan["passages"]:
        identifier = passage["document"]["id"]
        target = content_root / "listening" / f"{identifier}.json"
        if target.resolve() != Path(passage["source"]).resolve():
            raise ValueError("Export target is not the planned content source")
        record = verified_record(path, passage)
        review = read_json(path.parent / f"{identifier}.review.json")
        if (review.get("status") != "approved" or review.get("audioSha256") != record["audio"]["sha256"]
                or review.get("contentSha256") != passage["contentSha256"]):
            raise ValueError(f"Final-recording approval is absent or stale: {identifier}")
        destination = content_root / "audio" / f"{identifier}.ogg"
        if destination.exists() and engine.file_hash(destination) != record["audio"]["sha256"]:
            raise ValueError(f"Refusing to replace an existing different recording: {destination}")
        prepared.append((passage, record, review, target, destination))
    for passage, record, review, target, destination in prepared:
        engine.atomic_write(destination, (path.parent / destination.name).read_bytes())
        engine.write_json(target, {**passage["document"], "audio": record["audio"], "audioReview": review})


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--resource", required=True)
    plan.add_argument("--subscription", required=True)
    plan.add_argument("--preview-ids", nargs="+", required=True)
    plan.add_argument("--inputs", nargs="+", type=Path, required=True)
    rendering = sub.add_parser("render")
    rendering.add_argument("--resource", required=True)
    rendering.add_argument("--subscription", required=True)
    rendering.add_argument("--preview", action="store_true")
    rendering.add_argument("--allow-paid", action="store_true", required=True)
    approval = sub.add_parser("approve")
    approval.add_argument("--id", help="Omit only for the representative preview approval")
    approval.add_argument("--listened", action="store_true", required=True)
    approval.add_argument("--note", required=True)
    approval.add_argument("--reviewed-on", required=True)
    exporting = sub.add_parser("export")
    exporting.add_argument("--content-root", type=Path, required=True)
    checking = sub.add_parser("verify")
    for command in (rendering, approval, exporting, checking):
        command.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            endpoint = engine.resolve_endpoint(resource=args.resource)
            output = create_plan(args.inputs, args.output.resolve(), endpoint, args.subscription, args.preview_ids)
            saved = read_json(output)
            print(f'{output}: {len(saved["passages"])} passages; no synthesis or listening approval')
        elif args.command == "render":
            render(args.plan.resolve(), args.resource, args.subscription, args.preview)
        elif args.command == "approve":
            approve(args.plan.resolve(), args.id, args.note, args.reviewed_on)
        elif args.command == "export":
            export(args.plan.resolve(), args.content_root)
        else:
            saved = load_plan(args.plan)
            for passage in saved["passages"]:
                verified_record(args.plan, passage)
            print("All planned exports decoded successfully. This is not listening approval.")
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, engine.PermanentSynthesisError, engine.TransientExhaustionError) as exc:
        parser.exit(1, f"Listening operation failed: {exc}\n")


if __name__ == "__main__":
    main()
