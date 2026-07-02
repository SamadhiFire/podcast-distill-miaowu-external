#!/usr/bin/env python3
"""Collect Xiaoyuzhou updates and transcribe them with Alibaba Tingwu."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, time as datetime_time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any
from urllib.parse import urlparse
import zipfile

import requests

try:
    from aliyunsdkcore.auth.credentials import AccessKeyCredential
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest
except ModuleNotFoundError:  # pragma: no cover - dependency check happens at runtime.
    AccessKeyCredential = None  # type: ignore[assignment]
    AcsClient = None  # type: ignore[assignment]
    CommonRequest = None  # type: ignore[assignment]

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "config"
REPORTS_DIR = BASE_DIR / "reports"
TZ_SHANGHAI = timezone(timedelta(hours=8))
TINGWU_DOMAIN = "tingwu.cn-beijing.aliyuncs.com"
TINGWU_VERSION = "2023-09-30"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class CollectError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Report date in YYYY-MM-DD. Defaults to today in Asia/Shanghai.")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--window-start", help="Explicit ISO 8601 window start.")
    parser.add_argument("--window-end", help="Explicit ISO 8601 window end.")
    parser.add_argument("--sources-file", default=str(CONFIG_DIR / "xiaoyuzhou_sources.json"))
    parser.add_argument("--items-json")
    parser.add_argument("--manifest-json")
    parser.add_argument("--subtitles-dir", default="subtitles")
    parser.add_argument("--bundle-path", default="subtitles_bundle.zip")
    parser.add_argument("--status-json")
    parser.add_argument("--cache-dir", default=".cache/tingwu")
    parser.add_argument("--discover-only", action="store_true", help="Only scan Xiaoyuzhou updates; do not call Tingwu.")
    parser.add_argument("--max-items", type=int, help="Only transcribe the first N updated episodes.")
    parser.add_argument("--poll-interval", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=int, default=3 * 60 * 60)
    parser.add_argument("--force", action="store_true", help="Ignore cached transcript outputs.")
    parser.add_argument("--min-coverage", type=float, default=0.95)
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


def resolve_window(
    report_date: str,
    tz: timezone,
    explicit_start: str | None,
    explicit_end: str | None,
) -> tuple[datetime, datetime]:
    if bool(explicit_start) != bool(explicit_end):
        raise RuntimeError("--window-start and --window-end must be provided together")
    if explicit_start and explicit_end:
        start = datetime.fromisoformat(explicit_start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(explicit_end.replace("Z", "+00:00"))
    else:
        end_date = datetime.strptime(report_date, "%Y-%m-%d").date()
        end = datetime.combine(end_date, datetime_time(6, 0), tzinfo=tz)
        start = end - timedelta(days=1)
    if start.tzinfo is None or end.tzinfo is None:
        raise RuntimeError("window_start/window_end must include timezone offsets")
    return start, end


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_sources(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise RuntimeError(f"{path} must contain a JSON array")
        sources = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("source_url") or entry.get("url") or "").strip()
            if not url:
                continue
            sources.append(
                {
                    "category": str(entry.get("category") or "").strip(),
                    "platform": "xiaoyuzhou",
                    "source_name": str(entry.get("source_name") or entry.get("name") or "").strip(),
                    "source_url": url,
                }
            )
        return sources

    sources: list[dict[str, Any]] = []
    category = ""
    platform = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            category = re.sub(r"^##\s*\d+\.\s*", "", line).strip()
            continue
        if line == "### Xiaoyuzhou":
            platform = "xiaoyuzhou"
            continue
        if line == "### YouTube":
            platform = "youtube"
            continue
        if not line.startswith("- ") or platform != "xiaoyuzhou":
            continue
        parts = [part.strip() for part in line[2:].split("|")]
        if len(parts) < 2 or not parts[1].startswith("http"):
            continue
        sources.append(
            {
                "category": category,
                "platform": "xiaoyuzhou",
                "source_name": parts[0],
                "source_url": parts[1],
            }
        )
    return sources


def parse_next_data(html: str) -> dict[str, Any]:
    match = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise CollectError("__NEXT_DATA__ not found")
    return json.loads(match.group(1))


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def in_window(value: datetime | None, start: datetime, end: datetime) -> bool:
    if not value:
        return False
    return start.astimezone(timezone.utc) <= value.astimezone(timezone.utc) < end.astimezone(timezone.utc)


def episode_id_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if parts and parts[-2:-1] == ["episode"]:
        return parts[-1]
    if parts:
        return parts[-1]
    raise RuntimeError(f"Cannot parse Xiaoyuzhou episode id from URL: {url}")


def get_json_page(url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            return parse_next_data(response.content.decode("utf-8", errors="replace"))
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise CollectError(str(last_error))


def discover_source(source: dict[str, Any], start: datetime, end: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = get_json_page(source["source_url"])
    podcast = data.get("props", {}).get("pageProps", {}).get("podcast") or {}
    episodes = podcast.get("episodes") or []
    source_name = source["source_name"] or podcast.get("title") or ""
    updates: list[dict[str, Any]] = []
    for episode in episodes:
        published = parse_datetime(episode.get("pubDate"))
        if not in_window(published, start, end):
            continue
        eid = str(episode.get("eid") or "").strip()
        if not eid:
            continue
        duration = episode.get("duration")
        try:
            duration_seconds = int(float(duration or 0))
        except (TypeError, ValueError):
            duration_seconds = 0
        updates.append(
            {
                "platform": "xiaoyuzhou",
                "category": source["category"],
                "source_name": source_name,
                "source_url": source["source_url"],
                "title": episode.get("title") or "",
                "original_title": episode.get("title") or "",
                "url": f"https://www.xiaoyuzhoufm.com/episode/{eid}",
                "episode_id": eid,
                "published_at": published.astimezone(TZ_SHANGHAI).isoformat() if published else "",
                "duration": duration_seconds,
                "duration_seconds": duration_seconds,
                "description": episode.get("description") or episode.get("shownotes") or "",
            }
        )
    summary = {
        "source_name": source_name,
        "source_url": source["source_url"],
        "episode_count": len(updates),
        "status": "updated" if updates else "no_update",
    }
    return updates, summary


def resolve_episode_audio(item: dict[str, Any]) -> dict[str, Any]:
    data = get_json_page(item["url"])
    episode = data.get("props", {}).get("pageProps", {}).get("episode") or {}
    audio_url = (
        ((episode.get("enclosure") or {}).get("url"))
        or (((episode.get("media") or {}).get("source") or {}).get("url"))
        or (((episode.get("media") or {}).get("backupSource") or {}).get("url"))
    )
    if not audio_url:
        raise CollectError(f"audio_url not found for {item['url']}")
    podcast = episode.get("podcast") or {}
    title = episode.get("title") or item.get("title") or ""
    published = parse_datetime(episode.get("pubDate"))
    duration = episode.get("duration") or item.get("duration_seconds") or 0
    try:
        duration_seconds = int(float(duration))
    except (TypeError, ValueError):
        duration_seconds = int(item.get("duration_seconds") or 0)
    enriched = dict(item)
    enriched.update(
        {
            "title": title,
            "original_title": title,
            "source_name": podcast.get("title") or item.get("source_name") or "",
            "audio_url": audio_url,
            "published_at": published.astimezone(TZ_SHANGHAI).isoformat() if published else item.get("published_at", ""),
            "duration": duration_seconds,
            "duration_seconds": duration_seconds,
            "description": episode.get("description") or episode.get("shownotes") or item.get("description", ""),
        }
    )
    return enriched


class TingwuClient:
    def __init__(self, app_key: str, access_key_id: str, access_key_secret: str) -> None:
        if AcsClient is None or CommonRequest is None or AccessKeyCredential is None:
            raise RuntimeError("aliyun-python-sdk-core is required. Run: pip install aliyun-python-sdk-core")
        self.app_key = app_key
        credential = AccessKeyCredential(access_key_id, access_key_secret)
        self.client = AcsClient(region_id="cn-beijing", credential=credential)

    def request(self, method: str, uri: str) -> Any:
        req = CommonRequest()
        req.set_accept_format("json")
        req.set_domain(TINGWU_DOMAIN)
        req.set_version(TINGWU_VERSION)
        req.set_protocol_type("https")
        req.set_method(method)
        req.set_uri_pattern(uri)
        req.add_header("Content-Type", "application/json")
        return req

    def create_task(self, item: dict[str, Any]) -> dict[str, Any]:
        body = {
            "AppKey": self.app_key,
            "Input": {
                "SourceLanguage": "cn",
                "TaskKey": f"xiaoyuzhou_{item['episode_id']}",
                "FileUrl": item["audio_url"],
            },
            "Parameters": {
                "Transcription": {
                    "DiarizationEnabled": False,
                }
            },
        }
        req = self.request("PUT", "/openapi/tingwu/v2/tasks")
        req.add_query_param("type", "offline")
        req.set_content(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        return json.loads(self.client.do_action_with_exception(req))

    def get_task(self, task_id: str) -> dict[str, Any]:
        req = self.request("GET", f"/openapi/tingwu/v2/tasks/{task_id}")
        return json.loads(self.client.do_action_with_exception(req))

    def wait_for_task(self, task_id: str, poll_interval: int, timeout_seconds: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = self.get_task(task_id)
            data = status.get("Data") or {}
            task_status = str(data.get("TaskStatus") or "")
            print(f"Tingwu task {task_id}: {task_status}", flush=True)
            if task_status in {"COMPLETED", "FAILED"}:
                return status
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Tingwu task timed out after {timeout_seconds} seconds: {task_id}")
            time.sleep(max(5, poll_interval))


def format_srt_time(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(ms_total, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def format_vtt_time(seconds: float) -> str:
    return format_srt_time(seconds).replace(",", ".")


def extract_segments(transcription_json: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    transcription = transcription_json.get("Transcription") or {}
    paragraphs = transcription.get("Paragraphs") or []
    groups: dict[tuple[int, int], dict[str, Any]] = {}
    order = 0
    for paragraph_index, paragraph in enumerate(paragraphs):
        words = paragraph.get("Words") or []
        for word in words:
            sentence_id = int(word.get("SentenceId") or 0)
            key = (paragraph_index, sentence_id or order)
            if key not in groups:
                groups[key] = {
                    "order": order,
                    "start": word.get("Start"),
                    "end": word.get("End"),
                    "texts": [],
                }
                order += 1
            group = groups[key]
            group["texts"].append(str(word.get("Text") or ""))
            group["start"] = min_number(group.get("start"), word.get("Start"))
            group["end"] = max_number(group.get("end"), word.get("End"))

    segments: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda value: value["order"]):
        text = "".join(group["texts"]).strip()
        if not text:
            continue
        start = float(group.get("start") or 0) / 1000.0
        end = float(group.get("end") or 0) / 1000.0
        if end <= start:
            end = start + 0.5
        segments.append({"start": start, "end": end, "text": text})

    audio_info = transcription.get("AudioInfo") or {}
    duration_ms = audio_info.get("Duration") or 0
    try:
        audio_duration_seconds = float(duration_ms) / 1000.0
    except (TypeError, ValueError):
        audio_duration_seconds = 0.0
    return segments, audio_duration_seconds


def min_number(a: Any, b: Any) -> Any:
    values = [value for value in (a, b) if value is not None]
    if not values:
        return None
    return min(values)


def max_number(a: Any, b: Any) -> Any:
    values = [value for value in (a, b) if value is not None]
    if not values:
        return None
    return max(values)


def write_transcript_files(
    item: dict[str, Any],
    transcription_json: dict[str, Any],
    subtitles_dir: Path,
    min_coverage: float,
) -> dict[str, Any]:
    segments, audio_duration_seconds = extract_segments(transcription_json)
    if not segments:
        raise RuntimeError(f"Tingwu returned no transcript segments for {item['url']}")
    episode_id = item["episode_id"]
    stem = f"xiaoyuzhou_{episode_id}"
    txt_name = f"{stem}.txt"
    srt_name = f"{stem}.srt"
    vtt_name = f"{stem}.vtt"
    meta_name = f"{stem}.json"

    subtitles_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(segment["text"] for segment in segments).strip() + "\n"
    (subtitles_dir / txt_name).write_text(text, encoding="utf-8", newline="\n")

    srt_lines: list[str] = []
    vtt_lines = ["WEBVTT", ""]
    for idx, segment in enumerate(segments, 1):
        srt_lines.extend(
            [
                str(idx),
                f"{format_srt_time(segment['start'])} --> {format_srt_time(segment['end'])}",
                segment["text"],
                "",
            ]
        )
        vtt_lines.extend(
            [
                str(idx),
                f"{format_vtt_time(segment['start'])} --> {format_vtt_time(segment['end'])}",
                segment["text"],
                "",
            ]
        )
    (subtitles_dir / srt_name).write_text("\n".join(srt_lines), encoding="utf-8", newline="\n")
    (subtitles_dir / vtt_name).write_text("\n".join(vtt_lines), encoding="utf-8", newline="\n")

    text_bytes = text.encode("utf-8")
    duration_seconds = float(item.get("duration_seconds") or 0)
    if audio_duration_seconds > duration_seconds:
        duration_seconds = audio_duration_seconds
    last_timestamp_seconds = max(segment["end"] for segment in segments)
    coverage_ratio = min(last_timestamp_seconds / duration_seconds, 1.0) if duration_seconds else 0.0
    meta = {
        "platform": "xiaoyuzhou",
        "episode_id": episode_id,
        "url": item["url"],
        "title": item.get("title", ""),
        "source_name": item.get("source_name", ""),
        "source_url": item.get("source_url", ""),
        "audio_url": item.get("audio_url", ""),
        "text": txt_name,
        "subtitle_srt": srt_name,
        "subtitle_vtt": vtt_name,
        "text_chars": len(text),
        "sha256": hashlib.sha256(text_bytes).hexdigest(),
        "duration_seconds": duration_seconds,
        "last_timestamp_seconds": last_timestamp_seconds,
        "coverage_ratio": coverage_ratio,
        "source": "asr",
        "source_method": "asr",
        "asr_provider": "aliyun_tingwu",
        "language": "zh",
    }
    write_json(subtitles_dir / meta_name, meta)
    if duration_seconds >= 300 and coverage_ratio < min_coverage:
        raise RuntimeError(
            f"coverage_ratio {coverage_ratio:.4f} < {min_coverage:.4f} for {item['url']}"
        )
    return meta


def download_json(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Tingwu result is not a JSON object")
    return data


def cached_meta(cache_dir: Path, subtitles_dir: Path, episode_id: str) -> dict[str, Any] | None:
    cache_meta = cache_dir / episode_id / f"xiaoyuzhou_{episode_id}.json"
    if not cache_meta.exists():
        return None
    stem = f"xiaoyuzhou_{episode_id}"
    for suffix in (".json", ".txt", ".srt", ".vtt"):
        source = cache_dir / episode_id / f"{stem}{suffix}"
        if not source.exists():
            return None
    subtitles_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (".json", ".txt", ".srt", ".vtt"):
        shutil.copy2(cache_dir / episode_id / f"{stem}{suffix}", subtitles_dir / f"{stem}{suffix}")
    return json.loads(cache_meta.read_text(encoding="utf-8"))


def save_to_cache(cache_dir: Path, subtitles_dir: Path, episode_id: str) -> None:
    stem = f"xiaoyuzhou_{episode_id}"
    target_dir = cache_dir / episode_id
    target_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (".json", ".txt", ".srt", ".vtt"):
        source = subtitles_dir / f"{stem}{suffix}"
        if source.exists():
            shutil.copy2(source, target_dir / source.name)


def transcribe_item(
    item: dict[str, Any],
    client: TingwuClient,
    subtitles_dir: Path,
    cache_dir: Path,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any]]:
    episode_id = item["episode_id"]
    enriched = resolve_episode_audio(item)
    if not args.force:
        meta = cached_meta(cache_dir, subtitles_dir, episode_id)
        if meta:
            meta.update(
                {
                    "title": enriched.get("title", meta.get("title", "")),
                    "source_name": enriched.get("source_name", meta.get("source_name", "")),
                    "source_url": enriched.get("source_url", meta.get("source_url", "")),
                    "audio_url": enriched.get("audio_url", meta.get("audio_url", "")),
                }
            )
            stem = f"xiaoyuzhou_{episode_id}"
            write_json(subtitles_dir / f"{stem}.json", meta)
            save_to_cache(cache_dir, subtitles_dir, episode_id)
            return {**enriched, "transcript_status": "success", "transcript_meta": meta}, {
                "url": enriched["url"],
                "status": "success",
                "cached": True,
                "text_chars": meta.get("text_chars"),
                "coverage_ratio": meta.get("coverage_ratio"),
            }

    created = client.create_task(enriched)
    data = created.get("Data") or {}
    task_id = data.get("TaskId")
    if not task_id:
        raise RuntimeError(f"Tingwu create task response has no TaskId: {created}")
    final = client.wait_for_task(task_id, args.poll_interval, args.timeout_seconds)
    final_data = final.get("Data") or {}
    if final_data.get("TaskStatus") != "COMPLETED":
        raise RuntimeError(f"Tingwu task failed: {json.dumps(final, ensure_ascii=False)[:1000]}")
    result_url = (final_data.get("Result") or {}).get("Transcription")
    if not result_url:
        raise RuntimeError(f"Tingwu task completed without Transcription result URL: {task_id}")
    transcription_json = download_json(result_url)
    meta = write_transcript_files(enriched, transcription_json, subtitles_dir, args.min_coverage)
    save_to_cache(cache_dir, subtitles_dir, episode_id)
    return {**enriched, "transcript_status": "success", "transcript_meta": meta}, {
        "url": enriched["url"],
        "status": "success",
        "cached": False,
        "task_id": task_id,
        "text_chars": meta.get("text_chars"),
        "coverage_ratio": meta.get("coverage_ratio"),
    }


def make_bundle(subtitles_dir: Path, bundle_path: Path) -> None:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if subtitles_dir.exists():
            for path in sorted(subtitles_dir.glob("*")):
                if path.is_file():
                    archive.write(path, Path("subtitles") / path.name)


def main() -> int:
    args = parse_args()
    tz = load_timezone(args.timezone)
    report_date = resolve_report_date(args.date, tz)
    window_start, window_end = resolve_window(report_date, tz, args.window_start, args.window_end)

    reports_dir = REPORTS_DIR
    items_path = Path(args.items_json or reports_dir / f"daily_items_{report_date}.json")
    manifest_path = Path(args.manifest_json or reports_dir / f"daily_items_{report_date}.manifest.json")
    status_path = Path(args.status_json or reports_dir / f"tingwu_xiaoyuzhou_{report_date}.status.json")
    subtitles_dir = Path(args.subtitles_dir)
    bundle_path = Path(args.bundle_path)
    cache_dir = Path(args.cache_dir)

    if subtitles_dir.exists() and not args.discover_only:
        shutil.rmtree(subtitles_dir)
    subtitles_dir.mkdir(parents=True, exist_ok=True)

    sources = parse_sources(Path(args.sources_file))
    all_items: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    print(
        f"Scanning {len(sources)} Xiaoyuzhou source(s): "
        f"{window_start.isoformat()} -> {window_end.isoformat()}",
        flush=True,
    )
    for source in sources:
        try:
            updates, summary = discover_source(source, window_start, window_end)
            all_items.extend(updates)
            source_summaries.append(summary)
            print(f"- {summary['source_name']}: {summary['episode_count']} update(s)", flush=True)
        except Exception as exc:
            source_summaries.append(
                {
                    "source_name": source["source_name"],
                    "source_url": source["source_url"],
                    "episode_count": 0,
                    "status": "error",
                    "error_message": str(exc),
                }
            )
            failures.append({"source_url": source["source_url"], "error_type": "source_resolve_failed", "error_message": str(exc)})
            print(f"- {source['source_name']}: ERROR {exc}", flush=True)

    selected_items = all_items[: args.max_items] if args.max_items else list(all_items)
    completed_items: list[dict[str, Any]] = []
    transcript_results: list[dict[str, Any]] = []
    if args.discover_only:
        for item in selected_items:
            try:
                completed_items.append(resolve_episode_audio(item))
            except Exception as exc:
                completed_items.append({**item, "audio_url": "", "resolve_error": str(exc)})
                failures.append({"url": item["url"], "error_type": "episode_resolve_failed", "error_message": str(exc)})
    else:
        app_key = os.getenv("TINGWU_APP_KEY")
        access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID") or os.getenv("ALIBABA_ACCESS_KEY_ID")
        access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET") or os.getenv("ALIBABA_ACCESS_KEY_SECRET")
        if selected_items and not (app_key and access_key_id and access_key_secret):
            raise RuntimeError(
                "TINGWU_APP_KEY, ALIBABA_CLOUD_ACCESS_KEY_ID and "
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET are required for transcription"
            )
        client = TingwuClient(app_key or "", access_key_id or "", access_key_secret or "") if selected_items else None
        for index, item in enumerate(selected_items, 1):
            print(f"Transcribing {index}/{len(selected_items)}: {item['title']} ({item['url']})", flush=True)
            try:
                assert client is not None
                completed, result = transcribe_item(item, client, subtitles_dir, cache_dir, args)
                completed_items.append(completed)
                transcript_results.append(result)
            except Exception as exc:
                failed = {**item, "transcript_status": "failed", "error_type": "asr_failed", "error_message": str(exc)}
                completed_items.append(failed)
                transcript_results.append({"url": item["url"], "status": "failed", "error_message": str(exc)})
                failures.append({"url": item["url"], "error_type": "asr_failed", "error_message": str(exc)})
                print(f"  ERROR: {exc}", flush=True)

    daily_items = [
        {
            key: value
            for key, value in item.items()
            if key
            not in {
                "transcript_meta",
                "episode_id",
            }
        }
        for item in completed_items
    ]
    success_count = sum(1 for item in completed_items if item.get("transcript_status") == "success")
    failure_count = sum(1 for item in completed_items if item.get("transcript_status") == "failed")
    no_update_count = sum(1 for summary in source_summaries if summary.get("status") == "no_update")
    manifest = {
        "status": "failed" if failures and not args.discover_only else "success",
        "date": report_date,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "sources_profile": "xiaoyuzhou-default",
        "source_count": len(sources),
        "item_count": len(completed_items),
        "success_count": success_count,
        "no_update_count": no_update_count,
        "failure_count": failure_count + sum(1 for failure in failures if "source_url" in failure),
        "sources_summary": source_summaries,
        "transcript_results": transcript_results,
        "errors": failures,
    }

    write_json(items_path, daily_items)
    write_json(manifest_path, manifest)
    write_json(status_path, {"request": {"date": report_date}, "manifest": manifest})
    make_bundle(subtitles_dir, bundle_path)
    print(f"Wrote {items_path}")
    print(f"Wrote {manifest_path}")
    print(f"Wrote {bundle_path}")
    if failures and not args.discover_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
