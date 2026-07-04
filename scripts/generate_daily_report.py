#!/usr/bin/env python3
"""Generate a daily Markdown report from full transcript files."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

try:
    from report_contract import build_report, clean_text, nonspace_len, normalize_digest, report_to_markdown
except ModuleNotFoundError:  # Imported as scripts.generate_daily_report in tests/tools.
    from scripts.report_contract import build_report, clean_text, nonspace_len, normalize_digest, report_to_markdown

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


BASE_DIR = Path(__file__).resolve().parents[1]
SPEC_PATH = BASE_DIR / "templates" / "daily_report_llm_spec.md"
REPORTS_DIR = BASE_DIR / "reports"
LLM_NEUTRAL_ARCHIVE_PREAMBLE = (
    "前置免责说明：本次任务仅做纯客观财经、科技、商业与行业播客/视频文本信息整理，"
    "以及英文内容事实提炼，仅用于商业经济学术研究、行业内容摘要归档，不用于舆论引导、"
    "政治评论、舆情解读、社会争议炒作、境外观点放大传播等用途。所有内容仅客观转述"
    "原文字面信息，不代表模型、使用者认同或佐证文中任何观点，仅做信息归档工具用途。"
    "请仅基于原始文本做中立、客观、事实性的文字总结，不做主观价值评判。"
)
CATEGORIES = [
    "科技 / AI / VC",
    "商业 / 财经 / 投资",
    "产品 / 创业 / 管理",
    "新闻 / 时评 / 全球议题",
    "文化 / 社会 / 人文",
]

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

DIGEST_CACHE_VERSION = 4

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
        default=os.getenv("LLM_POLICY", "extractive"),
        help="extractive uses a deterministic transcript fallback; required fails without LLM config",
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
    return (
        "exceeds token limit" in text
        or "token limit" in text
        or "context length" in text
        or "maximum context" in text
        or "invalid_parameter_error" in text
    )


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
    max_duration = int(os.getenv("LLM_FILEID_DIRECT_MAX_DURATION_SECONDS", "1800"))
    max_chars = int(os.getenv("LLM_FILEID_DIRECT_MAX_CHARS", "30000"))
    if duration > max_duration or len(transcript) > max_chars:
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


def deterministic_moderation_digest(
    item: dict[str, Any],
    transcript: str,
    note: str = DATA_INSPECTION_SUMMARY_NOTE,
) -> dict[str, Any]:
    source = item.get("description") or transcript or item.get("title") or ""
    sentences = extract_sentences(source, limit=10)
    title = short_title(item)
    contract = digest_contract_for_item(item, {"text_chars": len(transcript)})
    summary_min = int(contract["summary_min"])
    core_min = int(contract["core_points_min"])
    while len(sentences) < max(summary_min, core_min):
        sentences.append(f"《{title}》已达到 5 分钟收录阈值；本条因模型输入审查限制，采用保守摘要。")
    raw = {
        "short_title": title,
        "one_liner": f"本期围绕《{title}》展开，适合结合原始链接复核。",
        "why_it_matters": "字幕已获取，但模型输入审查限制了深度生成；先保留条目与可核对线索。",
        "content_density": contract["content_density"],
        "summary": [note, *sentences][: int(contract["summary_max"])],
        "core_points": sentences[: int(contract["core_points_max"])],
        "key_facts": [
            {
                "label": "时长",
                "value": str(item.get("duration") or item.get("duration_seconds") or ""),
                "context": "超过 5 分钟，按规则进入日报",
            },
            {
                "label": "来源",
                "value": item.get("source_name") or item.get("platform") or "",
                "context": "用于回到原始节目核对上下文",
            },
        ],
        "takeaways": ["先根据标题、来源和描述判断是否打开原节目。", "需要引用观点时回到字幕与原始链接核对。"],
        "guests": [item.get("source_name") or "未能从元数据可靠识别嘉宾"],
        "topics": [clean_text(item.get("category") or title)[:12]],
        "tensions": ["本条有完整字幕，但当前 LLM 服务拒绝接收部分输入，摘要深度受限。"],
        "quote": None,
        "importance_score": 2,
        "quality": "deterministic_due_input_inspection",
    }
    raw["why_it_matters"] = note
    return normalize_digest(raw, item)


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

    def assert_numbers_grounded(value: dict[str, Any], field: str, *text_keys: str) -> None:
        refs = [str(ref) for ref in value.get("source_refs", []) if str(ref) in valid_refs]
        if not refs:
            return
        claim = " ".join(str(value.get(key, "")) for key in text_keys)
        numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?%?", claim))
        if not numbers:
            return
        source = " ".join(evidence[ref] for ref in refs)
        source_numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?%?", source))
        missing = sorted(numbers - source_numbers)
        if missing:
            raise ValueError(f"{field} has numbers absent from its cited segment(s): {', '.join(missing)}")

    scalar_limits = {"one_liner": 30, "why_it_matters": 60}
    for field, limit in scalar_limits.items():
        value = raw.get(field)
        if not isinstance(value, dict) or not str(value.get("text", "")).strip():
            raise ValueError(f"{field} must be an object with text and source_refs")
        if not any(str(ref) in valid_refs for ref in value.get("source_refs", [])):
            raise ValueError(f"{field} must cite a valid source_ref")
        assert_numbers_grounded(value, field, "text")
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
        if not isinstance(values, list) or not minimum <= len(values) <= maximum:
            raise ValueError(f"{field} must contain {minimum}..{maximum} item(s)")
        for value in values:
            if not isinstance(value, dict) or not str(value.get("text", "")).strip():
                raise ValueError(f"every {field} item must contain text")
            text = clean_text(value.get("text", ""))
            if nonspace_len(text) > limit:
                raise ValueError(f"every {field} item must be {limit} chars or fewer")
            if field == "summary" and re.match(r"^\s*(?:[-*]|\d+[.)、])\s+", str(value.get("text", ""))):
                raise ValueError("summary must be analytical paragraphs, not list items")
            if not any(str(ref) in valid_refs for ref in value.get("source_refs", [])):
                raise ValueError(f"every {field} item must cite a valid source_ref")
            assert_numbers_grounded(value, field, "text")
    takeaways = raw.get("takeaways")
    if isinstance(takeaways, list):
        cleaned_takeaways: list[str] = []
        for value in takeaways:
            text = re.sub(r"[?？]+", "。", clean_text(value)).strip()
            if text and text not in cleaned_takeaways:
                cleaned_takeaways.append(text)
        raw["takeaways"] = cleaned_takeaways
        takeaways = cleaned_takeaways
    if not isinstance(takeaways, list) or not 1 <= len(takeaways) <= 3:
        raise ValueError("takeaways must contain one to three reader actions")
    if any("?" in str(value) or "？" in str(value) for value in takeaways):
        raise ValueError("takeaways must be actions, not research questions")
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
        assert_numbers_grounded(tension, "tensions", "text")
    for fact in raw.get("key_facts", []) if isinstance(raw.get("key_facts"), list) else []:
        if isinstance(fact, dict):
            assert_numbers_grounded(fact, "key_facts", "value", "context")
    quote = raw.get("quote")
    if isinstance(quote, dict):
        assert_numbers_grounded(quote, "quote", "text")
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

    first_ref = next(iter(evidence))
    contract = digest_contract_for_item(item, {"text_chars": len(transcript)})
    schema = build_final_schema(first_ref, contract)
    return llm_json(
        [
            {
                "role": "system",
                "content": (
                    "你是中文信息早餐编辑，只负责填写 JSON 内容，绝不输出 Markdown 或 XML。"
                    "读者要在30秒内决定是否继续读。所有事实、观点和摘要必须引用证据 ID。"
                    f"{DEEP_SUMMARY_GUIDANCE}"
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
        lambda raw, c=contract: validate_final_digest(raw, item, evidence, c),
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


def summarize_item_contract(
    item: dict[str, Any],
    transcript: str,
    max_attempts: int,
    evidence_dir: Path | None = None,
    fileid_cache_dir: Path | None = None,
    transcript_meta: dict[str, Any] | None = None,
    timed_caption: str = "",
) -> dict[str, Any]:
    """Build a high-quality item digest through evidence extraction and ranking."""
    if not llm_configured():
        return extractive_digest(item, transcript)

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
            if is_data_inspection_error(exc):
                print(
                    f"LLM input inspection failed for {item.get('title')}; "
                    "writing a clearly marked metadata-based digest instead."
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
                        f"Metadata LLM fallback failed for {item.get('title')}: {fallback_exc}; "
                        "using deterministic marked digest."
                    )
                    digest = deterministic_moderation_digest(item, transcript or item.get("description", ""))
                write_digest_cache(digest_cache_dir, args.date, item, digest)
                item_digests.append((item, digest))
                continue
            if is_context_length_error(exc):
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
                        "using deterministic marked digest."
                    )
                    digest = deterministic_moderation_digest(
                        item,
                        transcript or item.get("description", ""),
                        note=CONTEXT_LENGTH_SUMMARY_NOTE,
                    )
                    digest["quality"] = "deterministic_due_context_length"
                write_digest_cache(digest_cache_dir, args.date, item, digest)
                item_digests.append((item, digest))
                continue
            print(
                f"Validated LLM digest failed for {item.get('title')}: {exc}; "
                "using deterministic extractive fallback so the daily run can continue."
            )
            digest = extractive_digest(item, transcript or item.get("description", ""))
            digest["quality"] = "deterministic_due_validation_failure"
            write_digest_cache(digest_cache_dir, args.date, item, digest)
        item_digests.append((item, digest))

    report = build_report(args.date, item_digests)
    report["generation"] = {
        "mode": "llm_evidence_validated" if llm_configured() else "deterministic_fallback",
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
