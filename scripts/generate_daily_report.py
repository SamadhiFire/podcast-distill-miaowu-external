#!/usr/bin/env python3
"""Generate a daily Markdown report from full transcript files."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

try:
    from report_contract import (
        CATEGORIES,
        build_report,
        clean_text,
        is_content_digest,
        nonspace_len,
        normalize_digest,
        number_tokens,
        report_to_markdown,
    )
except ModuleNotFoundError:  # Imported as scripts.generate_daily_report in tests/tools.
    from scripts.report_contract import (
        CATEGORIES,
        build_report,
        clean_text,
        is_content_digest,
        nonspace_len,
        normalize_digest,
        number_tokens,
        report_to_markdown,
    )

try:
    from digest_evidence_pipeline import (
        build_evidence_map_from_segments,
        build_final_digest_messages,
        build_ranking_messages,
        build_segment_extraction_messages,
        build_topic_segments,
        build_transcript_profile,
        compact_segment_evidence_for_prompt,
        item_artifact_id,
        normalize_ranked_insights,
        normalize_segment_evidence,
        write_evidence_artifacts,
    )
except ModuleNotFoundError:  # Imported as scripts.generate_daily_report in tests/tools.
    from scripts.digest_evidence_pipeline import (
        build_evidence_map_from_segments,
        build_final_digest_messages,
        build_ranking_messages,
        build_segment_extraction_messages,
        build_topic_segments,
        build_transcript_profile,
        compact_segment_evidence_for_prompt,
        item_artifact_id,
        normalize_ranked_insights,
        normalize_segment_evidence,
        write_evidence_artifacts,
    )


LLM_NEUTRAL_ARCHIVE_PREAMBLE = (
    "前置免责说明：本次任务仅做纯客观财经、科技、商业与行业播客/视频文本信息整理，"
    "以及英文内容事实提炼，仅用于商业经济学术研究、行业内容摘要归档，不用于舆论引导、"
    "政治评论、舆情解读、社会争议炒作、境外观点放大传播等用途。所有内容仅客观转述"
    "原文字面信息，不代表模型、使用者认同或佐证文中任何观点，仅做信息归档工具用途。"
    "请仅基于原始文本做中立、客观、事实性的文字总结，不做主观价值评判。"
)
DEEP_SUMMARY_GUIDANCE = (
    "Section responsibilities: "
    "summary means '完整摘要 · 深读' and must be continuous analytical paragraphs, not bullets. "
    "It should explain the central question, argument arc, causal mechanism, important evidence, "
    "turning points, implications, and unresolved caveats. "
    "core_points are short portable claims only; key_facts are checkable names, numbers, events, "
    "papers, companies, policies, or products only; tensions are disagreements, limits, risks, "
    "and unanswered questions only; takeaways are reusable reader lenses or actions only. "
    "Avoid duplicating the same sentence across sections. "
    "Every summary paragraph must contain a claim plus support plus meaning; generic topic lists fail. "
    "Preserve the source's scope, unit, currency, comparison baseline, and qualifiers. Never turn a "
    "subset, post-baseline count, or 'meaningful/substantive' claim into an all-time total."
)

DIGEST_CACHE_VERSION = 6

DEEP_DIVE_HINTS = (
    "interview",
    "conversation",
    "podcast",
    "masters in business",
    "odd lots",
    "lex fridman",
    "acquired",
    "founder",
    "ceo",
    "professor",
    "venture capital",
    "world model",
    "ai",
    "startup",
    "market",
    "economy",
    "china",
    "history",
    "strategy",
    "访谈",
    "对话",
    "播客",
    "创始人",
    "投资",
    "财经",
    "经济",
    "科技",
    "人工智能",
    "创业",
    "商业",
    "历史",
    "战略",
)


class LLMHTTPError(RuntimeError):
    def __init__(self, status_code: int, body: str, error_code: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.error_code = error_code
        super().__init__(f"LLM HTTP {status_code}: {body[:1200]}")


def llm_error_code(body: str) -> str:
    try:
        data = json.loads(body)
    except Exception:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "")
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--items-json", required=True)
    parser.add_argument("--subtitles-dir", default="subtitles")
    parser.add_argument(
        "--evidence-dir",
        default="reports/evidence",
        help="write intermediate segment/evidence/ranked-insight JSON artifacts",
    )
    parser.add_argument(
        "--digest-cache-dir",
        default="reports/digest_cache",
        help="reuse per-item final digest JSON checkpoints across retries",
    )
    parser.add_argument(
        "--fileid-cache-dir",
        default="reports/qwen_file_cache",
        help="cache DashScope qwen-long uploaded file ids by transcript hash",
    )
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
        default=os.getenv("LLM_POLICY", "required"),
        help="required fails without LLM config; extractive is kept only as a legacy alias and no longer emits rule summaries",
    )
    return parser.parse_args()


def normalize_url(url: str) -> str:
    parsed = urlparse(url or "")
    host = parsed.netloc.lower()
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]
        if video_id:
            return "https://www.youtube.com/watch?" + urlencode({"v": video_id})
    if "youtube.com" in host and parsed.path == "/watch":
        qs = parse_qs(parsed.query)
        if "v" in qs:
            return "https://www.youtube.com/watch?" + urlencode({"v": qs["v"][0]})
    shorts = re.fullmatch(r"/shorts/([^/]+)", parsed.path)
    if "youtube.com" in host and shorts:
        return "https://www.youtube.com/watch?" + urlencode({"v": shorts.group(1)})
    if "youtube.com" in host and parsed.path == "/playlist":
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
        meta["meta_path"] = str(meta_path)
        for key in ("subtitle_vtt", "subtitle_srt", "vtt", "srt", "subtitle"):
            declared = meta.get(key)
            if isinstance(declared, str) and declared.strip():
                candidate = meta_path.with_name(Path(declared).name)
                if candidate.exists():
                    meta["subtitle_path"] = str(candidate)
                    break
        index[url] = meta
    return index


def read_text(path: str | Path, max_chars: int | None = None) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return text if max_chars is None else text[:max_chars]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def llm_configured() -> bool:
    return bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"))


def llm_api_base() -> str:
    workspace_id = os.getenv("LLM_WORKSPACE_ID") or os.getenv("QWEN_WORKSPACE_ID")
    if workspace_id and os.getenv("LLM_USE_WORKSPACE_BASE", "0") == "1":
        return f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    return os.environ["LLM_BASE_URL"].rstrip("/")


def llm_endpoint(path: str) -> str:
    endpoint = llm_api_base().rstrip("/")
    if endpoint.endswith("/chat/completions"):
        endpoint = endpoint[: -len("/chat/completions")]
    return endpoint.rstrip("/") + path


def llm_chat(messages: list[dict[str, str]], temperature: float = 0.2) -> str:
    model = os.environ["LLM_MODEL"]
    api_key = os.getenv("LLM_API_KEY", "")
    endpoint = llm_endpoint("/chat/completions")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    effective_messages = list(messages)
    if os.getenv("LLM_NEUTRAL_ARCHIVE_PREAMBLE", "1") != "0":
        has_fileid = any(
            message.get("role") == "system"
            and str(message.get("content", "")).strip().startswith("fileid://")
            for message in effective_messages
        )
        if has_fileid and effective_messages and effective_messages[0].get("role") == "system":
            effective_messages[0] = {
                **effective_messages[0],
                "content": LLM_NEUTRAL_ARCHIVE_PREAMBLE + "\n\n" + str(effective_messages[0].get("content", "")),
            }
        else:
            effective_messages = [
                {"role": "system", "content": LLM_NEUTRAL_ARCHIVE_PREAMBLE},
                *effective_messages,
            ]
    retry_attempts = max(1, int(os.getenv("LLM_RETRY_ATTEMPTS", "4")))
    retry_base = float(os.getenv("LLM_RETRY_BASE_SECONDS", "2"))
    retry_max = float(os.getenv("LLM_RETRY_MAX_SECONDS", "30"))
    retry_statuses = {408, 409, 429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(1, retry_attempts + 1):
        try:
            resp = requests.post(
                endpoint,
                headers=headers,
                json={"model": model, "messages": effective_messages, "temperature": temperature},
                timeout=int(os.getenv("LLM_TIMEOUT", "180")),
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            retryable = True
        else:
            if resp.ok:
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            body = resp.text[:4000]
            error_code = llm_error_code(body)
            error = LLMHTTPError(resp.status_code, body, error_code)
            last_error = error
            retryable = resp.status_code in retry_statuses
            if not retryable:
                raise error
        if attempt >= retry_attempts or not retryable:
            break
        delay = min(retry_max, retry_base * (2 ** (attempt - 1)))
        print(f"LLM request failed on attempt {attempt}/{retry_attempts}; retrying in {delay:.1f}s: {last_error}", flush=True)
        time.sleep(delay)
    if last_error:
        raise last_error
    raise RuntimeError("LLM request failed without a response")


def sha1_text(value: str) -> str:
    import hashlib

    return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()


def qwen_file_cache_path(cache_dir: Path | None, transcript: str) -> Path | None:
    if cache_dir is None:
        return None
    return cache_dir / f"{sha1_text(transcript)}.json"


def wait_qwen_file_ready(file_id: str) -> None:
    if os.getenv("LLM_FILE_WAIT_READY", "1") == "0":
        return
    api_key = os.getenv("LLM_API_KEY", "")
    endpoint = llm_endpoint(f"/files/{file_id}")
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout_seconds = int(os.getenv("LLM_FILE_READY_TIMEOUT", "300"))
    deadline = time.time() + timeout_seconds
    ready_statuses = {"processed", "success", "succeeded", "ready", "available", "uploaded"}
    pending_statuses = {"pending", "processing", "running", "created", "in_progress"}
    while time.time() < deadline:
        try:
            resp = requests.get(endpoint, headers=headers, timeout=30)
        except requests.RequestException:
            time.sleep(2)
            continue
        if resp.status_code in {404, 405}:
            return
        if not resp.ok:
            return
        try:
            data = resp.json()
        except ValueError:
            return
        status = str(data.get("status") or data.get("file_status") or "").lower()
        if not status or status in ready_statuses:
            return
        if status not in pending_statuses:
            return
        time.sleep(3)


def upload_qwen_file(
    transcript: str,
    item: dict[str, Any],
    cache_dir: Path | None,
) -> str:
    if os.getenv("LLM_FILEID_ENABLED", "1") == "0":
        raise RuntimeError("LLM fileid mode is disabled")
    api_key = os.getenv("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_API_KEY is required for qwen-long file upload")

    cache_path = qwen_file_cache_path(cache_dir, transcript)
    if cache_path is not None and cache_path.exists():
        try:
            cached = read_json(cache_path)
            if (
                isinstance(cached, dict)
                and cached.get("base_url") == llm_api_base().rstrip("/")
                and cached.get("file_id")
            ):
                return str(cached["file_id"])
        except Exception:
            pass

    upload_endpoint = llm_endpoint("/files")
    headers = {"Authorization": f"Bearer {api_key}"}
    filename = re.sub(r"[^A-Za-z0-9_-]+", "_", str(item.get("title") or item.get("url") or "transcript"))
    filename = (filename[:80] or "transcript") + ".txt"
    retry_attempts = max(1, int(os.getenv("LLM_FILE_UPLOAD_RETRY_ATTEMPTS", os.getenv("LLM_RETRY_ATTEMPTS", "4"))))
    last_error: Exception | None = None
    for attempt in range(1, retry_attempts + 1):
        try:
            files = {"file": (filename, transcript.encode("utf-8"), "text/plain")}
            resp = requests.post(
                upload_endpoint,
                headers=headers,
                data={"purpose": "file-extract"},
                files=files,
                timeout=int(os.getenv("LLM_FILE_UPLOAD_TIMEOUT", "600")),
            )
            if resp.ok:
                data = resp.json()
                file_id = str(data.get("id") or data.get("file_id") or "")
                if not file_id:
                    raise RuntimeError(f"file upload response has no file id: {data}")
                wait_qwen_file_ready(file_id)
                if cache_path is not None:
                    write_json(
                        cache_path,
                        {
                            "file_id": file_id,
                            "base_url": llm_api_base().rstrip("/"),
                            "model": os.getenv("LLM_MODEL", ""),
                            "sha1": sha1_text(transcript),
                            "bytes": len(transcript.encode("utf-8", errors="replace")),
                            "title": item.get("title") or item.get("original_title") or "",
                        },
                    )
                return file_id
            error = LLMHTTPError(resp.status_code, resp.text[:4000], llm_error_code(resp.text[:4000]))
            last_error = error
            if resp.status_code not in {408, 409, 429, 500, 502, 503, 504}:
                raise error
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
        if attempt < retry_attempts:
            delay = min(30.0, 2.0 * (2 ** (attempt - 1)))
            print(f"Qwen file upload failed on attempt {attempt}/{retry_attempts}; retrying in {delay:.1f}s: {last_error}", flush=True)
            time.sleep(delay)
    if last_error:
        raise last_error
    raise RuntimeError("Qwen file upload failed without a response")


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
    """Call the model and ask it to repair contract failures with fixed prompts."""
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
            working.append(
                {
                    "role": "user",
                    "content": (
                        "Repair only the JSON object so it passes validation. "
                        "Keep the same facts and evidence refs. Do not add Markdown. "
                        "Every summary/core_points/guests/tensions item must be an object "
                        "with text and source_refs. Respect the required item counts exactly."
                    ),
                }
            )
    raise RuntimeError(f"LLM output failed validation after {max_attempts} attempt(s): {last_error}")


REPORT_THEME_BLOCKLIST = (
    "摘要受限",
    "模型安全审核",
    "摘要格式校验",
    "未生成摘要",
)


def report_theme_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the evidence-bounded input for a report-level theme synthesis."""
    candidates: list[dict[str, Any]] = []
    for item_index, item in enumerate(items):
        if not is_content_digest(item):
            continue
        one_liner = clean_text(item.get("one_liner", ""))
        summary = item.get("summary", [])
        summary_text = clean_text(summary[0] if isinstance(summary, list) and summary else summary)
        if not one_liner and not summary_text:
            continue
        candidates.append(
            {
                "item_id": f"I{item_index + 1}",
                "title": clean_text(item.get("short_title") or item.get("original_title")),
                "one_liner": one_liner[:180],
                "summary": summary_text[:360],
            }
        )
    return candidates


