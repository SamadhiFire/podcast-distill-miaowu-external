#!/usr/bin/env python3
"""
YouTube 播客更新检测器
======================
每天轮询 YouTube 频道和播放列表，检测过去 24 小时内发布的新视频。
策略: flat 扫描 ID/时长 → 批量获取日期 → 过滤 → 取详情

输出: reports/youtube-YYYY-MM-DD.md
状态: config/youtube_last_check.json
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 确保实时输出（CI 环境友好）
sys.stdout.reconfigure(line_buffering=True)

BASE_DIR = Path(__file__).parent
SOURCES_FILE = BASE_DIR / "config" / "youtube_sources.txt"
STATE_FILE = BASE_DIR / "config" / "youtube_last_check.json"
REPORTS_DIR = BASE_DIR / "reports"

TZ_SHANGHAI = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════════
# 解析配置
# ═══════════════════════════════════════════════════════════════

def parse_sources(filepath):
    """
    格式: 名称 | URL | 选项
    选项: min_duration=秒
    """
    sources = []
    text = Path(filepath).read_text(encoding="utf-8")
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        name = parts[0]
        url = parts[1]
        min_dur = 0
        if len(parts) >= 3:
            for opt in parts[2].split(","):
                opt = opt.strip()
                if opt.startswith("min_duration="):
                    min_dur = int(opt.split("=")[1])
        # 检测类型
        src_type = "playlist" if "playlist?list=" in url else "channel"
        sources.append({"name": name, "url": url, "min_duration": min_dur, "type": src_type})
    return sources


# ═══════════════════════════════════════════════════════════════
# yt-dlp 封装
# ═══════════════════════════════════════════════════════════════

def ytdlp_flat_scan(url, max_results=50, min_duration=0):
    """
    快速扫描: 获取视频 ID、标题、时长
    返回: [(id, title, duration), ...]
    """
    cmd = ["yt-dlp", "--flat-playlist", "--playlist-end", str(max_results)]
    if min_duration > 0:
        cmd += ["--match-filter", f"duration > {min_duration}"]
    cmd += ["--print", "%(id)s\t%(title)s\t%(duration)s\t%(uploader)s", url]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace"
        )
        results = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                vid = parts[0].strip()
                title = parts[1].strip()
                dur_str = parts[2].strip()
                uploader = parts[3].strip() if len(parts) > 3 else "NA"
                try:
                    dur = float(dur_str) if dur_str and dur_str != "NA" else 0
                except ValueError:
                    dur = 0
                # 跳过无标题的（已删除/私密）
                if title and title != "NA":
                    results.append({"id": vid, "title": title, "duration": int(dur),
                                    "uploader": uploader if uploader != "NA" else ""})
        return results
    except subprocess.TimeoutExpired:
        print(f"    ⚠ 超时")
        return []
    except Exception as e:
        print(f"    ✗ 错误: {e}")
        return []


def ytdlp_get_dates(video_ids):
    """
    批量获取视频上传日期
    返回: {id: upload_date, ...}
    """
    if not video_ids:
        return {}
    urls = [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids]
    cmd = ["yt-dlp", "--skip-download", "--print", "%(id)s\t%(upload_date)s"] + urls
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace"
        )
        dates = {}
        for line in r.stdout.strip().split("\n"):
            if "\t" in line:
                vid, dt = line.split("\t", 1)
                dates[vid.strip()] = dt.strip() if dt.strip() != "NA" else ""
        return dates
    except Exception as e:
        print(f"    ✗ 获取日期失败: {e}")
        return {}


def ytdlp_get_details(video_ids):
    """
    获取视频完整详情
    返回: [{id, title, description, duration, upload_date, view_count, ...}, ...]
    """
    if not video_ids:
        return []
    urls = [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids]
    cmd = ["yt-dlp", "--skip-download", "--dump-json"] + urls
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace"
        )
        details = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                details.append({
                    "id": d.get("id"),
                    "title": d.get("title"),
                    "fulltitle": d.get("fulltitle"),
                    "description": d.get("description", ""),
                    "duration": d.get("duration", 0),
                    "upload_date": d.get("upload_date", ""),
                    "uploader": d.get("uploader", ""),
                    "channel": d.get("channel", ""),
                    "view_count": d.get("view_count", 0),
                    "like_count": d.get("like_count", 0),
                    "comment_count": d.get("comment_count", 0),
                    "webpage_url": d.get("webpage_url"),
                    "subtitles": list(d.get("subtitles", {}).keys()),
                    "automatic_captions": list(d.get("automatic_captions", {}).keys()),
                    "chapters": [
                        {"title": c.get("title", ""), "start_time": c.get("start_time", 0)}
                        for c in d.get("chapters", [])
                    ] if d.get("chapters") else [],
                    "categories": d.get("categories", []),
                    "tags": d.get("tags", []),
                    "thumbnail": d.get("thumbnail", ""),
                })
            except json.JSONDecodeError:
                pass
        return details
    except Exception as e:
        print(f"    ✗ 获取详情失败: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# 核心逻辑
# ═══════════════════════════════════════════════════════════════

def check_source(source, cutoff_date):
    """
    检测单个源的更新。
    cutoff_date: YYYYMMDD 格式的截止日期
    返回: [new video dicts]
    """
    name = source["name"]
    url = source["url"]
    min_dur = source.get("min_duration", 0)
    src_type = source["type"]

    label = f"{'频道' if src_type == 'channel' else '播放列表'}"
    print(f"\n  [{label}] {name}")

    # 步骤 1: flat 扫描
    max_results = 50
    candidates = ytdlp_flat_scan(url, max_results=max_results, min_duration=min_dur)
    if not candidates:
        print(f"    (无结果)")
        return []

    print(f"    扫描 {len(candidates)} 个候选视频")

    # 步骤 2: 批量获取日期
    video_ids = [c["id"] for c in candidates]
    dates = ytdlp_get_dates(video_ids)

    # 步骤 3: 过滤日期
    recent = []
    for c in candidates:
        upload_date = dates.get(c["id"], "")
        if upload_date and upload_date >= cutoff_date:
            recent.append(c)

    if not recent:
        print(f"    无新视频 (cutoff: {cutoff_date})")
        return []

    print(f"    发现 {len(recent)} 个新视频: {[c['title'][:40] for c in recent]}")

    # 步骤 4: 获取详情
    recent_ids = [c["id"] for c in recent]
    details = ytdlp_get_details(recent_ids)

    # 合并
    for d in details:
        d["source_name"] = name
        d["source_url"] = url

    return details


def fmt_duration(seconds):
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}时{m}分{s}秒"
    return f"{m}分{s}秒"


def generate_report(new_videos, sources):
    """生成 Markdown 报告"""
    now = datetime.now(TZ_SHANGHAI)
    date_str = now.strftime("%Y-%m-%d")

    lines = []
    lines.append(f"# YouTube 播客更新报告 — {date_str}")
    lines.append("")
    lines.append(
        f"检查时间: {now.strftime('%Y-%m-%d %H:%M')} 北京时间 | "
        f"共检查 {len(sources)} 个源"
    )
    lines.append("")

    if not new_videos:
        lines.append("## 无新视频")
        lines.append("")
        lines.append("过去 24 小时内没有检测到新视频。")
    else:
        lines.append(f"## 新视频 ({len(new_videos)} 个)")
        lines.append("")

        # 按源分组
        by_source = {}
        for v in new_videos:
            sn = v.get("source_name", "未知")
            by_source.setdefault(sn, []).append(v)

        for sn, videos in by_source.items():
            lines.append(f"### {sn}")
            lines.append("")
            for v in videos:
                dur = fmt_duration(v["duration"])
                lines.append(f"**{v['title']}**")
                lines.append(f"- 链接: {v['webpage_url']}")
                lines.append(f"- 时长: {dur}")
                lines.append(f"- 上传日期: {v.get('upload_date', '未知')}")
                lines.append(f"- 频道: {v.get('uploader', '未知')}")
                lines.append(f"- 播放: {v.get('view_count', 0):,}")
                if v.get("subtitles"):
                    lines.append(f"- 字幕: {', '.join(v['subtitles'])}")
                if v.get("automatic_captions"):
                    lines.append(f"- 自动字幕: {', '.join(v['automatic_captions'][:5])}")
                if v.get("chapters"):
                    ch_str = " > ".join(c["title"] for c in v["chapters"][:5])
                    lines.append(f"- 章节: {ch_str}")
                if v.get("description"):
                    desc = v["description"].replace("\n", " ")[:200]
                    lines.append(f"- 简介: {desc}...")
                lines.append("")

    lines.append("---")
    lines.append(f"*由 GitHub Actions 自动生成*")

    report = "\n".join(lines)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"youtube-{date_str}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n报告已生成: {report_path}")
    return report_path


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("YouTube 播客更新检测器")
    print("=" * 60)

    # 1. 解析源列表
    sources = parse_sources(SOURCES_FILE)
    print(f"加载 {len(sources)} 个源")

    # 2. 计算截止日期 (北京时间昨天)
    cutoff = (datetime.now(TZ_SHANGHAI) - timedelta(days=1)).strftime("%Y%m%d")
    print(f"截止日期: {cutoff} (北京时间)")

    # 3. 逐个检测
    all_new = []
    for src in sources:
        new_vids = check_source(src, cutoff)
        all_new.extend(new_vids)

    # 4. 生成报告
    report_path = generate_report(all_new, sources)

    # 5. 摘要
    print(f"\n{'=' * 60}")
    if all_new:
        print(f"新视频: {len(all_new)} 个")
        for v in all_new:
            print(f"  [{v['source_name']}] {v['title']}")
    else:
        print("无新视频")

    return 0


if __name__ == "__main__":
    sys.exit(main())