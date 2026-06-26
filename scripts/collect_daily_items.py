#!/usr/bin/env python3
"""Collect newly published YouTube and Xiaoyuzhou items for the daily digest."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from yt_dlp import YoutubeDL


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "config"
REPORTS_DIR = BASE_DIR / "reports"
TZ_SHANGHAI = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Report date in YYYY-MM-DD, default: today in Asia/Shanghai")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--youtube-scan-limit", type=int, default=25)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-urls", default="config/daily_urls.txt")
    return parser.parse_args()


def report_date(value: str | None) -> datetime:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ_SHANGHAI)
    return datetime.now(TZ_SHANGHAI)


def normalize_playlist_url(url: str) -> str:
    parsed = urlparse(url.strip())
    qs = parse_qs(parsed.query)
    if "list" in qs:
        return "https://www.youtube.com/playlist?" + urlencode({"list": qs["list"][0]})
    return url.strip()


def parse_category_file(path: Path) -> list[dict[str, Any]]:
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
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        parts = [part.strip() for part in body.split("|")]
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        if not url.startswith("http"):
            continue
        opts = ",".join(parts[2:])
        min_duration = 0
        match = re.search(r"min_duration=(\d+)", opts)
        if match:
            min_duration = int(match.group(1))
        if platform == "youtube":
            url = normalize_playlist_url(url)
        sources.append(
            {
                "category": category,
                "platform": platform,
                "name": name,
                "url": url,
                "min_duration": min_duration,
            }
        )
    return sources


def parse_next_data(html: str) -> dict[str, Any] | None:
    match = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        return None
    return json.loads(match.group(1))


def fetch_xiaoyuzhou_source(source: dict[str, Any], cutoff_utc: datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = parse_next_data(resp.text)
        if not data:
            return items
        podcast = data.get("props", {}).get("pageProps", {}).get("podcast") or {}
        episodes = podcast.get("episodes") or []
    except Exception as exc:
        print(f"[xiaoyuzhou] {source['name']} failed: {exc}")
        return items

    for ep in episodes:
        pub = parse_datetime(ep.get("pubDate"))
        if not pub or pub < cutoff_utc:
            continue
        items.append(
            {
                "platform": "xiaoyuzhou",
                "category": source["category"],
                "source_name": podcast.get("title") or source["name"],
                "source_url": source["url"],
                "title": ep.get("title") or "",
                "original_title": ep.get("title") or "",
                "url": f"https://www.xiaoyuzhoufm.com/episode/{ep.get('eid')}",
                "published_at": pub.isoformat(),
                "duration": ep.get("duration"),
                "description": ep.get("description") or "",
            }
        )
    return items


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def video_url_from_entry(entry: dict[str, Any]) -> str | None:
    url = entry.get("webpage_url") or entry.get("url")
    if url and url.startswith("http"):
        return url
    vid = entry.get("id")
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def fetch_youtube_source(
    source: dict[str, Any],
    cutoff_utc: datetime,
    scan_limit: int,
    cookies: str | None,
) -> list[dict[str, Any]]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": scan_limit,
        "skip_download": True,
        "socket_timeout": 30,
        "ignoreerrors": True,
    }
    if cookies:
        opts["cookiefile"] = cookies

    items: list[dict[str, Any]] = []
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source["url"], download=False)
            entries = [entry for entry in (info.get("entries") or []) if entry]
    except Exception as exc:
        print(f"[youtube] {source['name']} failed: {exc}")
        return items

    detail_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 30,
        "ignoreerrors": True,
    }
    if cookies:
        detail_opts["cookiefile"] = cookies

    with YoutubeDL(detail_opts) as ydl:
        for entry in entries:
            url = video_url_from_entry(entry)
            if not url:
                continue
            try:
                detail = ydl.extract_info(url, download=False)
            except Exception:
                detail = entry
            duration = detail.get("duration") or entry.get("duration") or 0
            if source.get("min_duration", 0) and duration < source["min_duration"]:
                continue
            pub = parse_datetime(detail.get("timestamp") or detail.get("upload_date") or detail.get("release_timestamp"))
            if not pub or pub < cutoff_utc:
                continue
            items.append(
                {
                    "platform": "youtube",
                    "category": source["category"],
                    "source_name": detail.get("channel") or detail.get("uploader") or source["name"],
                    "source_url": source["url"],
                    "title": detail.get("title") or entry.get("title") or "",
                    "original_title": detail.get("title") or entry.get("title") or "",
                    "url": detail.get("webpage_url") or url,
                    "published_at": pub.isoformat(),
                    "duration": duration,
                    "description": detail.get("description") or "",
                    "view_count": detail.get("view_count"),
                    "like_count": detail.get("like_count"),
                    "comment_count": detail.get("comment_count"),
                    "chapters": detail.get("chapters") or [],
                    "subtitles": sorted((detail.get("subtitles") or {}).keys()),
                    "automatic_captions": sorted((detail.get("automatic_captions") or {}).keys()),
                }
            )
    return items


def main() -> int:
    args = parse_args()
    date = report_date(args.date)
    cutoff_utc = (date - timedelta(hours=args.lookback_hours)).astimezone(timezone.utc)
    sources = parse_category_file(CONFIG_DIR / "sources_by_category.md")
    cookies = "cookies.txt" if Path("cookies.txt").exists() else None

    all_items: list[dict[str, Any]] = []
    for source in sources:
        if source["platform"] == "xiaoyuzhou":
            all_items.extend(fetch_xiaoyuzhou_source(source, cutoff_utc))
        elif source["platform"] == "youtube":
            all_items.extend(fetch_youtube_source(source, cutoff_utc, args.youtube_scan_limit, cookies))

    all_items.sort(key=lambda item: item.get("published_at") or "", reverse=True)

    REPORTS_DIR.mkdir(exist_ok=True)
    out_json = Path(args.output_json) if args.output_json else REPORTS_DIR / f"daily_items_{date:%Y-%m-%d}.json"
    out_json.write_text(json.dumps(all_items, ensure_ascii=False, indent=2), encoding="utf-8")

    out_urls = Path(args.output_urls)
    out_urls.parent.mkdir(parents=True, exist_ok=True)
    out_urls.write_text(
        "\n".join(["# Auto-generated daily extraction URLs"] + [item["url"] for item in all_items]) + "\n",
        encoding="utf-8",
    )
    print(f"Collected {len(all_items)} items")
    print(f"Items: {out_json}")
    print(f"URLs: {out_urls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