def build_report_theme_messages(candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    payload = json.dumps(candidates, ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": (
                "你是中文日报编辑。请基于给定的条目摘要，提炼本期 2 到 3 条真实的信息主线。"
                "这不是栏目分类，也不是关键词罗列；每条应概括一项具体的事实、机制、变化或争议，"
                "并且只能使用所给条目中已有的信息。禁止写‘摘要受限’、模型状态、平台状态、"
                "或‘商业/财经/投资’这类栏目名。"
                "只输出 JSON，不要 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                "候选条目：\n"
                f"{payload}\n\n"
                "返回：{\"themes\":[{\"title\":\"8-28 字的具体中文主线\","
                "\"source_item_ids\":[\"I1\",\"I2\"]}]}。"
                "每条必须关联 1-3 个有效条目 ID；不要添加候选条目没有支持的事实。"
            ),
        },
    ]


def validate_report_themes(raw: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = raw.get("themes")
    if not isinstance(values, list) or not 2 <= len(values) <= 3:
        raise ValueError("themes must contain 2..3 entries")
    valid_ids = {str(item["item_id"]) for item in candidates}
    result: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("every theme must be an object")
        title = clean_text(value.get("title", ""))
        if not 8 <= nonspace_len(title) <= 28:
            raise ValueError("theme title must be 8..28 non-space characters")
        if title in CATEGORIES or any(blocked in title for blocked in REPORT_THEME_BLOCKLIST):
            raise ValueError("theme title is a category or internal placeholder")
        if title in seen_titles:
            raise ValueError("theme titles must be unique")
        source_ids = value.get("source_item_ids")
        if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 3:
            raise ValueError("every theme needs 1..3 source item IDs")
        normalized_ids = [str(source_id) for source_id in source_ids]
        if len(set(normalized_ids)) != len(normalized_ids) or any(
            source_id not in valid_ids for source_id in normalized_ids
        ):
            raise ValueError("theme source_item_ids must reference valid normal digests")
        seen_titles.add(title)
        result.append({"title": title, "source_item_ids": normalized_ids})
    return result


def generate_report_themes(items: list[dict[str, Any]], max_attempts: int) -> list[dict[str, Any]]:
    """Create evidence-linked daily themes, or suppress the map on failure."""
    candidates = report_theme_candidates(items)
    if len(candidates) < 2 or not llm_configured():
        return []
    return llm_json(
        build_report_theme_messages(candidates),
        lambda raw: validate_report_themes(raw, candidates),
        max_attempts=min(2, max_attempts),
    )


def digest_contract_for_item(
    item: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = profile or {}
    duration = int(item.get("duration") or item.get("duration_seconds") or 0)
    text_chars = int(profile.get("text_chars") or 0)
    metadata_text = " ".join(
        str(item.get(key) or "")
        for key in ("title", "original_title", "source_name", "category", "description", "platform")
    ).lower()
    density_score = 0
    if duration >= 1800 or text_chars >= 80000:
        density_score += 2
    elif duration >= 900 or text_chars >= 30000:
        density_score += 1
    if item.get("category") in {"科技 / AI / VC", "商业 / 财经 / 投资", "产品 / 创业 / 管理"}:
        density_score += 1
    if any(hint in metadata_text for hint in DEEP_DIVE_HINTS):
        density_score += 2
    if re.search(r"\b(ai|vc|ipo|fed|gdp|llm|gpu|saas|m&a)\b", metadata_text):
        density_score += 1

    if density_score >= 4:
        return {
            "content_density": "high",
            "summary_min": 6,
            "summary_max": 9,
            "summary_char_limit": 420,
            "core_points_min": 5,
            "core_points_max": 8,
        }
    if density_score >= 2:
        return {
            "content_density": "standard",
            "summary_min": 4,
            "summary_max": 6,
            "summary_char_limit": 340,
            "core_points_min": 4,
            "core_points_max": 6,
        }
    return {
        "content_density": "brief",
        "summary_min": 3,
        "summary_max": 5,
        "summary_char_limit": 280,
        "core_points_min": 3,
        "core_points_max": 5,
    }


def _valid_refs(raw: Any, valid_refs: set[str]) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(ref) for ref in raw if str(ref) in valid_refs]


def _coerce_cited_text_entry(value: Any, valid_refs: set[str]) -> dict[str, Any]:
    if isinstance(value, str):
        return {"text": value, "source_refs": []}
    if not isinstance(value, dict):
        return {"text": "", "source_refs": []}
    text = (
        value.get("text")
        or value.get("claim")
        or value.get("content")
        or value.get("point")
        or value.get("summary")
        or value.get("value")
        or ""
    )
    refs = (
        value.get("source_refs")
        or value.get("source_ref")
        or value.get("evidence_refs")
        or value.get("refs")
        or value.get("ref")
        or []
    )
    return {"text": text, "source_refs": _valid_refs(refs, valid_refs)}


def coerce_final_digest_shape(raw: dict[str, Any], evidence: dict[str, str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return raw
    valid_refs = set(evidence)
    output = dict(raw)
    for field in ("one_liner", "why_it_matters"):
        output[field] = _coerce_cited_text_entry(output.get(field), valid_refs)
    for field in ("summary", "core_points", "guests", "tensions"):
        values = output.get(field)
        if isinstance(values, dict):
            values = values.get("items") or values.get("points") or values.get("paragraphs") or []
        if not isinstance(values, list):
            values = []
        output[field] = [_coerce_cited_text_entry(value, valid_refs) for value in values]
    quote = output.get("quote")
    if isinstance(quote, dict):
        refs = quote.get("source_refs") or quote.get("source_ref") or quote.get("refs") or []
        output["quote"] = {**quote, "source_refs": _valid_refs(refs, valid_refs)}
    return output


def is_data_inspection_error(exc: Exception) -> bool:
    text = str(exc)
    return "data_inspection_failed" in text or "inappropriate content" in text


def is_context_length_error(exc: Exception) -> bool:
    text = str(exc).lower()
    explicit_context_error = (
        "exceeds token limit" in text
        or "token limit" in text
        or "context length" in text
        or "maximum context" in text
    )
    invalid_parameter_length_error = (
        "invalid_parameter_error" in text
        and ("input" in text or "context" in text)
        and ("length" in text or "token" in text)
    )
    return explicit_context_error or invalid_parameter_length_error


def metadata_fallback_enabled() -> bool:
    return os.getenv("LLM_METADATA_FALLBACK_ENABLED", "0").lower() in {
        "1",
        "true",
        "yes",
    }


class DirectFileIdContractError(RuntimeError):
    """The model replied, but could not satisfy the direct file-id contract."""


def direct_fileid_required() -> bool:
    return os.getenv("LLM_FILEID_DIRECT_REQUIRED", "0").lower() in {
        "1",
        "true",
        "yes",
    }


def evidence_fallback_enabled() -> bool:
    """Keep segmented evidence as an explicit opt-in when direct file-id is required."""
    configured = os.getenv("LLM_EVIDENCE_FALLBACK_ENABLED")
    if configured is not None:
        return configured.lower() in {"1", "true", "yes"}
    return not direct_fileid_required()


def item_failure_placeholder_enabled() -> bool:
    """Allow one known, content-safe item failure without cancelling the daily report."""
    return os.getenv("LLM_ITEM_FAILURE_POLICY", "placeholder").lower() != "fail"


DATA_INSPECTION_SUMMARY_NOTE = (
    "\u56e0 LLM \u670d\u52a1\u5bf9\u5b8c\u6574\u5b57\u5e55\u8f93\u5165\u89e6\u53d1 "
    "data_inspection_failed\uff0c\u672c\u6761\u6458\u8981\u53ea\u57fa\u4e8e"
    "\u8282\u76ee\u5143\u6570\u636e\u3001\u516c\u5f00\u63cf\u8ff0\u548c\u53ef\u5b89\u5168"
    "\u53d1\u9001\u7684\u7ebf\u7d22\u751f\u6210\uff1b\u5b8c\u6574\u5b57\u5e55\u5df2"
    "\u4fdd\u7559\uff0c\u8bf7\u4ee5\u539f\u6587\u4e3a\u51c6\u3002"
)
CONTEXT_LENGTH_SUMMARY_NOTE = (
    "\u56e0 LLM \u670d\u52a1\u5355\u8f6e\u8f93\u5165\u957f\u5ea6\u9650\u5236\uff0c"
    "\u672c\u6761\u6458\u8981\u53ea\u57fa\u4e8e\u8282\u76ee\u5143\u6570\u636e\u3001"
    "\u516c\u5f00\u63cf\u8ff0\u548c\u5df2\u538b\u7f29\u7684\u8bc1\u636e\u7ebf\u7d22"
    "\u751f\u6210\uff1b\u5b8c\u6574\u5b57\u5e55\u5df2\u4fdd\u7559\uff0c"
    "\u8bf7\u4ee5\u539f\u6587\u4e3a\u51c6\u3002"
)


def deterministic_item_failure_digest(item: dict[str, Any], reason: str) -> dict[str, Any]:
    """Emit an explicit, non-LLM placeholder without making claims about blocked content."""
    if reason == "provider_input_inspection":
        summary = (
            "上游模型的输入安全审核未允许处理本期完整转写，因此未生成基于逐字稿的自动摘要。"
        )
        one_liner = "模型安全审核未生成摘要。"
        quality = "provider_input_rejected"
    else:
        summary = (
            "上游模型未能按日报固定格式完成本期整篇转写摘要，因此未生成内容性结论。"
        )
        one_liner = "模型未通过摘要格式校验。"
        quality = "direct_fileid_contract_failed"

    raw = {
        "short_title": item.get("title") or item.get("original_title") or "摘要受限内容",
        "one_liner": one_liner,
        "why_it_matters": "单条内容异常不会中断当天日报发布。",
        "content_density": "brief",
        "summary": [
            summary
            + "原始节目链接和转写产物仍已保留；如需使用本期内容，请以原始来源为准。"
        ],
        "core_points": [
            "本期未生成基于完整转写的自动内容摘要。",
            "原始节目链接与转写产物已保留，便于人工核对。",
        ],
        "key_facts": [],
        "takeaways": ["如需引用本期内容，请直接阅读原始节目或转写并人工确认。"],
        "guests": ["未生成嘉宾与机构信息。"],
        "topics": ["摘要受限"],
        "tensions": ["第三方模型的安全审核或格式约束不应阻断整份日报。"],
        "quote": None,
        "importance_score": 1,
        "quality": quality,
    }
    digest = normalize_digest(raw, item)
    digest["quality"] = quality
    return digest


def prepend_generation_note(digest: dict[str, Any], note: str, max_items: int) -> dict[str, Any]:
    summary = [str(value) for value in digest.get("summary", []) if str(value).strip()]
    digest["summary"] = [note, *summary][:max_items]
    digest["why_it_matters"] = note[:60]
    return digest


def metadata_evidence_map(item: dict[str, Any]) -> dict[str, str]:
    parts = [
        f"title: {item.get('title') or item.get('original_title') or ''}",
        f"source: {item.get('source_name') or ''}",
        f"platform: {item.get('platform') or ''}",
        f"duration_seconds: {item.get('duration') or item.get('duration_seconds') or ''}",
        f"published_at: {item.get('published_at') or ''}",
        f"description: {item.get('description') or ''}",
    ]
    text = re.sub(r"\s+", " ", "\n".join(part for part in parts if part.strip())).strip()
    return {"M001": text[:8000] or "metadata unavailable"}


def build_metadata_digest_messages(
    item: dict[str, Any],
    profile: dict[str, Any],
    evidence: dict[str, str],
    schema: dict[str, Any],
    reason: str,
) -> list[dict[str, str]]:
    contract = schema.get("contract", {}) if isinstance(schema.get("contract"), dict) else {}
    count_contract = (
        f"content_density={schema.get('content_density')}; "
        f"summary items={contract.get('summary_items')}; "
        f"summary chars/item<={contract.get('summary_char_limit')}; "
        f"core_points items={contract.get('core_points_items')}; "
        f"takeaways items={contract.get('takeaways_items')}."
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a conservative Chinese daily digest editor. "
                f"The full transcript cannot be sent to the model because {reason}. "
                "Use only the supplied metadata and public description. Do not invent details. "
                "If a point is based on title/description rather than full transcript, phrase it cautiously. "
                "Every sourced field must cite M001. Output one JSON object only. "
                f"{DEEP_SUMMARY_GUIDANCE} Hard count contract: {count_contract}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Return JSON matching this schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
                f"Transcript profile:\n{json.dumps(profile, ensure_ascii=False)}\n\n"
                f"Metadata evidence:\n{json.dumps(evidence, ensure_ascii=False)}"
            ),
        },
    ]


def build_final_schema(first_ref: str, contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "short_title": "18 chars or fewer, Chinese reader-facing title",
        "one_liner": {
            "text": "30 chars or fewer; specific conclusion, not a title rewrite",
            "source_refs": [first_ref],
        },
        "why_it_matters": {
            "text": "60 chars or fewer; concrete reason to read",
            "source_refs": [first_ref],
        },
        "content_density": f"MUST be {contract['content_density']}",
        "summary": [
            {
                "text": (
                    f"{contract['summary_char_limit']} chars or fewer; this is '完整摘要 · 深读', "
                    "write one coherent analytical paragraph per item, not a bullet; each paragraph "
                    "must include claim + support + meaning, and must not duplicate core_points, "
                    "key_facts, tensions, or takeaways; "
                    f"return {contract['summary_min']} to {contract['summary_max']} items"
                ),
                "source_refs": [first_ref],
            }
        ],
        "core_points": [
            {
                "text": (
                    "90 chars or fewer; claim with support, not a topic label; "
                    f"return {contract['core_points_min']} to {contract['core_points_max']} items"
                ),
                "source_refs": [first_ref],
            }
        ],
        "key_facts": [
            {
                "label": "fact label",
                "value": "number, name, event, policy, product, or case",
                "context": "why this fact matters",
                "source_refs": [first_ref],
            }
        ],
        "takeaways": ["reader action or reusable lens; not a research question"],
        "guests": [
            {
                "text": "person / organization / role, or say no clear guest",
                "source_refs": [first_ref],
            }
        ],
        "topics": ["topic keyword"],
        "tensions": [
            {
                "text": "tradeoff, limit, disagreement, incentive conflict, or open question",
                "source_refs": [first_ref],
            }
        ],
        "quote": {
            "text": "optional quote or paraphrase",
            "speaker": "speaker",
            "kind": "paraphrase",
            "source_refs": [first_ref],
        },
        "importance_score": 4,
        "contract": {
            "summary_items": f"{contract['summary_min']}..{contract['summary_max']}",
            "summary_char_limit": contract["summary_char_limit"],
            "core_points_items": f"{contract['core_points_min']}..{contract['core_points_max']}",
            "takeaways_items": "1..3",
            "include_item": "This item is >= 5 minutes and must appear in the report.",
            "section_roles": DEEP_SUMMARY_GUIDANCE,
        },
    }


def build_fileid_final_digest_messages(
    item: dict[str, Any],
    profile: dict[str, Any],
    file_id: str,
    segment_evidence: list[dict[str, Any]],
    ranked: dict[str, Any],
    schema: dict[str, Any],
) -> list[dict[str, str]]:
    contract = schema.get("contract", {}) if isinstance(schema.get("contract"), dict) else {}
    count_contract = (
        f"Hard count contract: content_density={schema.get('content_density')}; "
        f"summary items={contract.get('summary_items', 'follow schema')}; "
        f"summary chars/item<={contract.get('summary_char_limit', 'follow schema')}; "
        f"core_points items={contract.get('core_points_items', 'follow schema')}; "
        f"takeaways items={contract.get('takeaways_items', '1..3')}. "
        "The item is eligible for the report; do not omit it because of a low score."
    )
    payload = {
        "profile": profile,
        "ranked": ranked,
        "segment_evidence": compact_segment_evidence_for_prompt(segment_evidence, aggressive=True),
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    payload_limit = int(os.getenv("LLM_FILEID_EVIDENCE_PAYLOAD_CHARS", "16000"))
    if len(payload_json) > payload_limit:
        payload_json = payload_json[:payload_limit]
    return [
        {
            "role": "system",
            "content": (
                "The attached file is the full transcript. Use it as the primary source. "
                "The structured evidence package is a navigation aid and citation map. "
                "Do not invent facts outside the transcript, metadata, and evidence package. "
                f"{DEEP_SUMMARY_GUIDANCE} "
                "Every factual or interpretive field with source_refs must cite valid segment refs such as S001. "
                f"{count_contract} Output one JSON object only, in Chinese."
            ),
        },
        {"role": "system", "content": f"fileid://{file_id}"},
        {
            "role": "user",
            "content": (
                f"{count_contract}\n\n"
                "Return JSON that matches this schema exactly:\n"
                f"{json.dumps(schema, ensure_ascii=False)}\n\n"
                f"Item metadata:\n{json.dumps(item, ensure_ascii=False)}\n\n"
                f"Evidence package:\n{payload_json}"
            ),
        },
    ]


def should_use_direct_fileid(item: dict[str, Any], transcript: str) -> bool:
    if os.getenv("LLM_FILEID_DIRECT_ENABLED", "1") == "0":
        return False
    duration = int(item.get("duration") or item.get("duration_seconds") or 0)
    if duration < 300:
        return False
    min_duration = int(os.getenv("LLM_FILEID_DIRECT_MIN_DURATION_SECONDS", "300"))
    min_chars = int(os.getenv("LLM_FILEID_DIRECT_MIN_CHARS", "1"))
    # A zero maximum means unlimited. qwen-long file uploads exist specifically
    # to keep long transcripts out of the inline chat context, so imposing a
    # small default ceiling here defeats file-id mode and re-enters the legacy
    # segmented pipeline for the longest episodes.
    max_duration = int(os.getenv("LLM_FILEID_DIRECT_MAX_DURATION_SECONDS", "0"))
    max_chars = int(os.getenv("LLM_FILEID_DIRECT_MAX_CHARS", "0"))
    if max_duration > 0 and duration > max_duration:
        return False
    if max_chars > 0 and len(transcript) > max_chars:
        return False
    return bool(transcript.strip()) and (duration >= min_duration or len(transcript) >= min_chars)


def build_fileid_direct_digest_messages(
    item: dict[str, Any],
    profile: dict[str, Any],
    file_id: str,
    schema: dict[str, Any],
) -> list[dict[str, str]]:
    contract = schema.get("contract", {}) if isinstance(schema.get("contract"), dict) else {}
    count_contract = (
        f"Hard count contract: content_density={schema.get('content_density')}; "
        f"summary items={contract.get('summary_items', 'follow schema')}; "
        f"summary chars/item<={contract.get('summary_char_limit', 'follow schema')}; "
        f"core_points items={contract.get('core_points_items', 'follow schema')}; "
        f"takeaways items={contract.get('takeaways_items', '1..3')}. "
        "This item is >= 5 minutes and must appear in the report."
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a senior Chinese editor for long-form podcast/video transcripts. "
                "The attached file is the complete transcript and is the primary source. "
                "Read across the full transcript before writing; do not summarize only the beginning. "
                "Find the episode's central question, argument arc, strongest mechanisms, examples, numbers, "
                "speaker positions, caveats, and reusable reader value. "
                "Preserve every number's scope, unit, currency, time period, baseline, and qualifiers. "
                "Never rewrite a subset or a count after a baseline as an all-time total. "
                "Do not write a chronological recap unless the argument is chronological. "
                f"{DEEP_SUMMARY_GUIDANCE} "
                "Every field with source_refs must cite F001 exactly. "
                f"{count_contract} Output one JSON object only, in Chinese."
            ),
        },
        {"role": "system", "content": f"fileid://{file_id}"},
        {
            "role": "user",
            "content": (
                f"{count_contract}\n\n"
                "Return JSON that matches this schema exactly. Use source_refs [\"F001\"] for all sourced fields:\n"
                f"{json.dumps(schema, ensure_ascii=False)}\n\n"
                f"Item metadata:\n{json.dumps(item, ensure_ascii=False)}\n\n"
                f"Transcript profile:\n{json.dumps(profile, ensure_ascii=False)}"
            ),
        },
    ]


