#!/usr/bin/env python3
"""
Unified subtitle extractor for YouTube, Bilibili and Xiaoyuzhou.

Outputs timed subtitles plus plain text:
  - YouTube: yt-dlp native subtitles first, youtube-transcript-api fallback
  - Bilibili: public player subtitle API first, yt-dlp + ASR fallback
  - Xiaoyuzhou: official transcript API when authenticated, ASR fallback
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

XIAO_APP_HOST = "https://www.xiaoyuzhoufm.com"
XIAO_TRANSCRIPT_ENDPOINT = (
    "https://podcast-api.midway.run/management/episode-transcript/get"
)


@dataclass
class ExtractionResult:
    ok: bool
    platform: str
    title: str = ""
    media_id: str = ""
    lang: str = ""
    source: str = ""
    subtitle_path: str | None = None
    text_path: str | None = None
    metadata_path: str | None = None
    message: str = ""


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "bilibili.com" in host or "b23.tv" in host:
        return "bilibili"
    if "xiaoyuzhoufm.com" in host:
        return "xiaoyuzhou"
    return "unknown"


def sanitize_filename(value: str, max_len: int = 120) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or "untitled")[:max_len]


def split_langs(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def normalize_lang(value: str) -> str:
    value = value.lower().replace("_", "-")
    return re.sub(r"[^a-z0-9-]", "", value)


def lang_matches(candidate: str, wanted: str) -> bool:
    candidate_n = normalize_lang(candidate)
    wanted_n = normalize_lang(wanted.replace(".*", ""))
    if not wanted_n or wanted_n == "all":
        return True
    if candidate_n == wanted_n:
        return True
    if wanted_n in {"zh", "zh-hans", "zh-cn"}:
        return candidate_n.startswith("zh") or candidate_n in {"chi", "zho"}
    if wanted_n == "en":
        return candidate_n == "en" or candidate_n.startswith("en-")
    return candidate_n.startswith(wanted_n + "-")


def choose_lang_key(keys: list[str], preferred: list[str]) -> str | None:
    for wanted in preferred:
        for key in keys:
            if lang_matches(key, wanted):
                return key
    return keys[0] if "all" in [normalize_lang(x) for x in preferred] and keys else None


def ensure_output_dir(path: str | Path) -> Path:
    output_dir = Path(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def make_base_path(output_dir: Path, platform: str, media_id: str, title: str) -> Path:
    head = sanitize_filename(f"{platform}_{media_id or 'item'}", 80)
    tail = sanitize_filename(title, 60)
    return output_dir / sanitize_filename(f"{head}_{tail}", 150)


def existing_bundle_result(
    output_dir: Path,
    platform: str,
    media_id: str,
    title: str,
    url: str,
) -> ExtractionResult | None:
    base = make_base_path(output_dir, platform, media_id, title)
    txt_path = base.with_suffix(".txt")
    vtt_path = base.with_suffix(".vtt")
    srt_path = base.with_suffix(".srt")
    metadata_path = base.with_suffix(".json")
    subtitle_path = vtt_path if vtt_path.exists() else srt_path
    if txt_path.exists() and subtitle_path.exists():
        return ExtractionResult(
            ok=True,
            platform=platform,
            title=title,
            media_id=media_id,
            source="existing",
            subtitle_path=str(subtitle_path),
            text_path=str(txt_path),
            metadata_path=str(metadata_path) if metadata_path.exists() else None,
            message=f"existing output for {url}",
        )
    return None


def format_vtt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    millis = int(round(seconds * 1000))
    h, rem = divmod(millis, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def format_srt_time(seconds: float) -> str:
    return format_vtt_time(seconds).replace(".", ",")


def segments_to_vtt(segments: list[dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0))
        end = float(segment.get("end", start + 1))
        lines.append(f"{format_vtt_time(start)} --> {format_vtt_time(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def segments_to_srt(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    idx = 1
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0))
        end = float(segment.get("end", start + 1))
        lines.append(str(idx))
        lines.append(f"{format_srt_time(start)} --> {format_srt_time(end)}")
        lines.append(text)
        lines.append("")
        idx += 1
    return "\n".join(lines)


def segments_to_text(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    previous = None
    for segment in segments:
        text = html.unescape(str(segment.get("text", ""))).strip()
        text = re.sub(r"\s+", " ", text)
        if text and text != previous:
            lines.append(text)
            previous = text
    return "\n".join(lines)


def parse_caption_text(content: str) -> str:
    lines: list[str] = []
    previous = None
    in_style = False
    for raw in content.splitlines():
        line = raw.strip()
        upper = line.upper()
        if not line:
            in_style = False
            continue
        if upper in {"WEBVTT", "STYLE", "REGION"}:
            in_style = upper in {"STYLE", "REGION"}
            continue
        if in_style:
            continue
        if "-->" in line:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if line.startswith(("NOTE", "Kind:", "Language:", "X-TIMESTAMP-MAP")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{[^}]*\}", "", line)
        line = html.unescape(line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and line != previous:
            lines.append(line)
            previous = line
    return "\n".join(lines)


def write_text(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def write_segments_bundle(
    output_dir: Path,
    platform: str,
    media_id: str,
    title: str,
    url: str,
    lang: str,
    source: str,
    segments: list[dict[str, Any]],
) -> ExtractionResult:
    base = make_base_path(output_dir, platform, media_id, title)
    vtt_path = write_text(base.with_suffix(".vtt"), segments_to_vtt(segments))
    srt_path = write_text(base.with_suffix(".srt"), segments_to_srt(segments))
    txt_path = write_text(base.with_suffix(".txt"), segments_to_text(segments))
    metadata = {
        "platform": platform,
        "media_id": media_id,
        "title": title,
        "url": url,
        "language": lang,
        "source": source,
        "segment_count": len(segments),
        "subtitle_vtt": vtt_path.name,
        "subtitle_srt": srt_path.name,
        "text": txt_path.name,
    }
    metadata_path = write_text(
        base.with_suffix(".json"), json.dumps(metadata, ensure_ascii=False, indent=2)
    )
    return ExtractionResult(
        ok=True,
        platform=platform,
        title=title,
        media_id=media_id,
        lang=lang,
        source=source,
        subtitle_path=str(vtt_path),
        text_path=str(txt_path),
        metadata_path=str(metadata_path),
    )


def write_caption_bundle(
    output_dir: Path,
    platform: str,
    media_id: str,
    title: str,
    url: str,
    lang: str,
    source: str,
    caption: str,
    ext: str,
) -> ExtractionResult:
    base = make_base_path(output_dir, platform, media_id, title)
    ext = ext.lower().lstrip(".") or "vtt"
    subtitle_path = write_text(base.with_suffix(f".{ext}"), caption)
    txt_path = write_text(base.with_suffix(".txt"), parse_caption_text(caption))
    metadata = {
        "platform": platform,
        "media_id": media_id,
        "title": title,
        "url": url,
        "language": lang,
        "source": source,
        "subtitle": subtitle_path.name,
        "text": txt_path.name,
    }
    metadata_path = write_text(
        base.with_suffix(".json"), json.dumps(metadata, ensure_ascii=False, indent=2)
    )
    return ExtractionResult(
        ok=True,
        platform=platform,
        title=title,
        media_id=media_id,
        lang=lang,
        source=source,
        subtitle_path=str(subtitle_path),
        text_path=str(txt_path),
        metadata_path=str(metadata_path),
    )


def request_text(url: str, headers: dict[str, str] | None = None, timeout: int = 60) -> str:
    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=merged, timeout=timeout)
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"download failed: {last_error}")


def run_command(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def choose_ytdlp_subtitle(info: dict[str, Any], preferred: list[str]) -> tuple[str, dict[str, Any], bool] | None:
    native = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    for generated, captions in [(False, native), (True, automatic)]:
        key = choose_lang_key(list(captions.keys()), preferred)
        if not key:
            continue
        formats = captions.get(key) or []
        for wanted_ext in ["vtt", "srt", "ttml", "json3"]:
            for item in formats:
                if item.get("url") and item.get("ext") == wanted_ext:
                    return key, item, generated
        for item in formats:
            if item.get("url"):
                return key, item, generated
    return None


def extract_youtube_ytdlp(
    url: str,
    output_dir: Path,
    preferred_langs: list[str],
    cookies: str | None,
    force: bool,
) -> ExtractionResult | None:
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        print("  yt-dlp is not installed")
        return None

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if cookies:
        opts["cookiefile"] = cookies

    print("  YouTube: probing yt-dlp captions...")
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        print(f"  yt-dlp metadata failed: {exc}")
        return None

    if "entries" in info and info["entries"]:
        info = info["entries"][0]
    media_id = info.get("id") or parse_youtube_id(url) or "youtube"
    title = info.get("title") or media_id
    if not force:
        existing = existing_bundle_result(output_dir, "youtube", media_id, title, url)
        if existing:
            print("  YouTube: existing output found, skipping")
            return existing
    chosen = choose_ytdlp_subtitle(info, preferred_langs)
    if not chosen:
        print("  yt-dlp found no matching captions")
        return None

    lang, item, generated = chosen
    ext = item.get("ext") or "vtt"
    source = "yt-dlp:auto" if generated else "yt-dlp:manual"
    print(f"  YouTube: downloading {lang} captions via {source}")
    try:
        caption = request_text(item["url"], headers={"Referer": url}, timeout=60)
    except Exception as exc:
        print(f"  yt-dlp caption download failed: {exc}")
        return None
    return write_caption_bundle(
        output_dir, "youtube", media_id, title, url, lang, source, caption, ext
    )


def parse_youtube_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/") or None
    qs = parse_qs(parsed.query)
    if qs.get("v"):
        return qs["v"][0]
    match = re.search(r"(?:/embed/|/shorts/|/v/)([A-Za-z0-9_-]{11})", url)
    return match.group(1) if match else None


def snippet_to_segment(snippet: Any) -> dict[str, Any]:
    if isinstance(snippet, dict):
        start = float(snippet.get("start", 0))
        duration = float(snippet.get("duration", 0))
        text = snippet.get("text", "")
    else:
        start = float(getattr(snippet, "start", 0))
        duration = float(getattr(snippet, "duration", 0))
        text = getattr(snippet, "text", "")
    return {"start": start, "end": start + max(duration, 0.1), "text": text}


def extract_youtube_transcript_api(
    url: str, output_dir: Path, preferred_langs: list[str]
) -> ExtractionResult | None:
    video_id = parse_youtube_id(url)
    if not video_id:
        return None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        print("  youtube-transcript-api is not installed")
        return None

    print("  YouTube: trying youtube-transcript-api fallback...")
    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        transcript = transcript_list.find_transcript(preferred_langs)
        fetched = transcript.fetch()
    except Exception as exc:
        print(f"  youtube-transcript-api failed: {exc}")
        return None

    segments = [snippet_to_segment(item) for item in fetched]
    title = f"YouTube {video_id}"
    lang = getattr(transcript, "language_code", "") or preferred_langs[0]
    source = "youtube-transcript-api:auto" if transcript.is_generated else "youtube-transcript-api:manual"
    return write_segments_bundle(
        output_dir, "youtube", video_id, title, url, lang, source, segments
    )


def extract_youtube(url: str, output_dir: Path, args: argparse.Namespace) -> ExtractionResult:
    preferred = split_langs(args.lang)
    result = extract_youtube_ytdlp(url, output_dir, preferred, args.cookies, args.force)
    if result:
        return result
    result = extract_youtube_transcript_api(url, output_dir, preferred)
    if result:
        return result
    result = transcribe_ytdlp_audio(url, output_dir, "youtube", parse_youtube_id(url), "", args)
    if result:
        return result
    return ExtractionResult(False, "youtube", message="no subtitles found and ASR is not available")


def resolve_bilibili_url(url: str) -> str:
    if "b23.tv" not in urlparse(url).netloc.lower():
        return url
    resp = requests.get(url, headers=DEFAULT_HEADERS, allow_redirects=True, timeout=30)
    return resp.url


def parse_bvid(url: str) -> str | None:
    match = re.search(r"(BV[0-9A-Za-z]+)", url)
    return match.group(1) if match else None


def bilibili_headers(args: argparse.Namespace) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    headers.update({"Referer": "https://www.bilibili.com", "Accept": "application/json"})
    cookie = args.bilibili_cookie or os.getenv("BILIBILI_COOKIE")
    if cookie:
        headers["Cookie"] = cookie
    return headers


def request_json(url: str, headers: dict[str, str], params: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def choose_bilibili_subtitle(subtitles: list[dict[str, Any]], preferred: list[str]) -> dict[str, Any] | None:
    keys = [str(item.get("lan") or item.get("lan_doc") or "") for item in subtitles]
    key = choose_lang_key(keys, preferred)
    if key:
        for item in subtitles:
            if str(item.get("lan") or item.get("lan_doc") or "") == key:
                return item
    return subtitles[0] if subtitles else None


def extract_bilibili(url: str, output_dir: Path, args: argparse.Namespace) -> ExtractionResult:
    url = resolve_bilibili_url(url)
    bvid = parse_bvid(url)
    if not bvid:
        return ExtractionResult(False, "bilibili", message="cannot find BV id")

    headers = bilibili_headers(args)
    print("  Bilibili: probing public player subtitle API...")
    try:
        view = request_json(
            "https://api.bilibili.com/x/web-interface/view",
            headers,
            {"bvid": bvid},
        )
        if view.get("code") != 0:
            raise RuntimeError(view.get("message") or view)
        data = view["data"]
        pages = data.get("pages") or []
        page_no = int(parse_qs(urlparse(url).query).get("p", ["1"])[0])
        page = pages[max(0, min(page_no - 1, len(pages) - 1))] if pages else {}
        cid = page.get("cid")
        title = data.get("title") or bvid
        if page.get("part") and len(pages) > 1:
            title = f"{title} - {page.get('part')}"
        if not args.force:
            existing = existing_bundle_result(output_dir, "bilibili", bvid, title, url)
            if existing:
                print("  Bilibili: existing output found, skipping")
                return existing
        player = request_json(
            "https://api.bilibili.com/x/player/v2",
            headers,
            {"bvid": bvid, "cid": cid},
        )
        player_data = player.get("data") or {}
        subtitle_info = player_data.get("subtitle") or {}
        subtitles = subtitle_info.get("subtitles") or []
    except Exception as exc:
        print(f"  Bilibili API failed: {exc}")
        result = transcribe_ytdlp_audio(url, output_dir, "bilibili", bvid, "", args)
        return result or ExtractionResult(False, "bilibili", media_id=bvid, message=str(exc))

    chosen = choose_bilibili_subtitle(subtitles, split_langs(args.lang))
    if not chosen:
        if player_data.get("need_login_subtitle"):
            print("  Bilibili says subtitles need login. Set BILIBILI_COOKIE if needed.")
        else:
            print("  Bilibili video has no CC subtitles.")
        result = transcribe_ytdlp_audio(url, output_dir, "bilibili", bvid, title, args)
        return result or ExtractionResult(
            False, "bilibili", title=title, media_id=bvid, message="no CC subtitles"
        )

    subtitle_url = chosen.get("subtitle_url") or chosen.get("url")
    if subtitle_url and subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    if not subtitle_url:
        return ExtractionResult(False, "bilibili", title=title, media_id=bvid, message="subtitle URL missing")

    print(f"  Bilibili: downloading {chosen.get('lan_doc') or chosen.get('lan')} subtitles")
    try:
        subtitle_json = request_json(subtitle_url, headers)
        body = subtitle_json.get("body") or []
        segments = [
            {"start": float(item.get("from", 0)), "end": float(item.get("to", 0)), "text": item.get("content", "")}
            for item in body
        ]
    except Exception as exc:
        return ExtractionResult(False, "bilibili", title=title, media_id=bvid, message=str(exc))

    return write_segments_bundle(
        output_dir,
        "bilibili",
        bvid,
        title,
        url,
        str(chosen.get("lan") or ""),
        "bilibili-player-api",
        segments,
    )


def parse_next_data(html_text: str) -> dict[str, Any]:
    match = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html_text,
        flags=re.S,
    )
    if not match:
        raise RuntimeError("__NEXT_DATA__ not found")
    return json.loads(match.group(1))


def fetch_xiaoyuzhou_episode(url: str) -> dict[str, Any]:
    html_text = request_text(url, headers={"Referer": XIAO_APP_HOST}, timeout=30)
    data = parse_next_data(html_text)
    episode = data.get("props", {}).get("pageProps", {}).get("episode")
    if not episode:
        raise RuntimeError("episode data not found")
    return episode


def find_transcript_sentences(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, dict):
        sentences = value.get("sentences")
        if isinstance(sentences, list) and sentences:
            return sentences
        data = value.get("data")
        if data is not None:
            found = find_transcript_sentences(data)
            if found:
                return found
        for item in value.values():
            found = find_transcript_sentences(item)
            if found:
                return found
    elif isinstance(value, list):
        if value and all(isinstance(item, dict) and "text" in item for item in value):
            return value
        for item in value:
            found = find_transcript_sentences(item)
            if found:
                return found
    return None


def normalize_xiaoyuzhou_segments(sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for item in sentences:
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        if "startMs" in item:
            start = float(item.get("startMs", 0)) / 1000.0
            end = float(item.get("endMs", item.get("startMs", 0) + 1000)) / 1000.0
        else:
            start = float(item.get("start", item.get("from", 0)))
            end = float(item.get("end", item.get("to", start + 1)))
        segments.append({"start": start, "end": max(end, start + 0.1), "text": text})
    return segments


def xiaoyuzhou_auth_headers(args: argparse.Namespace) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": XIAO_APP_HOST,
            "Referer": XIAO_APP_HOST,
        }
    )
    token = (
        args.xiaoyuzhou_access_token
        or os.getenv("XIAOYUZHOU_ACCESS_TOKEN")
        or os.getenv("X_JIKE_ACCESS_TOKEN")
    )
    if token:
        headers["x-jike-access-token"] = token
    cookie = os.getenv("XIAOYUZHOU_COOKIE")
    if cookie:
        headers["Cookie"] = cookie
    return headers


def try_xiaoyuzhou_official_transcript(
    url: str,
    output_dir: Path,
    episode: dict[str, Any],
    args: argparse.Namespace,
) -> ExtractionResult | None:
    media_id = episode.get("transcriptMediaId") or (episode.get("transcript") or {}).get("mediaId")
    token_present = bool(
        args.xiaoyuzhou_access_token
        or os.getenv("XIAOYUZHOU_ACCESS_TOKEN")
        or os.getenv("X_JIKE_ACCESS_TOKEN")
        or os.getenv("XIAOYUZHOU_COOKIE")
    )
    if not media_id:
        print("  Xiaoyuzhou: this episode does not expose transcriptMediaId")
        return None
    if not token_present:
        print("  Xiaoyuzhou: official transcript needs login; no token/cookie configured")
        return None

    eid = episode.get("eid") or "xiaoyuzhou"
    title = episode.get("title") or eid
    payloads = [
        {"eid": eid, "version": "release"},
        {"eid": eid, "version": "asr"},
        {"eid": eid, "mediaId": media_id, "opts": {"omitSentences": False}},
        {"eid": eid, "mediaId": media_id, "vendor": "asr"},
        {"eid": eid, "mediaId": media_id, "vendor": "ASR"},
    ]
    headers = xiaoyuzhou_auth_headers(args)
    print("  Xiaoyuzhou: trying official transcript API...")
    for payload in payloads:
        try:
            resp = requests.post(
                XIAO_TRANSCRIPT_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=30,
            )
            ctype = resp.headers.get("content-type", "")
            if "application/json" not in ctype:
                continue
            data = resp.json()
            sentences = find_transcript_sentences(data)
            if not sentences:
                continue
            segments = normalize_xiaoyuzhou_segments(sentences)
            if segments:
                return write_segments_bundle(
                    output_dir,
                    "xiaoyuzhou",
                    eid,
                    title,
                    url,
                    "zh",
                    "xiaoyuzhou-official-transcript",
                    segments,
                )
        except Exception as exc:
            print(f"  Xiaoyuzhou transcript attempt failed: {exc}")
    print("  Xiaoyuzhou: official transcript API did not return sentences")
    return None


def extract_xiaoyuzhou(url: str, output_dir: Path, args: argparse.Namespace) -> ExtractionResult:
    print("  Xiaoyuzhou: parsing episode page...")
    try:
        episode = fetch_xiaoyuzhou_episode(url)
    except Exception as exc:
        return ExtractionResult(False, "xiaoyuzhou", message=str(exc))

    eid = episode.get("eid") or "xiaoyuzhou"
    title = episode.get("title") or eid
    print(f"  title: {title}")
    if not args.force:
        existing = existing_bundle_result(output_dir, "xiaoyuzhou", eid, title, url)
        if existing:
            print("  Xiaoyuzhou: existing output found, skipping")
            return existing
    result = try_xiaoyuzhou_official_transcript(url, output_dir, episode, args)
    if result:
        return result

    audio_url = (
        ((episode.get("enclosure") or {}).get("url"))
        or (((episode.get("media") or {}).get("source") or {}).get("url"))
    )
    if not audio_url:
        return ExtractionResult(False, "xiaoyuzhou", title=title, media_id=eid, message="audio URL not found")
    result = transcribe_direct_audio(audio_url, output_dir, "xiaoyuzhou", eid, title, url, args)
    if result:
        return result
    return ExtractionResult(
        False,
        "xiaoyuzhou",
        title=title,
        media_id=eid,
        message="official transcript unavailable and ASR is not configured",
    )


def resolve_executable(path_or_name: str | None) -> str | None:
    if not path_or_name:
        return None
    path = Path(path_or_name)
    if path.exists():
        return str(path)
    found = shutil.which(path_or_name)
    if found:
        return found
    if os.name == "nt" and not path_or_name.endswith(".exe"):
        exe_path = Path(path_or_name + ".exe")
        if exe_path.exists():
            return str(exe_path)
    return None


def asr_ready(args: argparse.Namespace) -> tuple[str, Path] | None:
    if args.no_asr:
        return None
    whisper_bin = resolve_executable(args.whisper_bin)
    model = Path(args.whisper_model)
    if not whisper_bin or not model.exists():
        return None
    return whisper_bin, model


def transcribe_wav(
    wav_path: Path,
    output_dir: Path,
    platform: str,
    media_id: str,
    title: str,
    url: str,
    args: argparse.Namespace,
) -> ExtractionResult | None:
    ready = asr_ready(args)
    if not ready:
        print("  ASR skipped: whisper.cpp binary/model not configured")
        return None
    whisper_bin, model = ready
    base = make_base_path(output_dir, platform, media_id, title or media_id)
    cmd = [
        whisper_bin,
        "-m",
        str(model),
        "-f",
        str(wav_path),
        "-l",
        args.whisper_lang,
        "-otxt",
        "-osrt",
        "-of",
        str(base),
    ]
    print("  ASR: running whisper.cpp...")
    try:
        completed = run_command(cmd, timeout=args.asr_timeout)
    except subprocess.TimeoutExpired:
        print("  ASR timed out")
        return None
    if completed.returncode != 0:
        print(f"  ASR failed: {completed.stderr[-800:]}")
        return None
    txt_path = Path(str(base) + ".txt")
    srt_path = Path(str(base) + ".srt")
    metadata = {
        "platform": platform,
        "media_id": media_id,
        "title": title,
        "url": url,
        "language": args.whisper_lang,
        "source": "whisper.cpp",
        "subtitle_srt": srt_path.name if srt_path.exists() else None,
        "text": txt_path.name if txt_path.exists() else None,
    }
    metadata_path = write_text(
        base.with_suffix(".json"), json.dumps(metadata, ensure_ascii=False, indent=2)
    )
    return ExtractionResult(
        ok=txt_path.exists() or srt_path.exists(),
        platform=platform,
        title=title,
        media_id=media_id,
        lang=args.whisper_lang,
        source="whisper.cpp",
        subtitle_path=str(srt_path) if srt_path.exists() else None,
        text_path=str(txt_path) if txt_path.exists() else None,
        metadata_path=str(metadata_path),
    )


def transcribe_ytdlp_audio(
    url: str,
    output_dir: Path,
    platform: str,
    media_id: str | None,
    title: str,
    args: argparse.Namespace,
) -> ExtractionResult | None:
    if not asr_ready(args):
        print("  ASR fallback unavailable")
        return None
    media_id = media_id or "audio"
    with tempfile.TemporaryDirectory() as tmp:
        out_tpl = str(Path(tmp) / f"{sanitize_filename(platform + '_' + media_id, 80)}.%(ext)s")
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "-x",
            "--audio-format",
            "wav",
            "--postprocessor-args",
            "ffmpeg:-ar 16000 -ac 1",
            "-o",
            out_tpl,
        ]
        if args.cookies:
            cmd += ["--cookies", args.cookies]
        cmd.append(url)
        print("  ASR fallback: downloading audio with yt-dlp...")
        try:
            completed = run_command(cmd, timeout=args.download_timeout)
        except subprocess.TimeoutExpired:
            print("  audio download timed out")
            return None
        if completed.returncode != 0:
            print(f"  audio download failed: {completed.stderr[-800:]}")
            return None
        wavs = list(Path(tmp).glob("*.wav"))
        if not wavs:
            print("  audio download did not produce WAV")
            return None
        return transcribe_wav(wavs[0], output_dir, platform, media_id, title or media_id, url, args)


def transcribe_direct_audio(
    audio_url: str,
    output_dir: Path,
    platform: str,
    media_id: str,
    title: str,
    page_url: str,
    args: argparse.Namespace,
) -> ExtractionResult | None:
    if not asr_ready(args):
        print("  ASR fallback unavailable")
        return None
    ffmpeg = resolve_executable(args.ffmpeg_bin or "ffmpeg")
    if not ffmpeg:
        print("  ASR fallback unavailable: ffmpeg not found")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source_path = tmp_path / f"{sanitize_filename(media_id, 80)}.audio"
        wav_path = tmp_path / f"{sanitize_filename(media_id, 80)}.wav"
        print("  ASR fallback: downloading audio...")
        try:
            with requests.get(audio_url, headers=DEFAULT_HEADERS, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with source_path.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            fh.write(chunk)
        except Exception as exc:
            print(f"  audio download failed: {exc}")
            return None
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
        print("  ASR fallback: converting audio to WAV...")
        try:
            completed = run_command(cmd, timeout=600)
        except subprocess.TimeoutExpired:
            print("  ffmpeg timed out")
            return None
        if completed.returncode != 0 or not wav_path.exists():
            print(f"  ffmpeg failed: {completed.stderr[-800:]}")
            return None
        return transcribe_wav(wav_path, output_dir, platform, media_id, title, page_url, args)


def process_url(url: str, output_dir: Path, args: argparse.Namespace) -> ExtractionResult:
    platform = detect_platform(url)
    print(f"\n{'=' * 72}")
    print(f"URL: {url}")
    print(f"Platform: {platform}")
    if platform == "youtube":
        return extract_youtube(url, output_dir, args)
    if platform == "bilibili":
        return extract_bilibili(url, output_dir, args)
    if platform == "xiaoyuzhou":
        return extract_xiaoyuzhou(url, output_dir, args)
    return ExtractionResult(False, platform, message="unsupported platform")


def load_urls(args: argparse.Namespace) -> list[str]:
    if args.batch:
        path = Path(args.batch)
        if not path.exists():
            raise FileNotFoundError(path)
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    if args.url:
        return [args.url]
    raise ValueError("provide URL or --batch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract subtitles from YouTube, Bilibili and Xiaoyuzhou."
    )
    parser.add_argument("url", nargs="?", help="video or podcast episode URL")
    parser.add_argument("--batch", help="text file with one URL per line")
    parser.add_argument("--output", "-o", default="subtitles", help="output directory")
    parser.add_argument("--lang", default="zh-Hans,zh-CN,zh,en", help="preferred subtitle languages")
    parser.add_argument("--cookies", help="Netscape cookies file for yt-dlp")
    parser.add_argument("--bilibili-cookie", help="raw Bilibili Cookie header")
    parser.add_argument("--xiaoyuzhou-access-token", help="x-jike-access-token value")
    parser.add_argument("--force", action="store_true", help="overwrite existing outputs")
    parser.add_argument("--no-asr", action="store_true", help="disable audio transcription fallback")
    parser.add_argument("--ffmpeg-bin", default=os.getenv("FFMPEG_BIN", "ffmpeg"))
    parser.add_argument("--whisper-bin", default=os.getenv("WHISPER_BIN", "whisper.cpp/build/bin/whisper-cli"))
    parser.add_argument("--whisper-model", default=os.getenv("WHISPER_MODEL", "whisper.cpp/models/ggml-small.bin"))
    parser.add_argument("--whisper-lang", default=os.getenv("WHISPER_LANG", "auto"))
    parser.add_argument("--download-timeout", type=int, default=1800)
    parser.add_argument("--asr-timeout", type=int, default=7200)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        urls = load_urls(args)
    except Exception as exc:
        print(f"URL load failed: {exc}")
        return 2

    output_dir = ensure_output_dir(args.output)
    results: list[ExtractionResult] = []
    for url in urls:
        result = process_url(url, output_dir, args)
        results.append(result)
        if result.ok:
            print(f"  OK: {result.title or result.media_id}")
            print(f"  source: {result.source} lang={result.lang}")
            if result.subtitle_path:
                print(f"  subtitle: {result.subtitle_path}")
            if result.text_path:
                print(f"  text: {result.text_path}")
        else:
            print(f"  FAILED: {result.message}")

    ok = sum(1 for item in results if item.ok)
    print(f"\n{'=' * 72}")
    print(f"Done: {ok}/{len(results)} succeeded")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
