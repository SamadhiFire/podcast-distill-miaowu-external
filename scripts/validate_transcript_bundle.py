#!/usr/bin/env python3
"""Validate transcript artifacts before report generation and publishing."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import zipfile


ALLOWED_TRANSCRIPT_SOURCES = {"official_caption", "auto_caption", "asr"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items-json", required=True)
    parser.add_argument("--manifest-json", required=True)
    parser.add_argument("--subtitles-dir", default="subtitles")
    parser.add_argument("--bundle-zip")
    parser.add_argument("--min-coverage", type=float, default=0.95)
    parser.add_argument("--min-duration-seconds", type=float, default=300)
    parser.add_argument("--expected-url", action="append", default=[])
    parser.add_argument("--output-json")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"{path}: cannot read JSON: {exc}") from exc


def normalize_url(url: str) -> str:
    parsed = urlparse(url or "")
    host = parsed.netloc.lower()
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]
        if video_id:
            return "https://www.youtube.com/watch?" + urlencode({"v": video_id})
    if host.endswith("youtube.com"):
        qs = parse_qs(parsed.query)
        if parsed.path == "/watch" and qs.get("v"):
            return "https://www.youtube.com/watch?" + urlencode({"v": qs["v"][0]})
        shorts = re.fullmatch(r"/shorts/([^/]+)", parsed.path)
        if shorts:
            return "https://www.youtube.com/watch?" + urlencode({"v": shorts.group(1)})
        if parsed.path == "/playlist" and qs.get("list"):
            return "https://www.youtube.com/playlist?" + urlencode({"list": qs["list"][0]})
    return (url or "").split("?s=")[0].rstrip("/")


def parse_number(value: Any, label: str, failures: list[str], context: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        failures.append(f"{context}: missing or invalid {label}")
        return None
    return number


def duration_of(item: dict[str, Any]) -> float | None:
    value = item.get("duration", item.get("duration_seconds"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_youtube_collection_url(url: str) -> bool:
    parsed = urlparse(url or "")
    if "youtube.com" not in parsed.netloc.lower():
        return False
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.path == "/playlist":
        return True
    if parts and (parts[0] == "channel" or parts[0] == "c" or parts[0].startswith("@")):
        return True
    if len(parts) >= 2 and parts[0] in {"user", "playlist"}:
        return True
    return False


def resolve_declared_file(subtitles_dir: Path, declared: str) -> Path:
    relative = Path(declared)
    if relative.is_absolute():
        raise RuntimeError(f"absolute file path is not allowed in transcript metadata: {declared}")
    if relative.parts and relative.parts[0] == subtitles_dir.name:
        candidate = subtitles_dir.parent / relative
    else:
        candidate = subtitles_dir / relative
    resolved = candidate.resolve()
    if not resolved.is_relative_to(subtitles_dir.resolve()):
        raise RuntimeError(f"declared file escapes subtitles directory: {declared}")
    return resolved


def transcript_method(meta: dict[str, Any]) -> str:
    raw = str(meta.get("source_method") or meta.get("source") or "").strip().lower()
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1]
    return raw


def subtitle_sidecar_exists(meta_path: Path, meta: dict[str, Any], subtitles_dir: Path) -> bool:
    names: list[str] = []
    for key in ("subtitle_srt", "subtitle_vtt", "srt", "vtt", "subtitle"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    for name in names:
        try:
            if resolve_declared_file(subtitles_dir, name).exists():
                return True
        except RuntimeError:
            return False
    return meta_path.with_suffix(".srt").exists() or meta_path.with_suffix(".vtt").exists()


def validate_manifest(manifest: Any, failures: list[str]) -> None:
    if not isinstance(manifest, dict):
        failures.append("manifest.json must be a JSON object")
        return
    status = str(manifest.get("status", "")).lower()
    if status and status not in {"success", "complete"}:
        failures.append(f"manifest status is not success: {status}")
    try:
        failure_count = int(manifest.get("failure_count") or 0)
    except (TypeError, ValueError):
        failures.append("manifest failure_count is invalid")
        failure_count = 0
    if failure_count:
        failures.append(f"manifest failure_count is {failure_count}")
    errors = manifest.get("errors") or manifest.get("failures") or []
    if errors:
        failures.append(f"manifest contains errors/failures: {json.dumps(errors, ensure_ascii=False)[:1200]}")


def validate_bundle_zip(path: Path, failures: list[str]) -> None:
    if not path.exists():
        failures.append(f"{path}: subtitles bundle zip not found")
        return
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile as exc:
        failures.append(f"{path}: invalid zip file: {exc}")
        return
    if not any(name.startswith("subtitles/") for name in names):
        failures.append(f"{path}: zip does not contain subtitles/ root")


def validate_transcript_meta(
    subtitles_dir: Path,
    min_coverage: float,
    min_duration_seconds: float,
    failures: list[str],
    warnings: list[str],
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    meta_paths = sorted(subtitles_dir.glob("*.json"))
    if not meta_paths:
        failures.append(f"{subtitles_dir}: no per-item transcript metadata found")
        return index

    for meta_path in meta_paths:
        context = str(meta_path)
        meta = load_json(meta_path)
        if not isinstance(meta, dict):
            failures.append(f"{context}: metadata must be a JSON object")
            continue
        url = str(meta.get("url") or "").strip()
        text_name = str(meta.get("text") or "").strip()
        if not url:
            failures.append(f"{context}: missing url")
        if not text_name:
            failures.append(f"{context}: missing text")
            continue

        try:
            text_path = resolve_declared_file(subtitles_dir, text_name)
        except RuntimeError as exc:
            failures.append(f"{context}: {exc}")
            continue
        if not text_path.exists():
            failures.append(f"{context}: text file does not exist: {text_name}")
            continue
        data = text_path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{context}: text file is not valid UTF-8: {exc}")
            continue

        expected_sha = str(meta.get("sha256") or "").strip().lower()
        actual_sha = hashlib.sha256(data).hexdigest()
        if not expected_sha:
            failures.append(f"{context}: missing sha256")
        elif actual_sha != expected_sha:
            failures.append(f"{context}: sha256 mismatch")

        text_chars = parse_number(meta.get("text_chars"), "text_chars", failures, context)
        if text_chars is not None:
            if text_chars <= 0:
                failures.append(f"{context}: text_chars <= 0")
            if int(text_chars) != len(text):
                failures.append(f"{context}: text_chars={int(text_chars)} but decoded text length={len(text)}")

        duration = parse_number(meta.get("duration_seconds"), "duration_seconds", failures, context)
        parse_number(meta.get("last_timestamp_seconds"), "last_timestamp_seconds", failures, context)
        coverage = parse_number(meta.get("coverage_ratio"), "coverage_ratio", failures, context)
        if duration is not None and coverage is not None and duration >= min_duration_seconds:
            if coverage < min_coverage:
                failures.append(f"{context}: coverage_ratio {coverage:.4f} < {min_coverage:.4f}")

        method = transcript_method(meta)
        if method not in ALLOWED_TRANSCRIPT_SOURCES:
            failures.append(
                f"{context}: source/source_method must be one of "
                f"{sorted(ALLOWED_TRANSCRIPT_SOURCES)}, got {method or '<missing>'}"
            )

        if not subtitle_sidecar_exists(meta_path, meta, subtitles_dir):
            failures.append(f"{context}: missing .srt or .vtt sidecar")

        normalized = normalize_url(url)
        if normalized in index:
            warnings.append(f"duplicate transcript metadata for {normalized}")
        index[normalized] = {"meta": meta, "path": str(meta_path), "coverage": coverage, "duration": duration}
    return index


def validate_items(
    items: Any,
    transcript_index: dict[str, dict[str, Any]],
    min_coverage: float,
    min_duration_seconds: float,
    expected_urls: list[str],
    failures: list[str],
) -> tuple[int, int]:
    if not isinstance(items, list):
        failures.append("daily_items.json must be a JSON array")
        return 0, 0
    required_count = 0
    daily_urls = {normalize_url(str(item.get("url") or "")) for item in items if isinstance(item, dict)}

    for expected in expected_urls:
        normalized_expected = normalize_url(expected)
        if normalized_expected not in daily_urls:
            failures.append(f"expected URL missing from daily_items.json: {expected}")

    for idx, item in enumerate(items, 1):
        context = f"daily_items[{idx}]"
        if not isinstance(item, dict):
            failures.append(f"{context}: item must be a JSON object")
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            failures.append(f"{context}: missing url")
            continue
        if str(item.get("platform") or "").lower() == "youtube" and is_youtube_collection_url(url):
            failures.append(f"{context}: YouTube item is still a collection URL, not a video URL: {url}")

        duration = duration_of(item)
        if duration is None:
            failures.append(f"{context}: missing or invalid duration")
            continue
        if duration < min_duration_seconds:
            continue
        required_count += 1
        normalized = normalize_url(url)
        transcript = transcript_index.get(normalized)
        if not transcript:
            failures.append(f"{context}: missing transcript metadata for required item: {url}")
            continue
        coverage = transcript.get("coverage")
        if coverage is None or float(coverage) < min_coverage:
            failures.append(f"{context}: transcript coverage below threshold for {url}")
    return len(items), required_count


def main() -> int:
    args = parse_args()
    failures: list[str] = []
    warnings: list[str] = []
    items_path = Path(args.items_json)
    manifest_path = Path(args.manifest_json)
    subtitles_dir = Path(args.subtitles_dir)

    items = load_json(items_path)
    manifest = load_json(manifest_path)
    validate_manifest(manifest, failures)
    if args.bundle_zip:
        validate_bundle_zip(Path(args.bundle_zip), failures)
    if not subtitles_dir.exists():
        failures.append(f"{subtitles_dir}: directory not found")
        transcript_index: dict[str, dict[str, Any]] = {}
    else:
        transcript_index = validate_transcript_meta(
            subtitles_dir,
            args.min_coverage,
            args.min_duration_seconds,
            failures,
            warnings,
        )
    item_count, required_count = validate_items(
        items,
        transcript_index,
        args.min_coverage,
        args.min_duration_seconds,
        args.expected_url,
        failures,
    )

    report = {
        "status": "failed" if failures else "success",
        "item_count": item_count,
        "required_transcript_item_count": required_count,
        "transcript_metadata_count": len(transcript_index),
        "min_coverage": args.min_coverage,
        "min_duration_seconds": args.min_duration_seconds,
        "failures": failures,
        "warnings": warnings,
    }
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if warnings:
        print("Validation warnings:")
        for warning in warnings:
            print(f"- {warning}")
    if failures:
        print("Transcript artifact validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(
        "Transcript artifacts validated: "
        f"{item_count} item(s), {required_count} required transcript(s), "
        f"{len(transcript_index)} transcript metadata file(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
