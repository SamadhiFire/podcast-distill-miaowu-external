#!/usr/bin/env python3
"""Bridge the external YouTube transcript service into digest artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, time as datetime_time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import zipfile

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


DEFAULT_API_BASE = "https://gnevobefaowwiwwtfowj.supabase.co/functions/v1"
TZ_SHANGHAI = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = BASE_DIR / "reports"


class ExternalServiceError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect YouTube transcripts from the external Bolt/Supabase service "
            "and convert them to this repo's daily digest artifact format."
        )
    )
    parser.add_argument("--date", help="Report date in YYYY-MM-DD. Defaults to today in timezone.")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--window-start", help="Explicit ISO 8601 window start.")
    parser.add_argument("--window-end", help="Explicit ISO 8601 window end.")
    parser.add_argument("--items-json")
    parser.add_argument("--manifest-json")
    parser.add_argument("--status-json")
    parser.add_argument("--subtitles-dir", default="subtitles")
    parser.add_argument("--bundle-path", default="subtitles_bundle.zip")
    parser.add_argument(
        "--api-base",
        default=(
            os.getenv("MEDIA_API_BASE")
            or os.getenv("YOUTUBE_TRANSCRIPT_API_BASE")
            or DEFAULT_API_BASE
        ),
        help="Supabase functions base URL, ending in /functions/v1.",
    )
    parser.add_argument("--media-token", default=os.getenv("MEDIA_API_TOKEN"))
    parser.add_argument(
        "--sources-profile",
        default=os.getenv("YOUTUBE_SOURCES_PROFILE") or "youtube-default",
    )
    parser.add_argument("--poll-interval", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=int, default=3 * 60 * 60)
    parser.add_argument("--no-require-transcripts", action="store_true")
    parser.add_argument("--no-allow-asr", action="store_true")
    parser.add_argument(
        "--single-url",
        help="Local test mode: extract one YouTube URL via /media-extract instead of daily collection.",
    )
    parser.add_argument("--language", default="en", help="Preferred language for --single-url.")
    parser.add_argument(
        "--single-category",
        default="科技 / AI / VC",
        help="Category used for --single-url test artifacts.",
    )
    return parser.parse_args()


def load_timezone(name: str) -> timezone:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)  # type: ignore[return-value]
        except Exception:
            pass
    if name == "Asia/Shanghai":
        return TZ_SHANGHAI
    raise RuntimeError(f"Unsupported timezone without zoneinfo data: {name}")


def resolve_report_date(value: str | None, tz: timezone) -> str:
    if not value:
        return datetime.now(tz).strftime("%Y-%m-%d")
    return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_SHANGHAI)
    return parsed


def resolve_window(
    report_date: str,
    tz: timezone,
    explicit_start: str | None,
    explicit_end: str | None,
) -> tuple[datetime, datetime]:
    if bool(explicit_start) != bool(explicit_end):
        raise RuntimeError("--window-start and --window-end must be provided together")
    if explicit_start and explicit_end:
        return parse_iso_datetime(explicit_start), parse_iso_datetime(explicit_end)
    end_date = datetime.strptime(report_date, "%Y-%m-%d").date()
    end = datetime.combine(end_date, datetime_time(6, 0), tzinfo=tz)
    return end - timedelta(days=1), end


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


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
    return (url or "").split("?s=")[0].rstrip("/")


def youtube_id_from_url(url: str) -> str | None:
    parsed = urlparse(url or "")
    if parsed.netloc.lower() in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/")[0] or None
    qs = parse_qs(parsed.query)
    if qs.get("v"):
        return qs["v"][0]
    match = re.search(r"(?:/embed/|/shorts/|/v/)([A-Za-z0-9_-]{11})", url or "")
    return match.group(1) if match else None


def sanitize_filename(value: str, max_len: int = 120) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value or "")
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or "untitled")[:max_len]


def parse_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_transcript_text(value: str) -> str:
    text = value or ""
    text = re.sub(
        r"\s*---\s*Generated by https://youtube-transcript\.ai.*$",
        "",
        text,
        flags=re.I | re.S,
    )
    text = re.sub(
        r"\s*Generated by https://youtube-transcript\.ai.*$",
        "",
        text,
        flags=re.I | re.S,
    )
    text = re.sub(
        r"\s*Interactive version .*?https://youtube-transcript\.ai/\?v=\S+",
        "",
        text,
        flags=re.I | re.S,
    )
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def parse_caption_text(content: str) -> str:
    lines: list[str] = []
    previous = None
    in_style = False
    for raw in (content or "").splitlines():
        line = raw.strip()
        upper = line.upper()
        if not line:
            in_style = False
            continue
        if upper in {"WEBVTT", "STYLE", "REGION"}:
            in_style = upper in {"STYLE", "REGION"}
            continue
        if in_style or "-->" in line or re.fullmatch(r"\d+", line):
            continue
        if line.startswith(("NOTE", "Kind:", "Language:", "X-TIMESTAMP-MAP")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{[^}]*\}", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and line != previous:
            lines.append(line)
            previous = line
    return clean_transcript_text("\n".join(lines))


def caption_has_timestamps(value: str) -> bool:
    return bool(re.search(r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->", value or ""))


def timestamp_to_seconds(value: str) -> float:
    parts = re.split(r"[:,.]", value)
    if len(parts) != 4:
        return 0.0
    hours, minutes, seconds, millis = [int(part) for part in parts]
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def last_timestamp_seconds(*captions: str) -> float:
    last = 0.0
    pattern = re.compile(r"-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})")
    for caption in captions:
        for match in pattern.finditer(caption or ""):
            last = max(last, timestamp_to_seconds(match.group(1)))
    return last


def format_vtt_time(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(millis, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def single_cue_vtt(text: str, duration_seconds: float) -> str:
    end = max(1.0, duration_seconds)
    return f"WEBVTT\n\n00:00:00.000 --> {format_vtt_time(end)}\n{text.strip()}\n"


def srt_to_vtt(srt: str) -> str:
    content = (srt or "").strip()
    content = re.sub(
        r"(\d{1,2}:\d{2}:\d{2}),(\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}),(\d{3})",
        r"\1.\2 --> \3.\4",
        content,
    )
    return "WEBVTT\n\n" + content + "\n"


def transcript_method(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in {"official_caption", "manual", "human", "caption"}:
        return "official_caption"
    if "manual" in text or "official" in text:
        return "official_caption"
    if text in {"asr", "speech_to_text", "whisper"} or "asr" in text:
        return "asr"
    return "auto_caption"


class ExternalTranscriptClient:
    def __init__(self, api_base: str, token: str) -> None:
        if not token:
            raise RuntimeError("MEDIA_API_TOKEN is required")
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def endpoint(self, function_name: str, path: str) -> str:
        return f"{self.api_base}/{function_name}/{path.lstrip('/')}"

    def request_json(
        self,
        method: str,
        function_name: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 120,
    ) -> dict[str, Any]:
        url = self.endpoint(function_name, path)
        try:
            response = self.session.request(method, url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise ExternalServiceError(f"{method} {url}: request failed: {exc}") from exc
        if not response.ok:
            raise ExternalServiceError(
                f"{method} {url}: HTTP {response.status_code}: {response.text[:800]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ExternalServiceError(f"{method} {url}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ExternalServiceError(f"{method} {url}: JSON response is not an object")
        return data

    def get_file_json(self, function_name: str, path: str, default: Any) -> Any:
        url = self.endpoint(function_name, path)
        response = self.session.get(url, timeout=120)
        if response.status_code == 404:
            return default
        if not response.ok:
            raise ExternalServiceError(f"GET {url}: HTTP {response.status_code}: {response.text[:800]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ExternalServiceError(f"GET {url}: file response is not JSON") from exc

    def create_daily_job(
        self,
        report_date: str,
        window_start: datetime,
        window_end: datetime,
        sources_profile: str,
        require_transcripts: bool,
        allow_asr: bool,
    ) -> str:
        payload = {
            "date": report_date,
            "window_start": iso_z(window_start),
            "window_end": iso_z(window_end),
            "sources_profile": sources_profile,
            "require_transcripts": require_transcripts,
            "allow_asr": allow_asr,
        }
        data = self.request_json("POST", "daily-collector", "daily-collect", payload)
        job_id = data.get("job_id") or data.get("id")
        if not job_id:
            raise ExternalServiceError(f"daily-collect response has no job_id: {data}")
        return str(job_id)

    def create_media_job(self, url: str, language: str) -> str:
        data = self.request_json(
            "POST",
            "youtube-processor",
            "media-extract",
            {"url": url, "language": language},
        )
        job_id = data.get("job_id") or data.get("id")
        if not job_id:
            raise ExternalServiceError(f"media-extract response has no job_id: {data}")
        return str(job_id)

    def poll_job(
        self,
        function_name: str,
        path_prefix: str,
        job_id: str,
        poll_interval: int,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            data = self.request_json("GET", function_name, f"{path_prefix}/{job_id}", timeout=60)
            status = str(data.get("status") or "").lower()
            print(f"External YouTube job {job_id}: {status or 'unknown'}", flush=True)
            if status == "success":
                return data
            if status == "failed":
                message = data.get("error_message") or data.get("message") or data
                raise ExternalServiceError(f"External YouTube job failed: {message}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"External YouTube job timed out after {timeout_seconds}s: {job_id}")
            time.sleep(max(1, poll_interval))


def iter_bundle_records(bundle: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(bundle, list):
        for item in bundle:
            if isinstance(item, dict):
                records.append(item)
        return records
    if not isinstance(bundle, dict):
        return records

    for key in ("items", "videos", "transcripts", "subtitles", "results", "data"):
        value = bundle.get(key)
        if isinstance(value, list):
            records.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if isinstance(nested_value, dict):
                    record = dict(nested_value)
                    record.setdefault("_bundle_key", nested_key)
                    records.append(record)

    if any(key in bundle for key in ("transcript_text", "transcript_vtt", "transcript_srt", "url", "video_id")):
        records.append(bundle)

    for key, value in bundle.items():
        if isinstance(value, dict):
            record = dict(value)
            record.setdefault("_bundle_key", key)
            records.append(record)
    return records


def record_keys(record: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("url", "video_url", "webpage_url"):
        value = str(record.get(field) or "").strip()
        if value:
            keys.add(normalize_url(value))
    for field in ("video_id", "id", "_video_id", "_bundle_key"):
        value = str(record.get(field) or "").strip()
        if value:
            keys.add(value)
            keys.add(f"https://www.youtube.com/watch?v={value}")
    url = str(record.get("url") or record.get("video_url") or "").strip()
    video_id = youtube_id_from_url(url)
    if video_id:
        keys.add(video_id)
    return keys


def index_bundle_records(bundle: Any) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for record in iter_bundle_records(bundle):
        for key in record_keys(record):
            index.setdefault(key, record)
    return index


def lookup_transcript_record(
    item: dict[str, Any],
    bundle_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    candidates: list[str] = []
    url = str(item.get("url") or item.get("video_url") or "").strip()
    if url:
        candidates.append(normalize_url(url))
    video_id = str(item.get("video_id") or item.get("_video_id") or youtube_id_from_url(url) or "").strip()
    if video_id:
        candidates.extend([video_id, f"https://www.youtube.com/watch?v={video_id}"])
    for key in candidates:
        if key in bundle_index:
            return bundle_index[key]
    return {}


def first_text(records: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for record in records:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def normalize_youtube_item(item: dict[str, Any], record: dict[str, Any], report_date: str) -> dict[str, Any]:
    url = str(
        item.get("url")
        or item.get("video_url")
        or record.get("url")
        or record.get("video_url")
        or ""
    ).strip()
    video_id = (
        str(item.get("video_id") or item.get("_video_id") or record.get("video_id") or "").strip()
        or youtube_id_from_url(url)
    )
    if not url and video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
    title = str(item.get("title") or record.get("title") or f"YouTube {video_id or report_date}").strip()
    duration = int(
        parse_number(
            item.get("duration")
            or item.get("duration_seconds")
            or record.get("duration_seconds")
            or record.get("duration"),
            0,
        )
    )
    return {
        **item,
        "platform": "youtube",
        "category": item.get("category") or record.get("category") or "",
        "source_name": item.get("source_name") or record.get("source_name") or record.get("channel") or "YouTube",
        "source_url": item.get("source_url") or record.get("source_url") or "",
        "title": title,
        "original_title": item.get("original_title") or title,
        "url": url,
        "published_at": item.get("published_at") or record.get("published_at") or "",
        "duration": duration,
        "duration_seconds": duration,
        "description": item.get("description") or record.get("description") or "",
        "video_id": video_id or "",
        "transcript_status": "success",
    }


def write_youtube_transcript(
    item: dict[str, Any],
    record: dict[str, Any],
    subtitles_dir: Path,
) -> dict[str, Any] | None:
    sources = [item, record]
    text = clean_transcript_text(
        first_text(
            sources,
            ("transcript_text", "plain_text", "text_content", "txt", "text"),
        )
    )
    vtt = first_text(sources, ("transcript_vtt", "vtt", "subtitle_vtt_content", "vtt_content"))
    srt = first_text(sources, ("transcript_srt", "srt", "subtitle_srt_content", "srt_content"))
    if not text:
        text = parse_caption_text(vtt or srt)
    if not text:
        return None

    video_id = str(item.get("video_id") or youtube_id_from_url(str(item.get("url") or "")) or "").strip()
    if not video_id:
        video_id = hashlib.sha1(str(item.get("url") or item.get("title") or "").encode("utf-8")).hexdigest()[:12]
    stem = sanitize_filename(f"youtube_{video_id}", 100)
    txt_name = f"{stem}.txt"
    vtt_name = f"{stem}.vtt"
    srt_name = f"{stem}.srt"
    meta_name = f"{stem}.json"
    subtitles_dir.mkdir(parents=True, exist_ok=True)

    text_content = text.strip() + "\n"
    text_bytes = text_content.encode("utf-8")
    (subtitles_dir / txt_name).write_text(text_content, encoding="utf-8", newline="\n")

    duration = parse_number(item.get("duration_seconds") or item.get("duration"), 0.0)
    if not caption_has_timestamps(vtt) and caption_has_timestamps(srt):
        vtt = srt_to_vtt(srt)
    if not caption_has_timestamps(vtt):
        vtt = single_cue_vtt(text, duration)
    (subtitles_dir / vtt_name).write_text(vtt.strip() + "\n", encoding="utf-8", newline="\n")
    if caption_has_timestamps(srt):
        (subtitles_dir / srt_name).write_text(srt.strip() + "\n", encoding="utf-8", newline="\n")

    last_ts = parse_number(
        record.get("last_timestamp_seconds") or item.get("last_timestamp_seconds"),
        0.0,
    )
    if not last_ts:
        last_ts = last_timestamp_seconds(vtt, srt)
    if not duration:
        duration = parse_number(record.get("duration_seconds") or record.get("duration"), 0.0) or last_ts
    coverage = parse_number(record.get("coverage_ratio") or item.get("coverage_ratio"), 0.0)
    if not coverage and duration:
        coverage = min(last_ts / duration, 1.0)
    if not last_ts and duration:
        last_ts = duration
    source_method = transcript_method(
        record.get("transcript_source")
        or record.get("source_method")
        or record.get("source")
        or item.get("transcript_source")
    )
    meta = {
        "platform": "youtube",
        "video_id": video_id,
        "url": item.get("url", ""),
        "title": item.get("title", ""),
        "source_name": item.get("source_name", ""),
        "source_url": item.get("source_url", ""),
        "text": txt_name,
        "subtitle_vtt": vtt_name,
        "subtitle_srt": srt_name if (subtitles_dir / srt_name).exists() else None,
        "text_chars": len(text_content),
        "sha256": hashlib.sha256(text_bytes).hexdigest(),
        "duration_seconds": duration,
        "last_timestamp_seconds": last_ts,
        "coverage_ratio": coverage,
        "source": source_method,
        "source_method": source_method,
        "language": item.get("language") or record.get("language") or "",
    }
    write_json(subtitles_dir / meta_name, meta)
    return meta


def cleanup_youtube_outputs(subtitles_dir: Path) -> None:
    if not subtitles_dir.exists():
        return
    for path in subtitles_dir.glob("youtube_*"):
        if path.is_file() and path.suffix.lower() in {".json", ".txt", ".srt", ".vtt"}:
            path.unlink()


def make_bundle(subtitles_dir: Path, bundle_path: Path) -> None:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if subtitles_dir.exists():
            for path in sorted(subtitles_dir.glob("*")):
                if path.is_file():
                    archive.write(path, Path("subtitles") / path.name)


def deduplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in items:
        key = normalize_url(str(item.get("url") or "")) or f"{item.get('platform')}:{item.get('title')}"
        output[key] = item
    return list(output.values())


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: str(item.get("published_at") or ""), reverse=True)


def platform_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        platform = str(item.get("platform") or "unknown")
        counts[platform] = counts.get(platform, 0) + 1
    return counts


def merge_manifest(
    existing_manifest: dict[str, Any],
    report_date: str,
    window_start: datetime,
    window_end: datetime,
    items: list[dict[str, Any]],
    youtube_status: dict[str, Any],
    youtube_manifest: dict[str, Any],
    transcript_results: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    existing_errors = existing_manifest.get("errors") or existing_manifest.get("failures") or []
    if not isinstance(existing_errors, list):
        existing_errors = [existing_errors]
    all_errors = [*existing_errors, *errors]
    existing_sources = existing_manifest.get("sources_summary") or []
    return {
        "status": "failed" if all_errors else "success",
        "date": report_date,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "sources_profile": "xiaoyuzhou-plus-youtube-external",
        "source_count": existing_manifest.get("source_count", 0),
        "item_count": len(items),
        "success_count": sum(1 for item in items if item.get("transcript_status") == "success"),
        "failure_count": len(all_errors),
        "counts_by_platform": platform_counts(items),
        "sources_summary": existing_sources,
        "transcript_results": [
            *(existing_manifest.get("transcript_results") or []),
            *transcript_results,
        ],
        "youtube_external": {
            "job_id": youtube_status.get("id") or youtube_status.get("job_id"),
            "status": youtube_status.get("status"),
            "request_data": youtube_status.get("request_data"),
            "result_data": youtube_status.get("result_data"),
            "manifest": youtube_manifest,
        },
        "errors": all_errors,
    }


def build_single_payload(status: dict[str, Any], url: str, category: str, language: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = status.get("result_data") or {}
    request = status.get("request_data") or {}
    video_id = result.get("video_id") or request.get("video_id") or youtube_id_from_url(url) or ""
    item = {
        "platform": "youtube",
        "category": category,
        "source_name": "YouTube",
        "source_url": "",
        "title": result.get("title") or f"YouTube {video_id}",
        "original_title": result.get("title") or f"YouTube {video_id}",
        "url": url,
        "published_at": "",
        "duration": result.get("duration_seconds") or 0,
        "duration_seconds": result.get("duration_seconds") or 0,
        "description": "",
        "video_id": video_id,
        "language": language,
    }
    return [item], {video_id: {**result, "url": url, "language": language}}


def collect_external_payload(
    client: ExternalTranscriptClient,
    args: argparse.Namespace,
    report_date: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], Any]:
    if args.single_url:
        job_id = client.create_media_job(args.single_url, args.language)
        print(f"Created external media job: {job_id}", flush=True)
        status = client.poll_job(
            "youtube-processor",
            "media-extract",
            job_id,
            args.poll_interval,
            args.timeout_seconds,
        )
        items, bundle = build_single_payload(status, args.single_url, args.single_category, args.language)
        manifest = {"date": report_date, "total_videos": len(items), "mode": "single-url-test"}
        return status, items, manifest, bundle

    job_id = client.create_daily_job(
        report_date,
        window_start,
        window_end,
        args.sources_profile,
        not args.no_require_transcripts,
        not args.no_allow_asr,
    )
    print(f"Created external daily job: {job_id}", flush=True)
    status = client.poll_job(
        "daily-collector",
        "daily-collect",
        job_id,
        args.poll_interval,
        args.timeout_seconds,
    )
    items = client.get_file_json(
        "daily-collector",
        f"daily-collect/{job_id}/files/daily_items.json",
        [],
    )
    manifest = client.get_file_json(
        "daily-collector",
        f"daily-collect/{job_id}/files/manifest.json",
        {},
    )
    bundle = client.get_file_json(
        "daily-collector",
        f"daily-collect/{job_id}/files/subtitles_bundle.json",
        {},
    )
    if not isinstance(items, list):
        raise ExternalServiceError("daily_items.json from external service is not a JSON array")
    if not isinstance(manifest, dict):
        raise ExternalServiceError("manifest.json from external service is not a JSON object")
    return status, items, manifest, bundle


def main() -> int:
    args = parse_args()
    tz = load_timezone(args.timezone)
    report_date = resolve_report_date(args.date, tz)
    window_start, window_end = resolve_window(report_date, tz, args.window_start, args.window_end)

    items_path = Path(args.items_json or REPORTS_DIR / f"daily_items_{report_date}.json")
    manifest_path = Path(args.manifest_json or REPORTS_DIR / f"daily_items_{report_date}.manifest.json")
    status_path = Path(args.status_json or REPORTS_DIR / f"youtube_external_{report_date}.status.json")
    subtitles_dir = Path(args.subtitles_dir)
    bundle_path = Path(args.bundle_path)

    client = ExternalTranscriptClient(args.api_base, args.media_token or "")
    existing_items = load_json(items_path, [])
    if not isinstance(existing_items, list):
        existing_items = []
    existing_manifest = load_json(manifest_path, {})
    if not isinstance(existing_manifest, dict):
        existing_manifest = {}

    youtube_status, external_items, external_manifest, external_bundle = collect_external_payload(
        client, args, report_date, window_start, window_end
    )
    bundle_index = index_bundle_records(external_bundle)

    cleanup_youtube_outputs(subtitles_dir)
    transcript_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    youtube_items: list[dict[str, Any]] = []

    for raw_item in external_items:
        if not isinstance(raw_item, dict):
            continue
        record = lookup_transcript_record(raw_item, bundle_index)
        item = normalize_youtube_item(raw_item, record, report_date)
        meta = write_youtube_transcript(item, record, subtitles_dir)
        if meta:
            transcript_results.append(
                {
                    "url": item["url"],
                    "status": "success",
                    "text_chars": meta.get("text_chars"),
                    "coverage_ratio": meta.get("coverage_ratio"),
                    "source_method": meta.get("source_method"),
                }
            )
            item["transcript_status"] = "success"
        else:
            item["transcript_status"] = "failed"
            message = f"transcript missing from external bundle for {item.get('url') or item.get('title')}"
            transcript_results.append({"url": item.get("url"), "status": "failed", "error_message": message})
            if not args.no_require_transcripts:
                errors.append({"url": item.get("url"), "error_type": "transcript_missing", "error_message": message})
        youtube_items.append(item)

    non_youtube_items = [
        item
        for item in existing_items
        if not isinstance(item, dict) or str(item.get("platform") or "").lower() != "youtube"
    ]
    merged_items = sort_items(deduplicate_items([*non_youtube_items, *youtube_items]))
    merged_manifest = merge_manifest(
        existing_manifest,
        report_date,
        window_start,
        window_end,
        merged_items,
        youtube_status,
        external_manifest,
        transcript_results,
        errors,
    )

    write_json(items_path, merged_items)
    write_json(manifest_path, merged_manifest)
    write_json(
        status_path,
        {
            "request": {
                "date": report_date,
                "window_start": iso_z(window_start),
                "window_end": iso_z(window_end),
                "api_base": args.api_base,
                "sources_profile": args.sources_profile,
                "single_url": args.single_url,
            },
            "external_status": youtube_status,
            "external_manifest": external_manifest,
            "transcript_results": transcript_results,
            "errors": errors,
        },
    )
    make_bundle(subtitles_dir, bundle_path)

    print(f"Merged {len(youtube_items)} YouTube item(s)")
    print(f"Wrote {items_path}")
    print(f"Wrote {manifest_path}")
    print(f"Wrote {bundle_path}")
    if errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