def summarize_item_fileid_direct(
    item: dict[str, Any],
    transcript: str,
    transcript_meta: dict[str, Any] | None,
    max_attempts: int,
    fileid_cache_dir: Path | None,
) -> dict[str, Any]:
    profile = build_transcript_profile(item, transcript, transcript_meta)
    evidence = {"F001": transcript}
    contract = digest_contract_for_item(item, profile)
    schema = build_final_schema("F001", contract)
    print(f"Uploading transcript for direct qwen-long fileid mode: {item.get('title')}", flush=True)
    file_id = upload_qwen_file(transcript, item, fileid_cache_dir)
    digest = llm_json(
        build_fileid_direct_digest_messages(item, profile, file_id, schema),
        lambda raw, c=contract: validate_final_digest(raw, item, evidence, c),
        max_attempts,
    )
    digest["quality"] = "llm_fileid_full"
    return digest


def metadata_llm_digest(
    item: dict[str, Any],
    transcript: str,
    transcript_meta: dict[str, Any] | None,
    max_attempts: int,
    note: str = DATA_INSPECTION_SUMMARY_NOTE,
    reason: str = "the provider rejected it during input inspection",
) -> dict[str, Any]:
    profile = build_transcript_profile(item, transcript, transcript_meta)
    evidence = metadata_evidence_map(item)
    contract = digest_contract_for_item(item, profile)
    schema = build_final_schema("M001", contract)
    digest = llm_json(
        build_metadata_digest_messages(item, profile, evidence, schema, reason),
        lambda raw, c=contract: validate_final_digest(raw, item, evidence, c),
        max_attempts,
    )
    digest["quality"] = "llm_metadata_due_input_inspection"
    return prepend_generation_note(digest, note, int(contract["summary_max"]))


