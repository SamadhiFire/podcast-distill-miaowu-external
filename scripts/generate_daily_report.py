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

try:
    from report_contract import build_report, clean_text, nonspace_len, normalize_digest, report_to_markdown
except ModuleNotFoundError:  # Imported as scripts.generate_daily_report in tests/tools.
    from scripts.report_contract import build_report, clean_text, nonspace_len, normalize_digest, report_to_markdown


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
    parser.add_argument(
        "--output-json",
        help="write the validated canonical report JSON (default: output path with .json suffix)",
    )
    parser.add_argument(
        "--llm-max-attempts",
        type=int,
        default=int(os.getenv("LLM_MAX_ATTEMPTS", "3")),
        help="maximum contract/repair attempts for each LLM call",
    )
    parser.add_argument(
        "--require-transcripts",
        action="store_true",
        help="fail before generating or publishing when any non-short item has no transcript",
    )
    parser.add_argument(
        "--llm-policy",
        choices=("extractive", "required"),
        default=os.getenv("LLM_POLICY", "extractive"),
        help="extractive uses a deterministic transcript fallback; required fails without LLM config",
    )
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
        if not isinstance(meta, dict):
            # extraction_results.json / asr_results.json are aggregate arrays,
            # not per-item transcript metadata files.
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


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = (text or "").strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I | re.S).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("response does not contain a JSON object")
    value = json.loads(candidate[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("response JSON must be an object")
    return value


def llm_json(
    messages: list[dict[str, str]],
    validator: Any,
    max_attempts: int,
) -> Any:
    """Call a possibly weak model and repair contract failures deterministically."""
    working = list(messages)
    last_error = "unknown contract error"
    for attempt in range(1, max(1, max_attempts) + 1):
        raw = llm_chat(working, temperature=0.1)
        try:
            return validator(parse_json_object(raw))
        except Exception as exc:
            last_error = str(exc)
            if attempt >= max_attempts:
                break
            working += [
                {"role": "assistant", "content": raw[:12000]},
                {
                    "role": "user",
                    "content": (
                        f"上次输出未通过程序校验：{last_error}\n"
                        "只修复 JSON 结构和字段约束。不要解释，不要使用 Markdown 代码块，不要补充证据中没有的事实。"
                    ),
                },
            ]
    raise RuntimeError(f"LLM output failed validation after {max_attempts} attempt(s): {last_error}")


def build_evidence_map(transcript: str, segment_chars: int = 1400) -> dict[str, str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in (transcript or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines and transcript.strip():
        lines = [transcript.strip()]
    segments: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current and current_len + len(line) + 1 > segment_chars:
            segments.append(" ".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        segments.append(" ".join(current))
    return {f"E{idx:04d}": segment for idx, segment in enumerate(segments, 1)}


def evidence_batches(evidence: dict[str, str], max_chars: int) -> list[dict[str, str]]:
    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_len = 0
    for ref, text in evidence.items():
        addition = len(ref) + len(text) + 6
        if current and current_len + addition > max_chars:
            batches.append(current)
            current = {}
            current_len = 0
        current[ref] = text
        current_len += addition
    if current:
        batches.append(current)
    return batches


def normalize_partial(raw: dict[str, Any], allowed_refs: set[str]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for field in ("people", "facts", "claims", "data", "quotes"):
        values: list[dict[str, Any]] = []
        for entry in raw.get(field, []) if isinstance(raw.get(field), list) else []:
            if not isinstance(entry, dict):
                continue
            text = re.sub(r"\s+", " ", str(entry.get("text", ""))).strip()
            refs = [str(ref) for ref in entry.get("source_refs", []) if str(ref) in allowed_refs]
            if text and refs:
                values.append({"text": text[:180], "source_refs": refs[:3]})
            if len(values) >= 6:
                break
        output[field] = values
    if not any(output.values()):
        raise ValueError("all evidence arrays are empty or have invalid source_refs")
    return output


def validate_final_digest(
    raw: dict[str, Any], item: dict[str, Any], evidence: dict[str, str]
) -> dict[str, Any]:
    valid_refs = set(evidence)
    scalar_limits = {"one_liner": 30, "why_it_matters": 60}
    for field, limit in scalar_limits.items():
        value = raw.get(field)
        if not isinstance(value, dict) or not str(value.get("text", "")).strip():
            raise ValueError(f"{field} must be an object with text and source_refs")
        if nonspace_len(clean_text(value.get("text"))) > limit:
            raise ValueError(f"{field} exceeds {limit} non-space characters")
        if not any(str(ref) in valid_refs for ref in value.get("source_refs", [])):
            raise ValueError(f"{field} must cite a valid source_ref")
    duration = int(item.get("duration") or 0)
    density = clean_text(raw.get("content_density") or "standard").lower()
    if density not in {"brief", "standard", "high"}:
        raise ValueError("content_density must be brief, standard, or high")
    if duration >= 3600 or density == "high":
        list_rules = {"summary": (4, 6, 150), "core_points": (5, 7, 90)}
    elif duration >= 1800 or density == "standard":
        list_rules = {"summary": (3, 5, 150), "core_points": (4, 6, 90)}
    else:
        list_rules = {"summary": (2, 4, 150), "core_points": (3, 5, 90)}
    for field, (minimum, maximum, limit) in list_rules.items():
        values = raw.get(field)
        if not isinstance(values, list) or not minimum <= len(values) <= maximum:
            raise ValueError(f"{field} must contain {minimum}..{maximum} item(s)")
        for value in values:
            if not isinstance(value, dict) or not str(value.get("text", "")).strip():
                raise ValueError(f"every {field} item must contain text")
            if nonspace_len(clean_text(value.get("text"))) > limit:
                raise ValueError(f"a {field} item exceeds {limit} non-space characters")
            if not any(str(ref) in valid_refs for ref in value.get("source_refs", [])):
                raise ValueError(f"every {field} item must cite a valid source_ref")
    takeaways = raw.get("takeaways")
    if not isinstance(takeaways, list) or not 1 <= len(takeaways) <= 2:
        raise ValueError("takeaways must contain one or two reader actions")
    if any("?" in str(value) or "？" in str(value) for value in takeaways):
        raise ValueError("takeaways must be actions, not research questions")
    if any(nonspace_len(clean_text(value)) > 70 for value in takeaways):
        raise ValueError("a takeaway exceeds 70 non-space characters")
    if nonspace_len(clean_text(raw.get("short_title"))) > 18:
        raise ValueError("short_title exceeds 18 non-space characters")
    guests = raw.get("guests")
    if not isinstance(guests, list) or not 1 <= len(guests) <= 5:
        raise ValueError("guests must contain one to five evidence-backed entries")
    for guest in guests:
        if not isinstance(guest, dict) or not clean_text(guest.get("text")):
            raise ValueError("every guest entry must contain text")
        if not any(str(ref) in valid_refs for ref in guest.get("source_refs", [])):
            raise ValueError("every guest entry must cite a valid source_ref")
    tensions = raw.get("tensions", [])
    if not isinstance(tensions, list) or len(tensions) > 3:
        raise ValueError("tensions must be an array with at most three entries")
    for tension in tensions:
        if not isinstance(tension, dict) or not clean_text(tension.get("text")):
            raise ValueError("every tension must contain text")
        if not any(str(ref) in valid_refs for ref in tension.get("source_refs", [])):
            raise ValueError("every tension must cite a valid source_ref")
    digest = normalize_digest(raw, item, evidence=evidence, strict_evidence=True)
    digest["quality"] = "llm_evidence_validated"
    return digest


def extractive_digest(item: dict[str, Any], transcript: str) -> dict[str, Any]:
    sentences = extract_sentences(transcript or item.get("description", ""), limit=9)
    if len(sentences) < 3:
        compact_source = re.sub(r"\s+", " ", transcript or item.get("description", "")).strip()
        for start in range(0, min(len(compact_source), 720), 180):
            fragment = compact_source[start : start + 180].strip()
            if len(fragment) >= 20 and fragment not in sentences:
                sentences.append(fragment)
            if len(sentences) >= 3:
                break
    fallback = sentences or ["已取得完整字幕，但规则模式未能提取可靠语义摘要。"]
    raw = {
        "short_title": short_title(item),
        "one_liner": fallback[0],
        "why_it_matters": fallback[1] if len(fallback) > 1 else fallback[0],
        "summary": fallback[1:3] or fallback[:1],
        "core_points": fallback[:3],
        "key_facts": [],
        "takeaways": ["先用 30 秒结论判断是否值得打开原节目。", "涉及数据或决策时回到原字幕核对上下文。"],
        "guests": [],
        "topics": [item.get("category", "今日更新").split(" /")[0]],
        "quote": None,
        "importance_score": 3,
        "quality": "deterministic_fallback",
    }
    return normalize_digest(raw, item)


def summarize_item_contract(
    item: dict[str, Any], transcript: str, max_attempts: int
) -> dict[str, Any]:
    if not llm_configured():
        return extractive_digest(item, transcript)

    evidence = build_evidence_map(transcript)
    if not evidence:
        raise RuntimeError("transcript is empty after evidence segmentation")
    chunk_size = int(os.getenv("LLM_CHUNK_CHARS", "24000"))
    partials: list[dict[str, list[dict[str, Any]]]] = []
    for batch_index, batch in enumerate(evidence_batches(evidence, chunk_size), 1):
        source = "\n\n".join(f"[{ref}] {text}" for ref, text in batch.items())
        partials.append(
            llm_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是事实提取器，不是文章作者。只输出一个 JSON 对象。"
                            "每条信息必须引用输入中真实存在的 evidence ID；没有证据就不要输出。"
                            "固定字段为 people、facts、claims、data、quotes，值均为数组；"
                            "数组元素格式为 {\"text\":\"...\",\"source_refs\":[\"E0001\"]}。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"节目元数据：{json.dumps(item, ensure_ascii=False)}\n"
                            f"证据批次：{batch_index}\n\n{source}"
                        ),
                    },
                ],
                lambda raw, refs=set(batch): normalize_partial(raw, refs),
                max_attempts,
            )
        )

    schema = {
        "short_title": "18字内中文标题",
        "one_liner": {"text": "30字内完整句", "source_refs": ["E0001"]},
        "why_it_matters": {"text": "46字内，说明与读者的关系", "source_refs": ["E0001"]},
        "content_density": "brief | standard | high；长节目或高密度内容选 high",
        "summary": [{"text": "每段150字内；按时长与密度输出2到6段", "source_refs": ["E0001"]}],
        "core_points": [{"text": "每条90字内；按时长与密度输出3到7条", "source_refs": ["E0001"]}],
        "key_facts": [
            {"label": "14字内", "value": "26字内", "context": "42字内", "source_refs": ["E0001"]}
        ],
        "takeaways": ["读者可执行或可迁移的提示，1到2条，不写研究问题"],
        "guests": [{"text": "人物（机构/角色），1到5条", "source_refs": ["E0001"]}],
        "topics": ["主题词，最多3个"],
        "tensions": [{"text": "对立观点、限制或未决问题，最多3条", "source_refs": ["E0001"]}],
        "quote": None,
        "importance_score": 4,
    }
    return llm_json(
        [
            {
                "role": "system",
                "content": (
                    "你是中文信息早餐编辑，只负责填写 JSON 内容，绝不输出 Markdown 或 XML。"
                    "读者要在30秒内决定是否继续读。所有事实、观点和摘要必须引用证据 ID。"
                    "速览不能牺牲深度：30分钟以上节目至少保留3段摘要和4条观点，60分钟以上或高密度内容至少保留4段摘要和5条观点。"
                    "不要写‘值得后续研究的问题’，takeaways 必须是普通读者能直接采用的看法或行动。"
                    "中英文之间保留空格。金句无法确认逐字原文时 kind 必须写 paraphrase。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"严格按此 JSON 结构返回：\n{json.dumps(schema, ensure_ascii=False)}\n\n"
                    f"节目元数据：{json.dumps(item, ensure_ascii=False)}\n\n"
                    f"已校验的证据提取：{json.dumps(partials, ensure_ascii=False)}"
                ),
            },
        ],
        lambda raw: validate_final_digest(raw, item, evidence),
        max_attempts,
    )


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
        return extractive_item(item, transcript)

    chunk_size = int(os.getenv("LLM_CHUNK_CHARS", "30000"))
    chunks = chunk_text(transcript, chunk_size)
    partials: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        partials.append(
            llm_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是一位专业的中文播客/视频内容研究助理。你的职责是逐段分析节目字幕，"
                            "提取结构化信息，为后续撰写专业日报做准备。\n\n"
                            "【核心原则】\n"
                            "1. 只基于给定字幕文本提取信息，严禁编造、推测或补充字幕中不存在的内容。\n"
                            "2. 保持客观中立，准确反映讲者原意，不加入个人判断。\n"
                            "3. 如果某类信息在当前片段中未出现，明确标注'未提及'。\n\n"
                            "【提取要求】\n"
                            "对每一段字幕，提取以下六类信息：\n"
                            "1. 人物/机构：所有提及的人名、公司名、组织名及职位。格式：人名（机构，职位）\n"
                            "2. 关键事实：具体事件、产品、政策、研究结论、市场动态\n"
                            "3. 核心观点：讲者明确的判断、预测、建议、批评（区分事实与观点）\n"
                            "4. 重要数据：金额、百分比、时间、人数、增长率等（保留原始数字+上下文）\n"
                            "5. 关键金句：有洞察力的原话（30字内，标注讲者）\n"
                            "6. 结构定位：本段在整体讨论中的角色（背景/论证/案例/总结）\n\n"
                            "【输出格式】\n"
                            "用以下固定格式输出，不要省略任何部分：\n\n"
                            "【第N段分析】\n"
                            "- 人物/机构：\n"
                            "- 关键事实：\n"
                            "- 核心观点：\n"
                            "- 重要数据：\n"
                            "- 关键金句：\n"
                            "- 结构定位："
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"这是第 {idx}/{len(chunks)} 段完整字幕。\n\n"
                            "请严格按照系统指令的格式提取关键信息。特别注意：\n"
                            "- 区分'讲者明确说的'和'你的推测'\n"
                            "- 数据必须带单位/上下文（如'营收增长 35%'而非仅'35%'）\n"
                            "- 如果本段没有某类信息，写'未提及'，不要省略该行\n\n"
                            f"元数据：{json.dumps(item, ensure_ascii=False)}\n\n"
                            f"字幕文本：\n{chunk}"
                        ),
                    },
                ]
            )
        )

    partials_text = "\n\n---\n\n".join(partials)
    return llm_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是一位资深中文播客/视频日报编辑。你的任务是根据提取的要点和格式规范，"
                    "生成一篇结构完整、内容专业的日报条目。\n\n"
                    "【核心原则】\n"
                    "1. 严格遵循格式规范中的结构要求，不增删字段。\n"
                    "2. 所有内容必须基于提供的分段摘要，不编造任何信息。\n"
                    "3. 使用中文为主要语言，保留必要的英文专有名词。\n"
                    "4. 不写代码块，不用表格，不用整块引用（除非金句）。\n\n"
                    "【输出要求】\n"
                    "- 从 `## （N）中文短标题` 开始输出\n"
                    "- 不要输出一级标题（如 `# 1. 科技 / AI / VC`）\n"
                    "- 中文短标题控制在 15 字以内\n"
                    "- 一句话摘要必须是完整的一句话，包含'主题+意义'\n"
                    "- 完整摘要 4-8 个段落，按议题组织，不按时间顺序\n"
                    "- 关键内容按分类适配字段，无内容的字段可省略\n"
                    "- 值得后续整理的问题 2-5 个，支持后续知识库工作\n\n"
                    "【禁止使用三级标题】\n"
                    "以下字段标签必须使用 `**粗体**` 格式，严禁使用 `###` 三级标题：\n"
                    "**嘉宾与机构**、**一句话摘要**、**完整摘要**、**核心观点**、**关键内容**、**值得后续整理的问题**\n"
                    "错误示例：`### 嘉宾与机构`\n"
                    "正确示例：`**嘉宾与机构**`\n\n"
                    "【格式强制规则 — 必须严格遵守】\n"
                    "1. 核心观点必须是带编号的列表：`1. 观点一`、`2. 观点二`。严禁裸段落。\n"
                    "2. 关键金句必须用 `> ` 引用块包裹；翻译或重构的表达必须以 `意译：` 开头。\n"
                    "3. 关键数据有 3 个以上数值时，必须用表格：\n"
                    "   | 指标 | 数值 |\n"
                    "   |------|------|\n"
                    "4. 条目末尾必须输出 `---` 分割线。\n"
                    "5. 所有美元符号必须写成 `\\$`（如 `\\$200`）。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请根据以下信息生成单篇日报条目正文。\n\n"
                    "【重要提醒】\n"
                    "1. 严格按照格式规范中的 'Per-Item Required Structure' 输出\n"
                    "2. 参考格式规范最后的 'Complete Example' 了解正确的输出样式\n"
                    "3. 常见错误（必须避免）：跳过字段、用英文标题、按时间顺序写摘要、核心观点用裸段落而非编号列表、用 ### 三级标题代替 **粗体** 字段标签、关键金句不用 `> ` 引用块、忘记 `---` 分割线\n"
                    "4. 不要编造嘉宾、数据或金句；信息缺失时请如实说明\n"
                    "5. 所有 \\$ 符号必须写成 \\\\$（如 \\\\$200），不要输出裸 $ 符号\n\n"
                    f"格式规范：\n{spec}\n\n"
                    f"元数据：{json.dumps(item, ensure_ascii=False)}\n\n"
                    f"分段提取结果：\n\n{partials_text}"
                ),
            },
        ]
    )


