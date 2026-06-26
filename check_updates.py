#!/usr/bin/env python3
"""
小宇宙播客更新检测器
====================
每天轮询播客主页，检测过去 24 小时内发布的新节目。
首次运行会初始化状态文件，后续运行只报告增量更新。

输出: reports/YYYY-MM-DD.md  — 新节目报告
状态: config/last_check.json  — 各播客最后检查时间戳
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
PODCASTS_FILE = BASE_DIR / "config" / "podcasts.txt"
STATE_FILE = BASE_DIR / "config" / "last_check.json"
REPORTS_DIR = BASE_DIR / "reports"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 北京时间 (UTC+8)
TZ_SHANGHAI = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def parse_podcasts(filepath):
    """从 podcasts.txt 解析播客列表，返回 [(name, pid, url), ...]"""
    podcasts = []
    text = Path(filepath).read_text(encoding="utf-8")
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 格式: "名称 | 期数 | URL"
        m = re.match(r"(.+?)\s*\|\s*\d+期\s*\|\s*(https?://[^\s]+)", line)
        if m:
            name = m.group(1).strip()
            url = m.group(2).strip()
            pid = re.search(r"podcast/([a-f0-9]+)", url)
            if pid:
                podcasts.append((name, pid.group(1), url))
    return podcasts


def get_build_id():
    """从首页获取当前的 Next.js build ID"""
    try:
        r = requests.get("https://www.xiaoyuzhoufm.com", headers=HEADERS, timeout=30)
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def fetch_podcast_episodes(pid, build_id):
    """通过 Next.js SSG 端点获取播客节目列表"""
    url = f"https://www.xiaoyuzhoufm.com/_next/data/{build_id}/podcast/{pid}.json?id={pid}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return None, None
        data = r.json()
        pod = data["pageProps"]["podcast"]
        episodes = pod.get("episodes", [])
        return pod, episodes
    except Exception as e:
        print(f"  ✗ 获取失败: {e}")
        return None, None


def parse_date(date_str):
    """解析 ISO 8601 日期字符串为 UTC datetime"""
    if not date_str:
        return None
    try:
        # 处理带时区的格式
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def load_state():
    """加载状态文件"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    """保存状态文件"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fmt_duration(seconds):
    """格式化时长"""
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}时{m}分{s}秒"
    return f"{m}分{s}秒"


# ═══════════════════════════════════════════════════════════════
# 核心逻辑
# ═══════════════════════════════════════════════════════════════

def check_updates(podcasts, build_id):
    """
    检测所有播客的更新。
    返回: {
        "new_episodes": [episode dicts],
        "podcast_names": {pid: name},
        "check_time": ISO string,
        "total_podcasts": int,
        "total_episodes_found": int,
    }
    """
    state = load_state()
    now_utc = datetime.now(timezone.utc)
    check_time = now_utc.isoformat()

    # 24 小时前
    cutoff_utc = now_utc - timedelta(hours=24)

    new_episodes = []
    podcast_names = {}
    total_found = 0

    for name, pid, url in podcasts:
        print(f"\n检查: {name} ({pid})")
        podcast_names[pid] = name

        # 获取该播客上次检查时间
        last_check = state.get(pid)
        if last_check:
            last_check_dt = parse_date(last_check)
            # 使用 max(上次检查, 24小时前) 作为阈值
            since = max(last_check_dt, cutoff_utc) if last_check_dt else cutoff_utc
        else:
            # 首次检查: 只看过去 24 小时
            since = cutoff_utc

        # 获取节目列表
        pod, episodes = fetch_podcast_episodes(pid, build_id)
        if not pod or not episodes:
            print(f"  ⚠ 跳过")
            continue

        # 检测新节目
        found = 0
        for ep in episodes:
            pub_date = parse_date(ep.get("pubDate"))
            if not pub_date:
                continue

            if pub_date > since:
                found += 1
                ep_info = {
                    "pid": pid,
                    "podcast_name": name,
                    "eid": ep.get("eid"),
                    "title": ep.get("title"),
                    "description": (ep.get("description") or "")[:300],
                    "duration": ep.get("duration", 0),
                    "pubDate": ep.get("pubDate"),
                    "playCount": ep.get("playCount", 0),
                    "commentCount": ep.get("commentCount", 0),
                    "url": f"https://www.xiaoyuzhoufm.com/episode/{ep.get('eid')}",
                    "audio_url": (
                        ep.get("enclosure", {}).get("url")
                        or ep.get("media", {}).get("source", {}).get("url", "")
                    ),
                }
                new_episodes.append(ep_info)

        total_found += found
        print(f"  {pod.get('title', '?')}: {pod.get('episodeCount', '?')} 期 | "
              f"新节目: {found} 期")

    # 更新状态
    for pid in podcast_names:
        state[pid] = check_time
    save_state(state)

    return {
        "new_episodes": new_episodes,
        "podcast_names": podcast_names,
        "check_time": check_time,
        "total_podcasts": len(podcasts),
        "total_episodes_found": total_found,
    }