def load_evidence_artifacts(
    evidence_dir: Path | None,
    item: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]] | None:
    if evidence_dir is None:
        return None
    stem = item_artifact_id(item)
    segments_path = evidence_dir / f"item_segments_{stem}.json"
    evidence_path = evidence_dir / f"item_evidence_{stem}.json"
    ranked_path = evidence_dir / f"item_ranked_insights_{stem}.json"
    if not (segments_path.exists() and evidence_path.exists() and ranked_path.exists()):
        return None
    try:
        segments = read_json(segments_path)
        segment_evidence = read_json(evidence_path)
        ranked = read_json(ranked_path)
    except Exception:
        return None
    if not isinstance(segments, list) or not isinstance(segment_evidence, list) or not isinstance(ranked, dict):
        return None
    return segments, segment_evidence, ranked


def digest_cache_path(cache_dir: Path | None, report_date: str, item: dict[str, Any]) -> Path | None:
    if cache_dir is None:
        return None
    return cache_dir / report_date / f"{item_artifact_id(item)}.json"


def load_digest_cache(
    cache_dir: Path | None,
    report_date: str,
    item: dict[str, Any],
) -> dict[str, Any] | None:
    path = digest_cache_path(cache_dir, report_date, item)
    if path is None or not path.exists():
        return None
    try:
        data = read_json(path)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("cache_version") != DIGEST_CACHE_VERSION:
        return None
    if data.get("model") != os.getenv("LLM_MODEL", ""):
        return None
    digest = data.get("digest")
    return digest if isinstance(digest, dict) else None


