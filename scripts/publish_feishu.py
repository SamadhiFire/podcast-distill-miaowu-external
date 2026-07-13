#!/usr/bin/env python3
"""Publish a Markdown daily report to Feishu Wiki and notify a group bot."""

from __future__ import annotations

import argparse
from html import escape
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests

try:
    from report_contract import enrich_report_from_legacy_markdown, report_to_feishu_xml
except ModuleNotFoundError:  # Imported as scripts.publish_feishu in tests/tools.
    from scripts.report_contract import enrich_report_from_legacy_markdown, report_to_feishu_xml


FEISHU_API = "https://open.feishu.cn/open-apis"
DEFAULT_PINNED_WIKI_TITLE = "🎧 播客蒸馏室"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--doc-token", help="overwrite an existing Feishu document instead of creating a new one")
    parser.add_argument("--node-token", help="existing Wiki node token, used for the notification URL")
    parser.add_argument("--wiki-node-token", help="overwrite an existing Wiki node by resolving its obj_token")
    parser.add_argument("--wiki-url", help="overwrite an existing Wiki page URL, for example https://my.feishu.cn/wiki/...")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--cleanup-old",
        action="store_true",
        help="delete older daily-report wiki nodes after publishing",
    )
    return parser.parse_args()


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_tenant_access_token() -> str:
    resp = requests.post(
        f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
        json={
            "app_id": required_env("FEISHU_APP_ID"),
            "app_secret": required_env("FEISHU_APP_SECRET"),
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu token error: {data}")
    return data["tenant_access_token"]


def feishu_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}


def create_wiki_doc(token: str, title: str) -> tuple[str, str | None]:
    space_id = required_env("FEISHU_WIKI_SPACE_ID")
    body: dict[str, Any] = {
        "obj_type": "docx",
        "node_type": "origin",
        "title": title,
    }
    body["parent_node_token"] = get_report_parent_node_token(token)
    resp = requests.post(
        f"{FEISHU_API}/wiki/v2/spaces/{space_id}/nodes",
        headers=feishu_headers(token),
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu create wiki node error: {data}")
    node = data.get("data", {}).get("node", {})
    doc_token = node.get("obj_token") or node.get("token")
    node_token = node.get("node_token")
    if not doc_token:
        raise RuntimeError(f"Cannot find document token in Feishu response: {data}")
    return doc_token, node_token


def extract_wiki_node_token(value: str) -> str:
    """Accept either a raw Wiki node token or a Feishu/Lark Wiki URL."""
    candidate = (value or "").strip()
    if not candidate:
        return ""
    match = re.search(r"/wiki/([^/?#]+)", candidate)
    if match:
        return match.group(1)
    return candidate


def get_wiki_doc_token(token: str, node_token: str) -> tuple[str, str]:
    """Resolve a Wiki node token to the underlying Docx obj_token."""
    space_id = required_env("FEISHU_WIKI_SPACE_ID")
    data = feishu_json_request(
        "GET",
        f"/wiki/v2/spaces/{space_id}/nodes/{node_token}",
        token,
    )
    node = data.get("data", {}).get("node", {})
    obj_type = str(node.get("obj_type") or "").lower()
    doc_token = node.get("obj_token") or node.get("token")
    if obj_type and obj_type != "docx":
        raise RuntimeError(f"Wiki node is {obj_type}, not docx: {node_token}")
    if not doc_token:
        raise RuntimeError(f"Cannot resolve docx obj_token for Wiki node: {data}")
    return str(doc_token), str(node.get("node_token") or node_token)


INLINE_TOKEN_RE = re.compile(r"(\*\*.+?\*\*|https?://[^\s<>]+)")
ORDERED_RE = re.compile(r"^\s*\d+\.\s+(.+)$")
UNORDERED_RE = re.compile(r"^\s*[-*]\s+(.+)$")


def _inline_markdown_to_xml(text: str) -> str:
    """Convert the small inline Markdown subset used by reports to XML."""
    text = text.replace("\\$", "$")
    parts: list[str] = []
    cursor = 0
    for match in INLINE_TOKEN_RE.finditer(text):
        parts.append(escape(text[cursor : match.start()], quote=False))
        token = match.group(0)
        if token.startswith("**"):
            parts.append(f"<b>{escape(token[2:-2], quote=False)}</b>")
        else:
            href = escape(token, quote=True)
            parts.append(f'<a href="{href}">{escape(token, quote=False)}</a>')
        cursor = match.end()
    parts.append(escape(text[cursor:], quote=False))
    return "".join(parts)


def _parse_table_row(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _is_table_separator(row: str) -> bool:
    cells = _parse_table_row(row)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _markdown_table_to_xml(lines: list[str]) -> str:
    if len(lines) < 2 or not _is_table_separator(lines[1]):
        return "\n".join(f"<p>{_inline_markdown_to_xml(line)}</p>" for line in lines)
    xml = ["<table>", "<thead><tr>"]
    for cell in _parse_table_row(lines[0]):
        xml.append(f'<th background-color="light-gray">{_inline_markdown_to_xml(cell)}</th>')
    xml += ["</tr></thead>", "<tbody>"]
    for row in lines[2:]:
        xml.append("<tr>")
        for cell in _parse_table_row(row):
            xml.append(f"<td>{_inline_markdown_to_xml(cell)}</td>")
        xml.append("</tr>")
    xml += ["</tbody>", "</table>"]
    return "\n".join(xml)


def _consume_list(lines: list[str], start: int, ordered: bool) -> tuple[str, int]:
    matcher = ORDERED_RE if ordered else UNORDERED_RE
    tag = "ol" if ordered else "ul"
    items: list[str] = []
    i = start
    while i < len(lines):
        match = matcher.match(lines[i])
        if not match:
            break
        chunks = [match.group(1).strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not matcher.match(lines[i]):
            if lines[i].startswith((" ", "\t")):
                chunks.append(lines[i].strip())
                i += 1
            else:
                break
        body = "<br/>".join(_inline_markdown_to_xml(chunk) for chunk in chunks)
        seq = ' seq="auto"' if ordered else ""
        items.append(f"<li{seq}>{body}</li>")
        while i < len(lines) and not lines[i].strip():
            # A blank line ends the current list unless another item follows.
            next_i = i + 1
            while next_i < len(lines) and not lines[next_i].strip():
                next_i += 1
            if next_i < len(lines) and matcher.match(lines[next_i]):
                i = next_i
            else:
                break
        if i >= len(lines) or not matcher.match(lines[i]):
            break
    return f"<{tag}>\n" + "\n".join(items) + f"\n</{tag}>", i


def markdown_to_feishu_xml(markdown: str) -> str:
    """Render report Markdown as strict lark-doc XML.

    The previous implementation mixed raw Markdown inside XML callouts, which
    produced literal tags, flattened sections and inconsistent lists in Feishu.
    """
    lines = markdown.replace("\r\n", "\n").splitlines()
    if lines and re.match(r"^# \d{4}-\d{2}-\d{2} .+日报\s*$", lines[0]):
        lines = lines[1:]

    result: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped == "---":
            result.append("<hr/>")
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            level = min(len(heading.group(1)), 6)
            result.append(f"<h{level}>{_inline_markdown_to_xml(heading.group(2))}</h{level}>")
            i += 1
            continue
        if stripped.startswith("> "):
            quotes: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                quotes.append(lines[i].strip()[2:])
                i += 1
            result.append(
                "<blockquote>" + "<br/>".join(_inline_markdown_to_xml(q) for q in quotes) + "</blockquote>"
            )
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            table: list[str] = []
            while i < len(lines):
                row = lines[i].strip()
                if not (row.startswith("|") and row.endswith("|")):
                    break
                table.append(row)
                i += 1
            result.append(_markdown_table_to_xml(table))
            continue
        if ORDERED_RE.match(lines[i]):
            xml, i = _consume_list(lines, i, ordered=True)
            result.append(xml)
            continue
        if UNORDERED_RE.match(lines[i]):
            xml, i = _consume_list(lines, i, ordered=False)
            result.append(xml)
            continue
        result.append(f"<p>{_inline_markdown_to_xml(stripped)}</p>")
        i += 1

    xml = "\n".join(result)
    ET.fromstring(f"<root>{xml}</root>")
    return xml


def assert_no_encoding_damage(text: str, label: str) -> None:
    """Refuse to publish text that looks like mojibake or lossy encoding."""
    if "\ufffd" in text:
        raise ValueError(f"{label} contains Unicode replacement characters")
    if re.search(r"\?{4,}", text):
        raise ValueError(f"{label} contains long runs of question marks; possible encoding damage")


def feishu_json_request(
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Call a Feishu OpenAPI JSON endpoint with small CI-friendly retries."""
    url = f"{FEISHU_API}{path}"
    for attempt in range(4):
        try:
            resp = requests.request(
                method,
                url,
                headers=feishu_headers(token),
                params=params,
                json=body,
                timeout=timeout,
            )
            if resp.status_code in {429, 500, 502, 503, 504}:
                resp.raise_for_status()
            resp.raise_for_status()
            data = resp.json()
            if data.get("code", 0) != 0:
                raise RuntimeError(f"Feishu API error {method} {path}: {data}")
            return data
        except (requests.RequestException, ValueError, RuntimeError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Feishu API request failed without response: {method} {path}")


def write_doc_via_openapi(
    token: str,
    doc_token: str,
    xml_content: str,
    command: str = "append",
) -> None:
    """Import strict lark-doc XML through the same OpenAPI path used by lark-cli."""
    body: dict[str, Any] = {
        "format": "xml",
        "content": xml_content,
        "revision_id": -1,
    }
    if command == "append":
        body["command"] = "block_insert_after"
        body["block_id"] = "-1"
    else:
        body["command"] = command

    feishu_json_request(
        "PUT",
        f"/docs_ai/v1/documents/{doc_token}",
        token,
        body=body,
        timeout=120,
    )
    print(f"Feishu Docs AI imported {len(xml_content)} XML char(s) to doc {doc_token} with {command}")


def notify(title: str, url: str, summary: str) -> None:
    webhook = os.getenv("FEISHU_NOTIFY_WEBHOOK")
    if not webhook:
        return

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": summary},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看完整日报"},
                            "url": url,
                            "type": "primary",
                        }
                    ],
                },
            ],
        },
    }
    resp = requests.post(
        webhook,
        json=card,
        timeout=30,
    )
    resp.raise_for_status()


def list_root_nodes(token: str) -> list[dict[str, Any]]:
    """List all top-level nodes (no parent) in the wiki space."""
    space_id = required_env("FEISHU_WIKI_SPACE_ID")
    all_nodes: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 50}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(
            f"{FEISHU_API}/wiki/v2/spaces/{space_id}/nodes",
            headers=feishu_headers(token),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu list nodes error: {data}")
        items = data.get("data", {}).get("items") or []
        all_nodes.extend(items)
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break
    return all_nodes


def get_report_parent_node_token(token: str) -> str:
    """Resolve the Wiki hub used as the parent of newly published reports.

    An explicit token takes priority for deployments where the hub's title is
    customized. This lookup does not move or reorder any existing Wiki node.
    """
    explicit_parent = os.getenv("FEISHU_PARENT_NODE_TOKEN")
    if explicit_parent:
        return explicit_parent

    parent_title = os.getenv(
        "FEISHU_PARENT_WIKI_TITLE",
        os.getenv("FEISHU_PINNED_WIKI_TITLE", DEFAULT_PINNED_WIKI_TITLE),
    )
    nodes = list_root_nodes(token)
    parent = next((node for node in nodes if node.get("title") == parent_title), None)
    parent_token = str((parent or {}).get("node_token") or "")
    if not parent_token:
        raise RuntimeError(
            f"Report parent Wiki page not found: {parent_title}. "
            "Set FEISHU_PARENT_NODE_TOKEN to its node token if it has a different title."
        )
    return parent_token


def delete_wiki_node(token: str, node_token: str) -> bool:
    """Delete a wiki node. Returns True on success."""
    space_id = required_env("FEISHU_WIKI_SPACE_ID")
    try:
        resp = requests.delete(
            f"{FEISHU_API}/wiki/v2/spaces/{space_id}/nodes/{node_token}",
            headers=feishu_headers(token),
            timeout=30,
        )
        data = resp.json()
        return data.get("code") == 0
    except Exception:
        return False


def update_wiki_node_title(token: str, node_token: str, title: str) -> None:
    """Update the wiki node title to match the document title."""
    space_id = required_env("FEISHU_WIKI_SPACE_ID")
    resp = requests.post(
        f"{FEISHU_API}/wiki/v2/spaces/{space_id}/nodes/{node_token}/update_title",
        headers=feishu_headers(token),
        json={"title": title},
        timeout=30,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return
    code = data.get("code", data.get("StatusCode", 0))
    if code != 0:
        raise RuntimeError(f"Feishu update node title error: {data}")


def build_notify_summary(markdown: str) -> str:
    """Extract the top highlights from the markdown report for the webhook card."""
    lines = markdown.splitlines()
    top_items: list[str] = []
    in_top = False
    for line in lines:
        if line.startswith("# 本日最值得关注的内容"):
            in_top = True
            continue
        if in_top:
            if line.startswith("# ") or line.startswith("## "):
                break
            stripped = line.strip()
            if stripped and re.match(r"^\d+\.", stripped):
                top_items.append(stripped)
    if not top_items:
        return "今日日报已生成，点击下方按钮查看完整内容。"
    return "**今日精选：**\n" + "\n".join(top_items[:5])


def build_notify_summary_from_report(report: dict[str, Any]) -> str:
    items = report.get("items", [])
    lines: list[str] = []
    for rank, idx in enumerate(report.get("top_items", [])[:3], 1):
        if isinstance(idx, int) and 0 <= idx < len(items):
            item = items[idx]
            lines.append(f"{rank}. **{item.get('short_title', '')}**：{item.get('one_liner', '')}")
    if not lines:
        return "今日日报已生成，点击下方按钮查看完整内容。"
    return "**3 分钟速览：**\n" + "\n".join(lines)


def cleanup_old_daily_reports(token: str, current_title: str) -> int:
    nodes = list_root_nodes(token)
    deleted = 0
    for node in nodes:
        title = node.get("title", "")
        node_token = node.get("node_token", "")
        if not title or not node_token:
            continue
        # Delete nodes that look like old daily reports
        is_old_report = (
            "播客/视频更新日报" in title or
            title.startswith("日报 ") or
            title == "DEBUG" or title == "DEBUG2" or
            title.startswith("TEST_")
        )
        # But keep the current report and 播客蒸馏室
        if is_old_report and title != current_title and title != "播客蒸馏室":
            print(f"  Deleting old node: {title}")
            if delete_wiki_node(token, node_token):
                deleted += 1
    return deleted


def main() -> int:
    args = parse_args()
    source_path = Path(args.file)
    source_text = source_path.read_text(encoding="utf-8")
    assert_no_encoding_damage(source_text, str(source_path))
    report: dict[str, Any] | None = None
    report_path = source_path if source_path.suffix.lower() == ".json" else source_path.with_suffix(".json")
    if report_path.exists():
        try:
            candidate = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and candidate.get("schema_version") == 2:
                report = candidate
        except (OSError, ValueError):
            report = None
    if report and source_path.suffix.lower() != ".json":
        report = enrich_report_from_legacy_markdown(report, source_text)
    if report:
        report["title"] = args.title
    xml_content = report_to_feishu_xml(report) if report else markdown_to_feishu_xml(source_text)
    assert_no_encoding_damage(xml_content, "rendered Feishu XML")
    if args.dry_run or not os.getenv("FEISHU_APP_ID"):
        print(f"Dry run: would publish {source_path} as {args.title} ({len(xml_content)} XML chars)")
        url = f"https://my.feishu.cn/wiki/DRY_RUN"
        summary = "日报处于 dry-run 模式，飞书知识库未实际更新。"
        notify(args.title, url, summary)
        return 0

    try:
        token = get_tenant_access_token()
        if args.doc_token:
            document_id = args.doc_token
            node_token = args.node_token
            publish_document_id = document_id
            command = "overwrite"
        elif args.wiki_url or args.wiki_node_token:
            requested_node_token = extract_wiki_node_token(args.wiki_url or args.wiki_node_token)
            if not requested_node_token:
                raise RuntimeError("--wiki-url or --wiki-node-token did not contain a Wiki node token")
            document_id, node_token = get_wiki_doc_token(token, requested_node_token)
            update_wiki_node_title(token, node_token, args.title)
            publish_document_id = node_token
            command = "overwrite"
        else:
            document_id, node_token = create_wiki_doc(token, args.title)
            publish_document_id = node_token or document_id
            command = "overwrite"
        write_doc_via_openapi(token, publish_document_id, xml_content, command=command)
        if args.cleanup_old:
            deleted = cleanup_old_daily_reports(token, args.title)
            print(f"Cleaned up {deleted} old report node(s)")
        url = f"https://my.feishu.cn/wiki/{node_token}" if node_token else ""
        summary = build_notify_summary_from_report(report) if report else build_notify_summary(source_text)
        notify(args.title, url, summary)
        print(f"Published to Feishu Wiki: document={document_id} node={node_token}")
        return 0
    except Exception as exc:
        url = ""
        summary = f"发布失败：{exc}"
        try:
            notify(args.title, url, summary)
        except Exception as notify_exc:
            print(f"Feishu failure notification also failed: {notify_exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
