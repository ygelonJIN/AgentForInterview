#!/usr/bin/env python3
"""Build the human HTML and AI-readable flow artifacts from one graph source.

Usage:
    python3 scripts/build_orchestration_flow.py
    python3 scripts/build_orchestration_flow.py --check
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SOURCE = DOCS / "orchestration-flow.graph.json"
TEMPLATE = DOCS / "orchestration-flow.template.html"
HTML_OUT = DOCS / "orchestration-flow-review.html"
AI_JSON_OUT = DOCS / "orchestration-flow.ai.json"
AI_MD_OUT = DOCS / "orchestration-flow.ai.md"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_issue_details(issues: list[dict]) -> str:
    labels = {"high": "高", "medium": "中", "low": "低"}
    rows = []
    for issue in issues:
        rows.append(
            '<details class="issue"><summary>'
            f'<span class="priority {esc(issue["priority"])}">{labels[issue["priority"]]}</span>'
            f'<strong>#{esc(issue["id"])} {esc(issue["title"])}</strong></summary>'
            f'<div class="detail">{esc(issue["detail"])}'
            f'<div class="path">{esc(issue["path"])}</div></div></details>'
        )
    return "\n".join(rows)


def render_route_rows(mapping: list[dict]) -> str:
    return "\n".join(
        '<tr>'
        f'<td>{esc(item["priority"])}</td>'
        f'<td><code>{esc(item["condition"])}</code></td>'
        f'<td>{esc(item["node"])}</td>'
        f'<td>{"否" if not item["model_involved"] else "是"}</td>'
        '</tr>'
        for item in mapping
    )


def render_audit_rows(items: list[str]) -> str:
    return "\n".join(f'<tr><td>{esc(index)}</td><td>{esc(item)}</td></tr>' for index, item in enumerate(items, 1))


def render_evidence_rows(items: list[dict]) -> str:
    return "\n".join(
        f'<tr><td>{esc(item["area"])}</td><td>{esc(item["path"])}</td></tr>'
        for item in items
    )


def render_markdown(data: dict) -> str:
    graph = data["graph"]
    route = data["routeTruth"]
    nodes = graph["nodes"]
    edges = graph["edges"]
    node_by_id = {node["id"]: node for node in nodes}
    out = [
        "# SmartLife Agent 流程图 AI 可读版本",
        "",
        "> 本文件由 `scripts/build_orchestration_flow.py` 从 `orchestration-flow.graph.json` 自动生成。不要单独手改本文件。",
        "",
        "## 图摘要",
        "",
        f"- 节点数：{len(nodes)}",
        f"- 连线数：{len(edges)}",
        f"- 根节点：`{graph['rootId']}` {node_by_id[graph['rootId']]['title']}",
        f"- 路由汇合点：`{graph['routeRootId']}`",
        f"- 共同收尾点：`{graph['tailRootId']}`",
        "",
        "## LangGraph 条件路由",
        "",
        f"- 实现方式：`{route['implementation']}`",
        f"- 是否调用模型：`{'是' if route['is_model'] else '否'}`",
        f"- 函数：`{route['function']}`",
        f"- 证据：`{route['source']}`",
        "",
        "|优先级|条件|节点|模型参与|",
        "|---:|---|---|---|",
    ]
    for item in route["mapping"]:
        out.append(f"|{item['priority']}|`{item['condition']}`|{item['node']}|{'是' if item['model_involved'] else '否'}|")
    out += ["", "## 节点", "", "|ID|类型|标题|说明|问题|父节点|子节点|进入边|", "|---|---|---|---|---:|---|---|---|"]
    for node in nodes:
        out.append(
            f"|`{node['id']}`|{node['kind']}|{node['title']}|{node['text']}|"
            f"{node.get('issue') or ''}|{','.join(node.get('parents', [])) or '-'}|"
            f"{','.join(node.get('childrenIds', [])) or '-'}|{node.get('edgeLabel') or ''}|"
        )
    out += ["", "## 连线", "", "|来源|目标|标签|类型|", "|---|---|---|---|"]
    for edge in edges:
        out.append(f"|`{edge['from']}`|`{edge['to']}`|{edge.get('label') or ''}|{edge['kind']}|")
    out += ["", "## 当前问题", "", "|编号|优先级|标题|说明|代码位置|", "|---:|---|---|---|---|"]
    for issue in data["issues"]:
        out.append(f"|#{issue['id']}|{issue['priority']}|{issue['title']}|{issue['detail']}|`{issue['path']}`|")
    out += ["", "## 内容审计", "", "### 补充的重要内容"]
    out += [f"- {item}" for item in data["contentAudit"]["added"]]
    out += ["", "### 降级或不放进主树的内容"]
    out += [f"- {item}" for item in data["contentAudit"]["secondary"]]
    out += ["", "## 代码证据", "", "|流程|绝对路径|", "|---|---|"]
    out += [f"|{item['area']}|`{item['path']}`|" for item in data["codeEvidence"]]
    out += ["", "## 修改方式", "", "1. 只修改 `docs/orchestration-flow.graph.json`。", "2. 运行 `python3 scripts/build_orchestration_flow.py`。", "3. HTML、AI JSON、AI Markdown 会同步更新。"]
    return "\n".join(out) + "\n"


def build() -> dict[str, str]:
    data = json.loads(SOURCE.read_text())
    required = {"graph", "issues", "routeTruth", "contentAudit", "codeEvidence"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"graph source missing keys: {sorted(missing)}")
    template = TEMPLATE.read_text()
    graph_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html_out = (
        template
        .replace("__FLOW_GRAPH_JSON__", graph_json)
        .replace("__ISSUE_DETAILS__", render_issue_details(data["issues"]))
        .replace("__ROUTE_TABLE_ROWS__", render_route_rows(data["routeTruth"]["mapping"]))
        .replace("__AUDIT_ADDED_ROWS__", render_audit_rows(data["contentAudit"]["added"]))
        .replace("__AUDIT_SECONDARY_ROWS__", render_audit_rows(data["contentAudit"]["secondary"]))
        .replace("__EVIDENCE_ROWS__", render_evidence_rows(data["codeEvidence"]))
    )
    ai_json = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return {
        str(HTML_OUT): html_out,
        str(AI_JSON_OUT): ai_json,
        str(AI_MD_OUT): render_markdown(data),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated artifacts are synchronized")
    args = parser.parse_args()
    generated = build()
    if args.check:
        stale = []
        for path, content in generated.items():
            current = Path(path).read_text() if Path(path).exists() else ""
            if current != content:
                stale.append(path)
        if stale:
            print("stale generated artifacts:")
            print("\n".join(stale))
            return 1
        print("flow artifacts are synchronized")
        return 0
    for path, content in generated.items():
        Path(path).write_text(content)
    print("generated:")
    print("\n".join(generated))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