def write_digest_cache(
    cache_dir: Path | None,
    report_date: str,
    item: dict[str, Any],
    digest: dict[str, Any],
) -> None:
    path = digest_cache_path(cache_dir, report_date, item)
    if path is None:
        return
    write_json(
        path,
        {
            "cache_version": DIGEST_CACHE_VERSION,
            "date": report_date,
            "model": os.getenv("LLM_MODEL", ""),
            "item_url": item.get("url", ""),
            "item_title": item.get("title") or item.get("original_title") or "",
            "digest": digest,
        },
    )


def validate_final_digest(
    raw: dict[str, Any],
    item: dict[str, Any],
    evidence: dict[str, str],
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = coerce_final_digest_shape(raw, evidence)
    valid_refs = set(evidence)

    def source_numbers_for(value: dict[str, Any]) -> set[str]:
        refs = [str(ref) for ref in value.get("source_refs", []) if str(ref) in valid_refs]
        if not refs:
            return set()
        return number_tokens(" ".join(evidence[ref] for ref in refs))

    def missing_numbers(value: dict[str, Any], *text_keys: str) -> list[str]:
        claim = " ".join(str(value.get(key, "")) for key in text_keys)
        numbers = number_tokens(claim)
        if not numbers:
            return []
        grounded = source_numbers_for(value)
        return sorted(numbers - grounded)

    def split_sentences(text: str) -> list[str]:
        pieces = re.findall(r"[^。！？!?;；\n.]+[。！？!?;；.]?", text)
        return [piece.strip() for piece in pieces if piece.strip()]

    def has_malformed_number(sentence: str) -> bool:
        return bool(re.search(r"(?:^|[^\d])\d+\.(?:\s|$)", sentence))

    def remove_ungrounded_number_sentences(value: dict[str, Any], field: str) -> bool:
        text = str(value.get("text", "")).strip()
        if not text:
            return False
        has_missing_numbers = bool(missing_numbers(value, "text"))
        has_malformed_numbers = any(has_malformed_number(sentence) for sentence in split_sentences(text))
        if not has_missing_numbers and not has_malformed_numbers:
            return bool(text)
        grounded = source_numbers_for(value)
        kept: list[str] = []
        dropped: list[str] = []
        for sentence in split_sentences(text):
            sentence_numbers = number_tokens(sentence)
            if has_malformed_number(sentence):
                dropped.append("malformed")
                continue
            if sentence_numbers and sentence_numbers - grounded:
                dropped.extend(sorted(sentence_numbers - grounded))
                continue
            kept.append(sentence)
        cleaned = re.sub(r"\s+", " ", "".join(kept)).strip()
        value["text"] = cleaned
        if dropped:
            value.setdefault("validation_warnings", []).append(
                f"removed ungrounded number sentence from {field}: {', '.join(sorted(set(dropped)))}"
            )
        return bool(cleaned)

    scalar_limits = {"one_liner": 30, "why_it_matters": 60}
    for field, limit in scalar_limits.items():
        value = raw.get(field)
        if not isinstance(value, dict) or not str(value.get("text", "")).strip():
            raise ValueError(f"{field} must be an object with text and source_refs")
        if not any(str(ref) in valid_refs for ref in value.get("source_refs", [])):
            raise ValueError(f"{field} must cite a valid source_ref")
        if not remove_ungrounded_number_sentences(value, field):
            raise ValueError(f"{field} became empty after removing ungrounded number sentence(s)")
    contract = contract or digest_contract_for_item(item)
    density = clean_text(raw.get("content_density") or "standard").lower()
    if density not in {"brief", "standard", "high"}:
        density = str(contract["content_density"])
    if density != contract["content_density"]:
        density = str(contract["content_density"])
    raw["content_density"] = density
    list_rules = {
        "summary": (
            int(contract["summary_min"]),
            int(contract["summary_max"]),
            int(contract.get("summary_char_limit", 420)),
        ),
        "core_points": (int(contract["core_points_min"]), int(contract["core_points_max"]), 90),
    }
    for field, (minimum, maximum, limit) in list_rules.items():
        values = raw.get(field)
        if not isinstance(values, list) or len(values) < minimum:
            raise ValueError(f"{field} must contain {minimum}..{maximum} item(s)")
        cleaned_values: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict) or not str(value.get("text", "")).strip():
                continue
            if not remove_ungrounded_number_sentences(value, field):
                continue
            text = clean_text(value.get("text", ""))
            if nonspace_len(text) > limit:
                raise ValueError(f"every {field} item must be {limit} chars or fewer")
            if field == "summary" and re.match(r"^\s*(?:[-*]|\d+[.)、])\s+", str(value.get("text", ""))):
                raise ValueError("summary must be analytical paragraphs, not list items")
            if not any(str(ref) in valid_refs for ref in value.get("source_refs", [])):
                continue
            cleaned_values.append(value)
        raw[field] = cleaned_values[:maximum]
        if len(raw[field]) < minimum:
            raise ValueError(
                f"{field} must contain {minimum}..{maximum} item(s) after removing ungrounded number sentence(s)"
            )
    takeaways = raw.get("takeaways")
    if isinstance(takeaways, list):
        cleaned_takeaways: list[str] = []
        for value in takeaways:
            text = re.sub(r"[?？]+", "。", clean_text(value)).strip()
            if text and text not in cleaned_takeaways:
                cleaned_takeaways.append(text)
        raw["takeaways"] = cleaned_takeaways[:3]
        takeaways = raw["takeaways"]
    if not isinstance(takeaways, list) or not 1 <= len(takeaways) <= 3:
        raise ValueError("takeaways must contain one to three reader actions")
    if any("?" in str(value) or "？" in str(value) for value in takeaways):
        raise ValueError("takeaways must be actions, not research questions")
    guests = raw.get("guests")
    if not isinstance(guests, list) or len(guests) < 1:
        raise ValueError("guests must contain one to five evidence-backed entries")
    cleaned_guests: list[dict[str, Any]] = []
    for guest in guests:
        if not isinstance(guest, dict) or not clean_text(guest.get("text")):
            continue
        if not any(str(ref) in valid_refs for ref in guest.get("source_refs", [])):
            continue
        if remove_ungrounded_number_sentences(guest, "guests"):
            cleaned_guests.append(guest)
        if len(cleaned_guests) >= 5:
            break
    raw["guests"] = cleaned_guests
    if not raw["guests"]:
        raise ValueError("guests must contain one to five evidence-backed entries")
    tensions = raw.get("tensions", [])
    if not isinstance(tensions, list):
        raise ValueError("tensions must be an array with at most three entries")
    cleaned_tensions: list[dict[str, Any]] = []
    for tension in tensions:
        if not isinstance(tension, dict) or not clean_text(tension.get("text")):
            continue
        if not any(str(ref) in valid_refs for ref in tension.get("source_refs", [])):
            continue
        if remove_ungrounded_number_sentences(tension, "tensions"):
            cleaned_tensions.append(tension)
        if len(cleaned_tensions) >= 3:
            break
    raw["tensions"] = cleaned_tensions
    cleaned_facts: list[dict[str, Any]] = []
    for fact in raw.get("key_facts", []) if isinstance(raw.get("key_facts"), list) else []:
        if isinstance(fact, dict) and not missing_numbers(fact, "value", "context"):
            cleaned_facts.append(fact)
    raw["key_facts"] = cleaned_facts[:8]
    quote = raw.get("quote")
    if isinstance(quote, dict):
        if missing_numbers(quote, "text"):
            raw["quote"] = None
    digest = normalize_digest(raw, item, evidence=evidence, strict_evidence=True)
    digest["quality"] = "llm_evidence_validated"
    return digest