def extract_sentences(text: str, limit: int = 10) -> list[str]:
    cleaned = re.sub(r"WEBVTT|\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    parts = re.split(r"(?<=[。！？!?])\s+|(?<=[。！？!?])|(?<=[.!?])\s+(?=[A-Z0-9\u4e00-\u9fff])", cleaned)
    sentences: list[str] = []
    seen: set[str] = set()
    for part in parts:
        sentence = part.strip(" -\t\r\n")
        if len(sentence) < 20 or sentence in seen:
            continue
        seen.add(sentence)
        sentences.append(sentence[:320])
        if len(sentences) >= limit:
            break
    return sentences


def extractive_item(item: dict[str, Any], transcript: str) -> str:
    title = short_title(item)
    source_text = transcript.strip() or (item.get("description") or "").strip()
    sentences = extract_sentences(source_text)
    one_line = sentences[0] if sentences else "未取得可用于摘要的字幕或节目描述。"
    overview = " ".join(sentences[:5]) if sentences else one_line
    core = sentences[:4] or [one_line]
    details = sentences[4:9] or sentences[:3] or [one_line]
    transcript_note = "已获取完整字幕" if item.get("transcript_available") else "基于节目公开描述"
    return f"""## （{{index}}）{title}

**原始标题**：{item.get('original_title') or item.get('title') or ''} ｜ **栏目**：{item.get('source_name', '')} ｜ **平台**：{platform_cn(item.get('platform'))} ｜ **更新**：{fmt_time(item.get('published_at'))} ｜ **时长**：{fmt_duration(item.get('duration'))} ｜ **分类**：{item.get('category', '待分类')} ｜ **推荐**：★★★☆☆
**链接**：{item.get('url', '')}

**嘉宾与机构**

- 规则模式未可靠识别；请结合原始标题、描述与字幕确认。

**一句话摘要**

{one_line}

**完整摘要**

{overview}

**核心观点**

{chr(10).join(f'{idx}. {sentence}' for idx, sentence in enumerate(core, 1))}

**关键内容**

{chr(10).join(f'- **内容 {idx}**：{sentence}' for idx, sentence in enumerate(details, 1))}

**值得后续整理的问题**

- 《{title}》中的核心判断依赖哪些事实和前提？
- 节目观点在不同市场、组织或时间尺度下是否仍然成立？

摘要模式：规则抽取（{transcript_note}）；配置 LLM 后可生成语义级中文摘要。

---
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


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}时{m}分{s}秒"
    return f"{m}分{s}秒"


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
            "本次未配置大语言模型，正文使用字幕或节目描述进行规则抽取，不包含模型推断。"
        )
    brief = json.dumps(
        [{k: item.get(k) for k in ["platform", "category", "source_name", "title", "published_at", "duration"]} for item in items],
        ensure_ascii=False,
    )
    return llm_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是一位中文播客/视频日报主编。根据今日所有条目信息，生成'概览'部分的正文。"
                    "语言简洁专业，用紧凑的段落，不用表格。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请生成日报中 `# 概览` 下的正文，不要输出标题。\n\n"
                    "【内容要求】\n"
                    "必须覆盖以下五方面：\n"
                    "1. 总更新数\n"
                    "2. 平台分布（YouTube / 小宇宙等）\n"
                    "3. 今日主要主题（用 2-3 个关键词概括）\n"
                    "4. 跨来源的模式或分歧（如有）\n"
                    "5. 阅读建议：哪些适合深度阅读、哪些适合归档、哪些可跳过\n\n"
                    "【格式要求】\n"
                    "- 用紧凑的段落，不用表格\n"
                    "- 语言简洁专业\n"
                    "- 控制在 200-400 字\n\n"
                    f"格式规范：\n{spec}\n\n"
                    f"今日条目摘要：\n{brief}"
                ),
            },
        ]
    )


def generate_noteworthy(items: list[dict[str, Any]], item_markdowns: list[str], spec: str) -> str:
    if not items:
        return "今天没有可评选的新增内容。"
    if not llm_configured():
        chosen = items[: min(5, len(items))]
        return "\n".join(
            f"{idx}. **{short_title(item)}**\n   来源：{item.get('source_name', '')}。推荐理由：本窗口内较新的更新，"
            f"{'已取得字幕，可继续深度整理' if item.get('transcript_available') else '已取得公开描述，可继续跟进'}。"
            for idx, item in enumerate(chosen, 1)
        )
    item_markdowns_text = "\n\n---\n\n".join(item_markdowns)
    return llm_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是一位中文播客/视频日报主编。根据条目正文，评选本日最值得关注的内容。"
                    "评选标准明确，不受播放量等早期数据影响。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请生成日报中 `# 本日最值得关注的内容` 下的正文，不要输出标题。\n\n"
                    "【评选标准】（按优先级排序）\n"
                    "1. 信息密度：是否包含具体观点、案例、框架、数字、方法\n"
                    "2. 一手程度：讲者是否为创始人、研究者、高管、投资者、直接参与者\n"
                    "3. 时效/趋势价值：是否解释当前转变、转折点、新模式或持续辩论\n"
                    "4. 可迁移性：能否成为知识库笔记、写作素材、投资视角、产品洞察\n"
                    "5. 稀缺性：是否包含 uncommon 经验、内部视角、深度回顾、跨学科洞察\n\n"
                    "【禁止事项】\n"
                    "- 禁止按播放量、点赞数、评论数排名\n"
                    "- 禁止以'因为是最新发布的'作为唯一推荐理由\n\n"
                    "【输出格式】\n"
                    "每条推荐必须严格使用以下格式：\n"
                    "1. **中文短标题**\n"
                    "   来源：栏目名。推荐理由：1-2句话说明为什么值得关注。\n"
                    "2. **中文短标题**\n"
                    "   来源：栏目名。推荐理由：...\n\n"
                    "每条前面必须有数字编号（1. 2. 3.），严禁裸段落。\n\n"
                    "候选条目正文：\n"
                    f"{item_markdowns_text}"
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
    if args.llm_policy == "required" and not llm_configured():
        print("LLM is required but LLM_BASE_URL and LLM_MODEL are not configured")
        return 2
    items = json.loads(Path(args.items_json).read_text(encoding="utf-8-sig"))
    # Filter out short clips (duration < 5 minutes = 300 seconds)
    original_count = len(items)
    items = [it for it in items if (it.get("duration") or 0) >= 300]
    skipped = original_count - len(items)
    if skipped:
        print(f"Skipped {skipped} short clip(s) (duration < 5min)")
    transcript_index = load_transcript_index(Path(args.subtitles_dir))

    prepared_items: list[tuple[dict[str, Any], str]] = []
    missing_transcripts: list[dict[str, Any]] = []
    for item in items:
        meta = transcript_index.get(normalize_url(item.get("url", "")))
        transcript = ""
        if meta and meta.get("text_path"):
            transcript = read_text(meta["text_path"])
        item["transcript_available"] = bool(transcript)
        if not transcript:
            missing_transcripts.append(item)
        prepared_items.append((item, transcript))

    if args.require_transcripts and missing_transcripts:
        print("Refusing to generate an incomplete report; transcripts are missing for:")
        for item in missing_transcripts:
            print(f"- {item.get('title') or item.get('url')}")
        return 3

    item_digests: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item, transcript in prepared_items:
        try:
            digest = summarize_item_contract(item, transcript or item.get("description", ""), args.llm_max_attempts)
        except Exception as exc:
            print(f"Refusing to publish low-quality model output for {item.get('title')}: {exc}")
            return 4
        item_digests.append((item, digest))

    report = build_report(args.date, item_digests)
    report["generation"] = {
        "mode": "llm_evidence_validated" if llm_configured() else "deterministic_fallback",
        "model": os.getenv("LLM_MODEL", ""),
        "max_contract_attempts": args.llm_max_attempts,
        "transcripts_required": bool(args.require_transcripts),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report_to_markdown(report), encoding="utf-8")
    output_json = Path(args.output_json) if args.output_json else output.with_suffix(".json")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Reader report written: {output}")
    print(f"Validated report JSON written: {output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
