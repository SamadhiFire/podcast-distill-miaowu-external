#!/usr/bin/env python3
"""Generate a daily Markdown report from full transcript files."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests


BASE_DIR = Path(__file__).resolve().parents[1]
SPEC_PATH = BASE_DIR / "templates" / "daily_report_llm_spec.md"
REPORTS_DIR = BASE_DIR / "reports"
CATEGORIES = [
    "科技 / AI / VC",
    "商业 / 财经 / 投资",
    "产品 / 创业 / 管理",
    "新闻 / 时评 / 全球议题",
    "文化 / 社会 / 人文",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--items-json", required=True)
    parser.add_argument("--subtitles-dir", default="subtitles")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def normalize_url(url: str) -> str:
    parsed = urlparse(url or "")
    if "youtube.com" in parsed.netloc and parsed.path == "/watch":
        qs = parse_qs(parsed.query)
        if "v" in qs:
            return "https://www.youtube.com/watch?" + urlencode({"v": qs["v"][0]})
    if "youtube.com" in parsed.netloc and parsed.path == "/playlist":
        qs = parse_qs(parsed.query)
        if "list" in qs:
            return "https://www.youtube.com/playlist?" + urlencode({"list": qs["list"][0]})
    return (url or "").split("?s=")[0].rstrip("/")


def load_transcript_index(subtitles_dir: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for meta_path in subtitles_dir.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        url = normalize_url(meta.get("url", ""))
        text_name = meta.get("text")
        if not text_name:
            continue
        text_path = meta_path.with_name(text_name)
        if not text_path.exists():
            continue
        meta["text_path"] = str(text_path)
        index[url] = meta
    return index


def read_text(path: str | Path, max_chars: int | None = None) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return text if max_chars is None else text[:max_chars]


def llm_configured() -> bool:
    return bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"))


def llm_chat(messages: list[dict[str, str]], temperature: float = 0.2) -> str:
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    model = os.environ["LLM_MODEL"]
    api_key = os.getenv("LLM_API_KEY", "")
    endpoint = base_url
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = requests.post(
        endpoint,
        headers=headers,
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=int(os.getenv("LLM_TIMEOUT", "180")),
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def chunk_text(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        chunks.append(text[start:end])
        start = end
    return chunks


def summarize_item(item: dict[str, Any], transcript: str, spec: str) -> str:
    if not llm_configured():
        return scaffold_item(item)

    chunk_size = int(os.getenv("LLM_CHUNK_CHARS", "30000"))
    chunks = chunk_text(transcript, chunk_size)
    partials: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        partials.append(
            llm_chat(
                [
                    {"role": "system", "content": "你是严谨的中文播客/视频研究助理。只基于给定字幕总结，不编造。"},
                    {
                        "role": "user",
                        "content": (
                            f"这是第 {idx}/{len(chunks)} 段完整字幕的一部分。"
                            "请提取事实、人物、机构、关键观点、数据和金句，保留后续写日报所需信息。\n\n"
                            f"元数据：{json.dumps(item, ensure_ascii=False)}\n\n字幕：\n{chunk}"
                        ),
                    },
                ]
            )
        )

    return llm_chat(
        [
            {"role": "system", "content": "你是严谨的中文播客/视频日报编辑。必须严格遵守格式规范。"},
            {
                "role": "user",
                "content": (
                    "根据格式规范和分段摘要，生成单篇条目正文。"
                    "从 `## （N）中文短标题` 开始输出，不要输出一级标题。\n\n"
                    f"格式规范：\n{spec}\n\n"
                    f"元数据：{json.dumps(item, ensure_ascii=False)}\n\n"
                    f"分段摘要：\n\n" + "\n\n---\n\n".join(partials)
                ),
            },
        ]
    )


def scaffold_item(item: dict[str, Any]) -> str:
    title = short_title(item)
    return f"""## （{{index}}）{title}

**原始标题**：{item.get('original_title') or item.get('title') or ''} ｜ **栏目**：{item.get('source_name', '')} ｜ **平台**：{platform_cn(item.get('platform'))} ｜ **更新**：{fmt_time(item.get('published_at'))} ｜ **分类**：{item.get('category', '待分类')} ｜ **推荐**：待评估
**链接**：{item.get('url', '')}

### 嘉宾与机构

- 待生成：需要配置 LLM_BASE_URL 和 LLM_MODEL 后基于完整字幕识别。

### 一句话摘要

待生成：已保留完整字幕输入位置。

### 完整摘要

待生成：需要配置大语言模型后基于完整字幕生成。

### 核心观点

1. 待生成。

### 关键内容