def summarize_item_contract(
    item: dict[str, Any],
    transcript: str,
    max_attempts: int,
    evidence_dir: Path | None = None,
    fileid_cache_dir: Path | None = None,
    transcript_meta: dict[str, Any] | None = None,
    timed_caption: str = "",
) -> dict[str, Any]:
    """Build an item digest, preferring direct file-id synthesis for long transcripts."""
    if not llm_configured():
        raise RuntimeError("大模型摘要生成失败：LLM_BASE_URL and LLM_MODEL are not configured")

    profile = build_transcript_profile(item, transcript, transcript_meta)
    if should_use_direct_fileid(item, transcript):
        try:
            return summarize_item_fileid_direct(
                item,
                transcript,
                transcript_meta,
                max_attempts,
                fileid_cache_dir,
            )
        except Exception as exc:
            if is_data_inspection_error(exc):
                raise
            if not evidence_fallback_enabled():
                if "LLM output failed validation after" in str(exc):
                    raise DirectFileIdContractError(
                        f"direct file-id synthesis could not satisfy its contract: {exc}"
                    ) from exc
                raise
            print(
                f"Direct qwen-long fileid synthesis failed for {item.get('title')}: {exc}; "
                "falling back to segmented evidence pipeline.",
                flush=True,
            )

    cached_artifacts = load_evidence_artifacts(evidence_dir, item)
    if cached_artifacts is not None:
        segments, segment_evidence, ranked = cached_artifacts
        print(f"Reusing evidence artifacts for {item.get('title')}", flush=True)
    else:
        segments = build_topic_segments(
            item,
            transcript,
            timed_caption=timed_caption,
            duration_seconds=profile.get("duration_seconds"),
        )
        if not segments:
            raise RuntimeError("transcript is empty after topic segmentation")

        segment_evidence = []
        for index, segment in enumerate(segments, 1):
            segment_id = str(segment["segment_id"])
            print(
                f"Extracting evidence {index}/{len(segments)} for {item.get('title')} ({segment_id})",
                flush=True,
            )
            segment_evidence.append(
                llm_json(
                    build_segment_extraction_messages(item, profile, segment),
                    lambda raw, sid=segment_id: normalize_segment_evidence(raw, sid),
                    max_attempts,
                )
            )

        evidence_for_ranking = build_evidence_map_from_segments(segments)
        if not evidence_for_ranking:
            raise RuntimeError("transcript is empty after evidence segmentation")
        print(f"Ranking evidence for {item.get('title')}", flush=True)
        ranked = llm_json(
            build_ranking_messages(item, profile, segment_evidence),
            lambda raw, refs=set(evidence_for_ranking): normalize_ranked_insights(raw, refs),
            max_attempts,
        )
        if evidence_dir is not None:
            write_evidence_artifacts(evidence_dir, item, segments, segment_evidence, ranked)

    evidence = build_evidence_map_from_segments(segments)
    if not evidence:
        raise RuntimeError("transcript is empty after evidence segmentation")

    first_ref = next(iter(evidence))
    contract = digest_contract_for_item(item, profile)
    schema = build_final_schema(first_ref, contract)

    use_fileid = (
        os.getenv("LLM_FILEID_ENABLED", "1") != "0"
        and bool(transcript.strip())
        and (
            len(transcript) >= int(os.getenv("LLM_FILEID_MIN_CHARS", "0"))
            or int(item.get("duration") or item.get("duration_seconds") or 0)
            >= int(os.getenv("LLM_FILEID_MIN_DURATION_SECONDS", "300"))
        )
    )
    if use_fileid:
        try:
            print(f"Uploading transcript for qwen-long fileid mode: {item.get('title')}", flush=True)
            file_id = upload_qwen_file(transcript, item, fileid_cache_dir)
            digest = llm_json(
                build_fileid_final_digest_messages(item, profile, file_id, segment_evidence, ranked, schema),
                lambda raw, c=contract: validate_final_digest(raw, item, evidence, c),
                max_attempts,
            )
            digest["quality"] = "llm_fileid_segmented"
            return digest
        except Exception as exc:
            if is_data_inspection_error(exc):
                raise
            print(
                f"qwen-long fileid final synthesis failed for {item.get('title')}: {exc}; "
                "falling back to compact evidence synthesis.",
                flush=True,
            )

    try:
        digest = llm_json(
            build_final_digest_messages(item, profile, evidence, segment_evidence, ranked, schema),
            lambda raw, c=contract: validate_final_digest(raw, item, evidence, c),
            max_attempts,
        )
    except Exception as exc:
        if not is_context_length_error(exc):
            raise
        old_limit = os.environ.get("LLM_FINAL_PAYLOAD_CHARS")
        os.environ["LLM_FINAL_PAYLOAD_CHARS"] = os.getenv("LLM_FINAL_PAYLOAD_CHARS_RETRY", "8000")
        try:
            digest = llm_json(
                build_final_digest_messages(item, profile, evidence, segment_evidence, ranked, schema),
                lambda raw, c=contract: validate_final_digest(raw, item, evidence, c),
                max_attempts,
            )
        finally:
            if old_limit is None:
                os.environ.pop("LLM_FINAL_PAYLOAD_CHARS", None)
            else:
                os.environ["LLM_FINAL_PAYLOAD_CHARS"] = old_limit
    digest["quality"] = "llm_evidence_ranked"
    return digest