def generate_report(result):
    """生成 Markdown 报告"""
    now = datetime.now(timezone.utc)
    beijing_time = now.astimezone(TZ_SHANGHAI)
    date_str = beijing_time.strftime("%Y-%m-%d")

    lines = []
    lines.append(f"# 小宇宙播客更新报告 — {date_str}")
    lines.append("")
    lines.append(
        f"检查时间: {beijing_time.strftime('%Y-%m-%d %H:%M')} 北京时间 | "
        f"共检查 {result['total_podcasts']} 个播客"
    )
    lines.append("")

    new_eps = result["new_episodes"]
    if not new_eps:
        lines.append("## 无新节目")
        lines.append("")
        lines.append("过去 24 小时内没有检测到新节目更新。")
    else:
        lines.append(f"## 新节目 ({len(new_eps)} 期)")
        lines.append("")

        # 按播客分组
        by_podcast = {}
        for ep in new_eps:
            pid = ep["pid"]
            by_podcast.setdefault(pid, []).append(ep)

        for pid, episodes in by_podcast.items():
            name = result["podcast_names"].get(pid, "未知")
            lines.append(f"### {name}")
            lines.append("")
            for ep in episodes:
                dur = fmt_duration(ep["duration"])
                lines.append(f"**{ep['title']}**")
                lines.append(f"- 链接: {ep['url']}")
                lines.append(f"- 时长: {dur}")
                lines.append(
                    f"- 发布时间: {ep['pubDate'][:19] if ep['pubDate'] else '未知'}"
                )
                if ep["description"]:
                    lines.append(f"- 简介: {ep['description'][:150]}...")
                lines.append("")

    lines.append("---")
    lines.append(f"*由 GitHub Actions 自动生成*")

    report = "\n".join(lines)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{date_str}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n报告已生成: {report_path}")
    return report_path


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("小宇宙播客更新检测器")
    print("=" * 60)

    # 1. 解析播客列表
    podcasts = parse_podcasts(PODCASTS_FILE)
    if not podcasts:
        print("✗ 未找到播客列表，请检查 config/podcasts.txt")
        sys.exit(1)
    print(f"加载 {len(podcasts)} 个播客")

    # 2. 获取 build ID
    print("\n获取 Next.js build ID...")
    build_id = get_build_id()
    if not build_id:
        print("✗ 无法获取 build ID")
        sys.exit(1)
    print(f"Build ID: {build_id}")

    # 3. 检测更新
    result = check_updates(podcasts, build_id)

    # 4. 生成报告
    report_path = generate_report(result)

    # 5. 输出摘要
    print(f"\n{'=' * 60}")
    new_count = len(result["new_episodes"])
    if new_count > 0:
        print(f"🎙 发现 {new_count} 期新节目:")
        for ep in result["new_episodes"]:
            print(f"  [{ep['podcast_name']}] {ep['title']}")
    else:
        print("✓ 过去 24 小时无新节目")

    return 0


if __name__ == "__main__":
    sys.exit(main())