- **关键内容**：待生成。

### 值得后续整理的问题

- 待生成。
"""


def platform_cn(value: str | None) -> str:
    if value == "xiaoyuzhou":
        return "小宇宙"
    if value == "youtube":
        return "YouTube"
    return value or ""


def fmt_time(value: str | None) -> str:
    if not value:
        return "未知"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def short_title(item: dict[str, Any]) -> str:
    title = item.get("title") or item.get("original_title") or "未命名内容"
    title = re.sub(r"\s+", " ", title).strip()
    return title[:60]


def generate_overview(items: list[dict[str, Any]], spec: str) -> str:
    if not items:
        return "今天未检测到新增播客或视频。"
    if not llm_configured():
        by_platform: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for item in items:
            by_platform[platform_cn(item.get("platform"))] = by_platform.get(platform_cn(item.get("platform")), 0) + 1
            by_category[item.get("category", "待分类")] = by_category.get(item.get("category", "待分类"), 0) + 1
        return (
            f"今天共检测到 {len(items)} 条更新。"
            f"平台分布：{format_counts(by_platform)}。"
            f"分类分布：{format_counts(by_category)}。"
            "由于尚未配置大语言模型，本日报已生成结构化占位内容；配置 LLM 后会基于完整字幕生成概览和摘要。"
        )
    brief = json.dumps(
        [{k: item.get(k) for k in ["platform", "category", "source_name", "title", "published_at", "duration"]} for item in items],
        ensure_ascii=False,
    )
    return llm_chat(
        [
            {"role": "system", "content": "你是中文播客/视频日报编辑。"},
            {"role": "user", "content": f"按规范生成 `# 概览` 下的正文，不要输出标题。\n\n规范：{spec}\n\n今日条目：{brief}"},
        ]
    )


def generate_noteworthy(items: list[dict[str, Any]], item_markdowns: list[str], spec: str) -> str:
    if not items:
        return "今天没有可评选的新增内容。"
    if not llm_configured():
        chosen = items[: min(5, len(items))]
        return "\n".join(
            f"{idx}. **{short_title(item)}**\n   来源：{item.get('source_name', '')}。推荐理由：待配置 LLM 后基于完整字幕评估。"
            for idx, item in enumerate(chosen, 1)
        )
    return llm_chat(
        [
            {"role": "system", "content": "你是中文播客/视频日报编辑。"},
            {
                "role": "user",
                "content": (
                    "根据规范选择本日最值得关注的内容，输出 `# 本日最值得关注的内容` 下的正文，不要输出标题。\n\n"
                    f"规范：{spec}\n\n候选条目正文：\n" + "\n\n---\n\n".join(item_markdowns)
                ),
            },
        ]
    )


def format_counts(counts: dict[str, int]) -> str:
    return "，".join(f"{key} {value} 条" for key, value in counts.items())


def replace_index(markdown: str, index: int) -> str:
    return markdown.replace("## （{index}）", f"## （{index}）", 1)


def main() -> int:
    args = parse_args()
    spec = SPEC_PATH.read_text(encoding="utf-8")
    items = json.loads(Path(args.items_json).read_text(encoding="utf-8-sig"))
    transcript_index = load_transcript_index(Path(args.subtitles_dir))

    item_markdowns: list[tuple[dict[str, Any], str]] = []
    for item in items:
        meta = transcript_index.get(normalize_url(item.get("url", "")))
        transcript = ""
        if meta and meta.get("text_path"):
            transcript = read_text(meta["text_path"])
        item["transcript_available"] = bool(transcript)
        item_markdown = summarize_item(item, transcript or item.get("description", ""), spec)
        item_markdowns.append((item, item_markdown))

    lines = [f"# {args.date} 播客/视频更新日报", ""]
    lines += ["# 概览", "", generate_overview(items, spec), ""]
    lines += [
        "# 本日最值得关注的内容",
        "",
        generate_noteworthy(items, [md for _, md in item_markdowns], spec),
        "",
    ]

    by_category: dict[str, list[tuple[dict[str, Any], str]]] = {cat: [] for cat in CATEGORIES}
    for item, md in item_markdowns:
        by_category.setdefault(item.get("category", "待分类"), []).append((item, md))

    for cat_idx, category in enumerate(CATEGORIES, 1):
        lines += [f"# {cat_idx}. {category}", ""]
        entries = by_category.get(category, [])
        if not entries:
            lines += ["今日无新增。", ""]
            continue
        for idx, (_, md) in enumerate(entries, 1):
            lines.append(replace_index(md, idx).strip())
            lines.append("")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Daily report written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