def main() -> int:
    args = parse_args()
    if args.llm_policy == "required" and not llm_configured():
        print("LLM is required but LLM_BASE_URL and LLM_MODEL are not configured")
        return 2
    items = json.loads(Path(args.items_json).read_text(encoding="utf-8-sig"))
    # Filter out short clips (duration < 5 minutes = 300 seconds)
    original_count = len(items)
    items = [it for it in items if (it.get("duration") or it.get("duration_seconds") or 0) >= 300]
    skipped = original_count - len(items)
    if skipped:
        print(f"Skipped {skipped} short clip(s) (duration < 5min)")
    transcript_index = load_transcript_index(Path(args.subtitles_dir))

    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else None
    digest_cache_dir = Path(args.digest_cache_dir) if args.digest_cache_dir else None
    fileid_cache_dir = Path(args.fileid_cache_dir) if args.fileid_cache_dir else None
    prepared_items: list[tuple[dict[str, Any], str, dict[str, Any], str]] = []
    missing_transcripts: list[dict[str, Any]] = []
    for item in items:
        meta = transcript_index.get(normalize_url(item.get("url", "")))
        transcript = ""
        timed_caption = ""
        if meta and meta.get("text_path"):
            transcript = read_text(meta["text_path"])
        if meta and meta.get("subtitle_path"):
            timed_caption = read_text(meta["subtitle_path"])
        item["transcript_available"] = bool(transcript)
        if not transcript:
            missing_transcripts.append(item)
        prepared_items.append((item, transcript, meta or {}, timed_caption))

    if args.require_transcripts and missing_transcripts:
        print("Refusing to generate an incomplete report; transcripts are missing for:")
        for item in missing_transcripts:
            print(f"- {item.get('title') or item.get('url')}")
        return 3

    item_digests: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item, transcript, meta, timed_caption in prepared_items:
        cached_digest = load_digest_cache(digest_cache_dir, args.date, item)
        if cached_digest is not None:
            print(f"Reusing final digest cache for {item.get('title')}", flush=True)
            item_digests.append((item, cached_digest))
            continue
        try:
            print(f"Generating digest for {item.get('title')}", flush=True)
            digest = summarize_item_contract(
                item,
                transcript or item.get("description", ""),
                args.llm_max_attempts,
                evidence_dir=evidence_dir,
                fileid_cache_dir=fileid_cache_dir,
                transcript_meta=meta,
                timed_caption=timed_caption,
            )
            write_digest_cache(digest_cache_dir, args.date, item, digest)
        except Exception as exc:
            failure_kind = ""
            if is_data_inspection_error(exc):
                failure_kind = "provider_input_inspection"
            elif isinstance(exc, DirectFileIdContractError):
                failure_kind = "direct_fileid_contract"

            if failure_kind:
                if failure_kind == "provider_input_inspection" and metadata_fallback_enabled():
                    print(
                        f"LLM input inspection failed for {item.get('title')}; "
                        "trying the clearly marked metadata-only digest first."
                    )
                    try:
                        digest = metadata_llm_digest(
                            item,
                            transcript or item.get("description", ""),
                            meta,
                            args.llm_max_attempts,
                        )
                    except Exception as fallback_exc:
                        print(
                            f"Metadata LLM fallback also failed for {item.get('title')}: {fallback_exc}",
                            flush=True,
                        )
                    else:
                        write_digest_cache(digest_cache_dir, args.date, item, digest)
                        item_digests.append((item, digest))
                        continue

                if item_failure_placeholder_enabled():
                    print(
                        f"Writing a transparent non-LLM placeholder for {item.get('title')}: {failure_kind}",
                        flush=True,
                    )
                    digest = deterministic_item_failure_digest(item, failure_kind)
                    write_digest_cache(digest_cache_dir, args.date, item, digest)
                    item_digests.append((item, digest))
                    continue

                print(
                    f"Full-transcript LLM digest failed for {item.get('title')}: {exc}",
                    flush=True,
                )
                print("大模型摘要生成失败，请重新运行。", flush=True)
                return 4
            if is_context_length_error(exc):
                if not metadata_fallback_enabled():
                    print(
                        f"Full-transcript LLM digest failed for {item.get('title')}: {exc}",
                        flush=True,
                    )
                    print("大模型摘要生成失败，请重新运行。", flush=True)
                    return 4
                print(
                    f"LLM input length limit hit for {item.get('title')}; "
                    "writing a clearly marked metadata-based digest instead."
                )
                try:
                    digest = metadata_llm_digest(
                        item,
                        transcript or item.get("description", ""),
                        meta,
                        args.llm_max_attempts,
                        note=CONTEXT_LENGTH_SUMMARY_NOTE,
                        reason="the provider reported a single-round input length limit",
                    )
                    digest["quality"] = "llm_metadata_due_context_length"
                except Exception as fallback_exc:
                    print(
                        f"Metadata LLM fallback failed for {item.get('title')}: {fallback_exc}; "
                        "大模型摘要生成失败，请重新运行。"
                    )
                    return 4
                write_digest_cache(digest_cache_dir, args.date, item, digest)
                item_digests.append((item, digest))
                continue
            print(
                f"大模型摘要生成失败 for {item.get('title')}: {exc}",
                flush=True,
            )
            print("未发布降级摘要；请重新运行本次日报生成。", flush=True)
            return 4
        item_digests.append((item, digest))

    report = build_report(args.date, item_digests)
    try:
        theme_entries = generate_report_themes(report["items"], args.llm_max_attempts)
    except Exception as exc:
        print(f"Skipping information map because report-level theme synthesis failed: {exc}", flush=True)
        theme_entries = []
    report["themes"] = [entry["title"] for entry in theme_entries]
    report["theme_sources"] = theme_entries
    report["generation"] = {
        "mode": "llm_evidence_validated" if llm_configured() else "llm_not_configured",
        "model": os.getenv("LLM_MODEL", ""),
        "max_contract_attempts": args.llm_max_attempts,
        "transcripts_required": bool(args.require_transcripts),
        "evidence_dir": str(evidence_dir) if evidence_dir else "",
        "fileid_cache_dir": str(fileid_cache_dir) if fileid_cache_dir else "",
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
