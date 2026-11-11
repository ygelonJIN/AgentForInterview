#!/usr/bin/env python3
"""从源码生成「全链路规格」：多层图 + 提示词。

只读 AST，不 import 业务模块：不触发 .env、模型客户端、数据库连接。

自动抽取：
- 所有 ``build_xxx_graph()`` 里的 add_node / add_edge / add_conditional_edges
- 所有 ``ChatPromptTemplate.from_messages([...])`` 的完整提示词
- 节点函数、常量表、阈值、源码

用法：
    python3 scripts/build_pipeline_spec.py
    python3 scripts/build_pipeline_spec.py --check
"""

from __future__ import annotations

import argparse
import ast
import html
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
# 文档统一放在工作区根的 docs/，不再往代码目录里写生成物。
_DOCS = Path(__file__).resolve().parents[2] / "docs" / "generated"
DEFAULT_HTML = str(_DOCS / "pipeline-spec.html")
DEFAULT_JSON = str(_DOCS / "pipeline-spec.json")

SCAN_PATTERNS = [
    "app/classifier.py",
    "app/llm_json.py",
    "app/graphs/*.py",
    "app/graphs/subgraphs/*.py",
    "app/agents/*.py",
    "app/services/*.py",
    "app/memory/*.py",
    "app/retrieval/*.py",
    "app/trip_gear/*.py",
]

STEP_LABELS = {
    "if": "判定", "elif": "判定", "for": "循环", "while": "循环",
    "try": "保护", "except": "异常", "finally": "收尾",
    "return": "返回", "raise": "抛出", "assign": "赋值",
    "with": "上下文",
}

GRAPH_TITLES = {
    "turn": "顶层主图",
    "memory": "记忆提取",
    "shopping": "购物",
    "travel": "旅行（无装备行程图）",
    "travel_plan": "旅行规划",
    "trip_gear": "装备挂载",
    "trip_gear_plan": "行程 + 装备协同",
    "general": "通用 Agent",
    "customer_service": "客服",
}

NODE_LABELS = {
    "__start__": "START",
    "__end__": "END",
    "prepare": "准备",
    "classify": "统一分类",
    "shopping": "购物",
    "travel": "旅行",
    "trip_gear": "装备",
    "general": "通用",
    "customer_service": "客服",
    "finalize": "收束",
    "extract_memory": "记忆提取",
    "plan_retrieval": "检索规划",
    "retrieve": "检索",
    "generate": "生成",
    "validate": "校验",
    "build_context": "构建上下文",
    "extract_requirements": "需求抽取",
    "clarify": "澄清",
    "build_steps": "拆步骤",
    "execute_steps": "执行步骤",
    "make_plan": "制定计划",
    "reflect": "反思",
    "revise": "修订",
    "derive": "约束派生",
    "plan": "逐日行程",
    "gear": "装备推导",
    "owned": "已有判定",
    "shop": "缺口检索",
    "verify": "校验",
    "repair": "修复",
    "load_orders": "加载订单",
    "synthesize": "合并",
    "agent": "Agent 循环",
    "tools": "工具执行",
    "travel_plan": "规划子图",
    "shopping_branch": "购物分支",
    "travel_branch": "旅行分支",
    "extract": "提取",
}


FIELD_NOTES = {
    "user_message": "本轮用户原话，不截断",
    "user_id": "用户标识，用于长期记忆与个性化",
    "thread_id": "会话线程 ID，短期记忆和检查点按它索引",
    "constraints": "SharedConstraints 的 dict 形态：预算、人数、天数、开始日期、目的地、补充要求",
    "constraints_text": "把 constraints 渲染成提示词用的条目文本，例如「- 预算: 3000 元」",
    "original_request": "父图透传的原始请求；没有时退回 user_message",
    "context": "拼好的背景文本：长期记忆 + 本地攻略 + 用户偏好，供规划器使用",
    "chat_history": "本 thread 的对话历史，给旅行服务做上下文",
    "classification": "分类结果 dict：route / intents / sub_intent / retrieval / confidence",
    "needs": "数据需求：是否需要商品事实、是否需要评价",
    "planned_strategy": "检索策略：sql_only / rag_only / mixed / none",
    "retrieval": "检索结果：SQL 行、RAG 文档、实际使用的策略",
    "response": "本轮最终回答文本",
    "current_plan": "当前版本的行程 Markdown",
    "final_plan": "去掉修订注记后的定稿",
    "reflection": "反思结果：是否满意、还剩哪些问题",
    "revision_count": "已修订次数",
    "plan_versions": "每次修订后的版本快照",
    "max_revisions": "修订次数上限",
    "requirements": "从原话抽出的需求：目的地、天数、预算等",
    "clarifications": "还缺哪些必填项（目的地 / 天数）",
    "evidence_results": "证据步骤的执行结果（攻略、天气、交通）",
    "evidence_context": "把证据结果拼成的文本",
    "branch_results": "并行分支的结果列表，按 name 归并",
    "memory_extraction": "候选记忆列表 + diagnostic",
    "agent_used": "本轮实际使用的 Agent 标签",
    "messages": "图内消息序列，供 ReAct 等子图读取",
    "status": "节点或本轮的状态标记",
    "error": "节点或本轮的错误信息",
    "evidence_steps": "证据步骤列表：攻略、天气、交通",
    "excluded_memories": "本轮不参与记忆提取的内容",
    "facts": "行程事实：目的地、天数、天气、住宿",
    "gear": "装备清单：名称、数量、为什么带",
    "history": "对话历史",
    "history_text": "把历史渲染成的文本",
    "issues": "校验发现的问题列表",
    "itinerary": "生成的逐日行程",
    "max_repairs": "修复次数上限",
    "max_steps": "ReAct 最大步数",
    "orders": "用户订单列表",
    "owned": "用户已经拥有的装备",
    "owned_ambiguous": "无法确认是否已有的装备",
    "plan": "行程计划",
    "products": "检索到的商品",
    "repair_options": "可替换的降级选项",
    "revision": "修订版本",
    "scene": "场景键，默认 general",
    "session_id": "当前会话 ID",
    "step3_emitted": "第三阶段事件是否已经发过",
    "step_count": "已执行步数",
    "suggestions": "建议列表",
    "tool_results": "工具调用结果",
    "tool_timeout": "单个工具的超时时间",
    "trace_id": "本轮链路 ID",
    "validation": "校验结果",
}

NODE_NOTES = {
    "plan_retrieval": "先判断这次需不需要商品事实、需不需要评价，再定 SQL / RAG / 混合策略。",
    "retrieve": "按策略并发取 SQL 与 RAG，把两边结果和实际策略合并成 retrieval。",
    "generate": "带着检索证据和共享约束生成回答；模型失败会重试一次。",
    "validate": "检查回答是否与证据一致；不一致就带校验反馈重新生成一次。",
    "build_context": "取出原始请求、历史与长期记忆，拼成规划器用的背景文本；同时把 constraints 渲染成 constraints_text。",
    "extract_requirements": "从原话里抽目的地、天数、预算等；缺目的地或天数就写进 clarifications。",
    "clarify": "缺必填项就先发澄清问题并结束本轮，等用户补充。",
    "build_steps": "把需求拆成若干证据步骤：本地攻略、天气、交通等。",
    "execute_steps": "逐步执行证据步骤，得到 evidence_results 并拼成 evidence_context。",
    "make_plan": "用背景、证据和约束生成逐日行程。",
    "reflect": "对照约束检查行程，产出 reflection（是否满意、还剩哪些问题）。",
    "revise": "按反思意见修订行程，revision_count 加一。",
    "finalize": "去掉修订注记，输出 final_plan 和 response。",
}

PARENT_SOURCES = {
    "user_message": "用户输入",
    "user_id": "用户输入",
    "scene": "用户输入",
    "thread_id": "顶层主图 / 准备",
    "constraints": "顶层主图 / 准备",
    "classification": "顶层主图 / 分类",
    "excluded_memories": "用户输入",
    "messages": "顶层主图 / 准备",
}

PROMPT_LINKS: Dict[Tuple[str, str], List[str]] = {
    ("turn", "classify"): ["_CLASSIFY_PROMPT", "_CLASSIFY_ARBITER_PROMPT"],
}


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def short(node: Optional[ast.AST], limit: int = 110) -> str:
    if node is None:
        return ""
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover
        return "…"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def literal_value(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def is_builder_call(node: ast.AST) -> Optional[str]:
    """build_xxx_graph() -> graph id。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name.startswith("build_") and name.endswith("_graph"):
            return name[len("build_"):-len("_graph")]
    return None


def graph_id_of(builder_name: str) -> str:
    return builder_name[len("build_"):-len("_graph")]


# ---------------------------------------------------------------------------
# 抽取：常量 / 函数 / 提示词
# ---------------------------------------------------------------------------


def extract_constants(tree: ast.Module) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        name = targets[0].id
        value = node.value
        item: Dict[str, Any] = {"name": name, "lineno": node.lineno}

        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
            base = value.func.value
            if isinstance(base, ast.Name) and base.id == "re" and value.func.attr == "compile":
                pattern = literal_value(value.args[0]) if value.args else None
                item.update({"kind": "regex", "pattern": pattern or short(value)})
                items.append(item)
                continue

        if isinstance(value, ast.Dict):
            entries = []
            for key, val in zip(value.keys, value.values):
                if key is None:
                    continue
                entries.append({
                    "key": literal_value(key) if literal_value(key) is not None else short(key),
                    "value": literal_value(val) if literal_value(val) is not None else short(val),
                })
            item.update({"kind": "dict", "size": len(entries), "entries": entries})
            items.append(item)
            continue

        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            entries = [
                literal_value(elt) if literal_value(elt) is not None else short(elt)
                for elt in value.elts
            ]
            kind = {ast.List: "list", ast.Tuple: "tuple", ast.Set: "set"}[type(value)]
            item.update({"kind": kind, "size": len(entries), "entries": entries})
            items.append(item)
            continue

        if isinstance(value, ast.Constant) and isinstance(value.value, (int, float, str, bool)):
            item.update({"kind": "scalar", "value": value.value})
            items.append(item)
    return items


def summarize_one(st: ast.stmt) -> str:
    if isinstance(st, ast.Return):
        return f"return {short(st.value, 70)}"
    if isinstance(st, ast.Assign) and len(st.targets) == 1:
        return f"{short(st.targets[0], 50)} = {short(st.value, 55)}"
    if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
        return short(st.value, 70)
    if isinstance(st, ast.Raise):
        return f"raise {short(st.exc, 50)}"
    if isinstance(st, (ast.If, ast.For, ast.While)):
        return "分支/循环"
    return ""


def summarize(body: List[ast.stmt]) -> str:
    if not body:
        return ""
    if len(body) == 1:
        return summarize_one(body[0]) or f"{len(body)} 条语句"
    first = summarize_one(body[0])
    return f"{first} 等 {len(body)} 条" if first else f"{len(body)} 条语句"


def walking_steps(stmts: Iterable[ast.stmt], depth: int = 0, branch: str = "") -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    for st in stmts:
        if isinstance(st, ast.If):
            steps.append({
                "kind": "elif" if branch == "elif" else "if",
                "lineno": st.lineno, "depth": depth, "branch": branch,
                "test": short(st.test, 170), "action": summarize(st.body),
            })
            steps.extend(walking_steps(st.body, depth + 1, ""))
            if st.orelse:
                if len(st.orelse) == 1 and isinstance(st.orelse[0], ast.If):
                    steps.extend(walking_steps(st.orelse, depth, "elif"))
                else:
                    steps.extend(walking_steps(st.orelse, depth + 1, "else"))
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            steps.append({
                "kind": "for", "lineno": st.lineno, "depth": depth, "branch": branch,
                "test": f"{short(st.target)} in {short(st.iter, 70)}", "action": "",
            })
            steps.extend(walking_steps(st.body, depth + 1, ""))
        elif isinstance(st, ast.While):
            steps.append({
                "kind": "while", "lineno": st.lineno, "depth": depth, "branch": branch,
                "test": short(st.test), "action": "",
            })
            steps.extend(walking_steps(st.body, depth + 1, ""))
        elif isinstance(st, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            steps.append({"kind": "try", "lineno": st.lineno, "depth": depth, "branch": branch})
            steps.extend(walking_steps(st.body, depth + 1, ""))
            for handler in st.handlers:
                steps.append({
                    "kind": "except", "lineno": handler.lineno, "depth": depth + 1,
                    "branch": branch, "test": short(handler.type) if handler.type else "Exception",
                })
                steps.extend(walking_steps(handler.body, depth + 2, ""))
            if st.orelse:
                steps.extend(walking_steps(st.orelse, depth + 1, "else"))
            if st.finalbody:
                steps.append({"kind": "finally", "lineno": st.lineno, "depth": depth, "branch": branch})
                steps.extend(walking_steps(st.finalbody, depth + 1, ""))
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            steps.append({
                "kind": "with", "lineno": st.lineno, "depth": depth, "branch": branch,
                "test": ", ".join(short(item.context_expr, 50) for item in st.items),
            })
            steps.extend(walking_steps(st.body, depth + 1, ""))
        elif isinstance(st, ast.Return):
            steps.append({
                "kind": "return", "lineno": st.lineno, "depth": depth, "branch": branch,
                "test": short(st.value, 90) if st.value is not None else "",
            })
        elif isinstance(st, ast.Raise):
            steps.append({
                "kind": "raise", "lineno": st.lineno, "depth": depth, "branch": branch,
                "test": short(st.exc, 70) if st.exc else "",
            })
        elif isinstance(st, (ast.Assign, ast.AnnAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                steps.append({
                    "kind": "assign", "lineno": st.lineno, "depth": depth, "branch": branch,
                    "test": f"{targets[0].id} = {short(st.value, 80)}",
                })
    return steps


def thresholds_in(node: ast.AST, scalars: Dict[str, Any]) -> List[Dict[str, Any]]:
    found: Dict[Tuple[Any, str], Dict[str, Any]] = {}
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Compare):
            continue
        for comparator in sub.comparators:
            value = None
            label = ""
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, (int, float)):
                value, label = comparator.value, short(comparator)
            elif isinstance(comparator, ast.Name) and comparator.id in scalars:
                value, label = scalars[comparator.id], comparator.id
            if value is None:
                continue
            key = (value, label)
            if key in found:
                found[key]["count"] += 1
            else:
                found[key] = {"value": value, "label": label, "count": 1}
    return sorted(found.values(), key=lambda item: (str(item["value"]), item["label"]))


def state_io(fn: ast.AST) -> Tuple[List[str], List[str], Dict[str, str]]:
    reads: Set[str] = set()
    writes: Set[str] = set()
    exprs: Dict[str, str] = {}
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == "get" and isinstance(sub.func.value, ast.Name) \
                and sub.func.value.id == "state" and sub.args:
            arg = sub.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                reads.add(arg.value)
        elif isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) \
                and sub.value.id == "state":
            key = sub.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                reads.add(key.value)
        elif isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
            for key, value in zip(sub.value.keys, sub.value.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if isinstance(value, ast.Constant) and value.value == "":
                        continue  # 清空字段不进“写”列表，避免误导
                    writes.add(key.value)
                    exprs[key.value] = short(value, 70)
    return sorted(reads), sorted(writes), exprs


def extract_function(fn: ast.AST, src: str, qualname: str, owner: str,
                     const_names: Set[str], scalars: Dict[str, Any]) -> Dict[str, Any]:
    doc = ast.get_docstring(fn) or ""
    args = []
    all_args = list(getattr(fn.args, "posonlyargs", [])) + list(fn.args.args)
    defaults = list(fn.args.defaults)
    offset = len(all_args) - len(defaults)
    for index, arg in enumerate(all_args):
        if arg.arg in {"self", "cls"}:
            continue
        default = short(defaults[index - offset], 40) if index >= offset else ""
        ann = short(arg.annotation, 40) if arg.annotation else ""
        args.append({"name": arg.arg, "annotation": ann, "default": default})

    refs = sorted({
        sub.id for sub in ast.walk(fn)
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id in const_names
    })
    calls = sorted({
        sub.func.id if isinstance(sub.func, ast.Name) else sub.func.attr
        for sub in ast.walk(fn) if isinstance(sub, ast.Call)
        and isinstance(sub.func, (ast.Name, ast.Attribute))
    })
    steps = walking_steps(fn.body)
    start = min([fn.lineno] + [item.lineno for item in getattr(fn, "decorator_list", [])])
    source = "\n".join(src.splitlines()[start - 1: fn.end_lineno])

    reads, writes, write_exprs = state_io(fn)
    locals_map: Dict[str, str] = {}
    for st in ast.walk(fn):
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            locals_map.setdefault(st.targets[0].id, short(st.value, 90))

    return {
        "qualname": qualname, "name": fn.name, "owner": owner,
        "reads": reads, "writes": writes, "write_exprs": write_exprs,
        "locals": locals_map,
        "lineno": fn.lineno, "end_lineno": fn.end_lineno, "doc": doc,
        "args": args, "is_async": isinstance(fn, ast.AsyncFunctionDef),
        "steps": steps, "refs": refs, "calls": calls,
        "thresholds": thresholds_in(fn, scalars),
        "source": source, "source_start": start,
    }


def extract_model(cls: ast.ClassDef) -> Optional[Dict[str, Any]]:
    bases = [short(base, 40) for base in cls.bases]
    if not any("BaseModel" in base or "TypedDict" in base for base in bases):
        return None
    fields = []
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            description = ""
            default = ""
            if isinstance(node.value, ast.Call):
                kwargs = {kw.arg: kw.value for kw in node.value.keywords if kw.arg}
                if "description" in kwargs:
                    description = literal_value(kwargs["description"]) or ""
                if "default" in kwargs:
                    default = short(kwargs["default"], 30)
            fields.append({
                "name": node.target.id,
                "annotation": short(node.annotation, 60),
                "description": description, "default": default,
            })
    return {"name": cls.name, "fields": fields, "lineno": cls.lineno}


def extract_prompts(tree: ast.Module, src: str) -> List[Dict[str, Any]]:
    """所有 ChatPromptTemplate.from_messages([...]) 的完整消息。"""
    prompts: List[Dict[str, Any]] = []

    def is_from_messages(call: ast.Call) -> bool:
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "from_messages":
            return False
        base = func.value
        if isinstance(base, ast.Name):
            return base.id.endswith("ChatPromptTemplate") or base.id == "PROMPT"
        if isinstance(base, ast.Attribute):
            return base.attr.endswith("ChatPromptTemplate")
        return False

    def parse_messages(arg: ast.AST) -> List[Dict[str, str]]:
        if not isinstance(arg, (ast.List, ast.Tuple)):
            return []
        messages = []
        for elt in arg.elts:
            if isinstance(elt, ast.Tuple) and len(elt.elts) == 2:
                role_node, content = elt.elts
                role = literal_value(role_node)
                if not isinstance(role, str):
                    role = short(role_node, 20)
                if isinstance(content, ast.Constant) and isinstance(content.value, str):
                    text = content.value
                elif isinstance(content, ast.JoinedStr):
                    text = short(content, 4000)
                else:
                    text = short(content, 200)
                messages.append({"role": role, "text": text})
            elif isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name) \
                    and elt.func.id == "MessagesPlaceholder":
                name = literal_value(elt.args[0]) if elt.args else "?"
                messages.append({"role": "placeholder", "text": f"{{{name}}}"})
        return messages

    def walk(node: ast.AST, owner: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            next_owner = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                next_owner = child.name
            elif isinstance(child, ast.ClassDef):
                next_owner = child.name
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                value = child.value
                if isinstance(value, ast.Call) and is_from_messages(value):
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                    name = ""
                    for target in targets:
                        if isinstance(target, ast.Name):
                            name = target.id
                        elif isinstance(target, ast.Attribute):
                            name = target.attr
                    messages = parse_messages(value.args[0]) if value.args else []
                    prompts.append({
                        "name": name or f"prompt@{child.lineno}",
                        "owner": owner,
                        "lineno": child.lineno,
                        "messages": messages,
                    })
            walk(child, next_owner)

    walk(tree)
    return prompts


def extract_imports(tree: ast.Module) -> Dict[str, str]:
    imports: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                imports[local] = node.module.replace(".", "/") + ".py"
    return imports


# ---------------------------------------------------------------------------
# 扫描模块
# ---------------------------------------------------------------------------


def scan_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception as exc:  # pragma: no cover
        print(f"[warn] 跳过 {path}: {exc}", file=sys.stderr)
        return None

    constants = extract_constants(tree)
    const_names = {item["name"] for item in constants}
    scalars = {item["name"]: item["value"] for item in constants if item["kind"] == "scalar"}

    functions: List[Dict[str, Any]] = []
    classes: List[Dict[str, Any]] = []
    models: List[Dict[str, Any]] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(extract_function(node, src, node.name, "", const_names, scalars))
        elif isinstance(node, ast.ClassDef):
            model = extract_model(node)
            if model:
                models.append(model)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append(
                        extract_function(item, src, f"{node.name}.{item.name}", node.name, const_names, scalars)
                    )

    prompts = extract_prompts(tree, src)
    for prompt in prompts:
        prompt["module"] = str(path.relative_to(ROOT))

    return {
        "path": str(path.relative_to(ROOT)),
        "src": src,
        "tree": tree,
        "lineno": len(src.splitlines()),
        "constants": constants,
        "functions": functions,
        "models": models,
        "prompts": prompts,
        "imports": extract_imports(tree),
    }


def scan_modules() -> Dict[str, Dict[str, Any]]:
    modules: Dict[str, Dict[str, Any]] = {}
    for pattern in SCAN_PATTERNS:
        for path in sorted(ROOT.glob(pattern)):
            if path.name == "__init__.py" or "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(ROOT))
            if rel in modules:
                continue
            module = scan_file(path)
            if module:
                modules[rel] = module
    return modules


# ---------------------------------------------------------------------------
# 解析图
# ---------------------------------------------------------------------------


def norm_endpoint(node: ast.AST) -> str:
    if isinstance(node, ast.Name) and node.id == "START":
        return "__start__"
    if isinstance(node, ast.Name) and node.id == "END":
        return "__end__"
    value = literal_value(node)
    return str(value) if value is not None else short(node)


def dict_mapping(node: ast.AST, module: Dict[str, Any]) -> Dict[str, str]:
    seen: Dict[str, str] = {}
    if isinstance(node, ast.Dict):
        for key, val in zip(node.keys, node.values):
            if key is None:
                continue
            seen[str(literal_value(key))] = norm_endpoint(val)
        return seen
    if isinstance(node, ast.Name):
        for candidate in module["tree"].body:
            if isinstance(candidate, ast.Assign) and len(candidate.targets) == 1:
                target = candidate.targets[0]
                if isinstance(target, ast.Name) and target.id == node.id and isinstance(candidate.value, ast.Dict):
                    return dict_mapping(candidate.value, module)
    return seen


def find_dict_constant(name: str, module: Dict[str, Any]) -> Optional[ast.Dict]:
    for candidate in module["tree"].body:
        if isinstance(candidate, ast.Assign) and len(candidate.targets) == 1:
            target = candidate.targets[0]
            if isinstance(target, ast.Name) and target.id == name and isinstance(candidate.value, ast.Dict):
                return candidate.value
    return None


def resolve_iterable(node: ast.AST, module: Dict[str, Any]) -> List[str]:
    """支持 for scene_node in ROUTE_TARGETS.values() 这类循环。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr in {"values", "keys"} and isinstance(node.func.value, ast.Name):
        table = find_dict_constant(node.func.value.id, module)
        if table:
            items = table.values if node.func.attr == "values" else table.keys
            return [str(literal_value(item)) for item in items if literal_value(item) is not None]
    if isinstance(node, ast.Name):
        table = find_dict_constant(node.id, module)
        if table:
            return [str(literal_value(item)) for item in table.keys if literal_value(item) is not None]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [str(literal_value(item)) for item in node.elts if literal_value(item) is not None]
    return []


def parse_agent_specs(modules: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """从 app/agents/specs.py 的 AST 读 agent_id → 子图 id。

    只读 AST、不 import 业务模块；AgentSpec 的 builder 是
    ``"模块:函数"`` 字符串，所以能静态拿到节点名。
    """
    for module in modules.values():
        path = str(module.get("path") or "")
        if not path.endswith("agents/specs.py"):
            continue
        mapping: Dict[str, str] = {}
        for node in ast.walk(module["tree"]):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AgentSpec"
            ):
                continue
            kwargs = {
                kw.arg: literal_value(kw.value)
                for kw in node.keywords
                if kw.arg
            }
            agent_id = kwargs.get("agent_id")
            builder = kwargs.get("builder")
            if isinstance(agent_id, str) and isinstance(builder, str) and ":" in builder:
                mapping[agent_id] = graph_id_of(str(builder).split(":")[-1])
        return mapping
    return {}


def parse_graph(fn: ast.AST, module: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    graph_id = graph_id_of(fn.name)
    local_builders: Dict[str, str] = {}
    graph_vars: Set[str] = set()

    for stmt in fn.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name = stmt.targets[0].id
            value = stmt.value
            builder = is_builder_call(value)
            if builder:
                local_builders[name] = builder
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                    and value.func.id == "StateGraph":
                graph_vars.add(name)

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    def resolve_endpoint(node: ast.AST, env: Dict[str, str]) -> Optional[str]:
        if isinstance(node, ast.Name) and node.id in env:
            return env[node.id]
        # spec.agent_id 这种属性访问由 Agent 清单注入，不在 AST 里解析，
        # 返回 None 而不是把 "spec.agent_id" 当成节点名。
        if isinstance(node, ast.Attribute) or (isinstance(node, ast.Call)):
            return None
        value = norm_endpoint(node)
        return value

    def handle_call(call: ast.Call, env: Dict[str, str]) -> None:
        func = call.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return
        if func.value.id not in graph_vars:
            return
        attr = func.attr
        if attr == "add_node" and len(call.args) >= 2:
            name = literal_value(call.args[0])
            if not isinstance(name, str):
                return
            target = call.args[1]
            node: Dict[str, Any] = {"id": name, "fn": "", "subgraph": ""}
            builder = is_builder_call(target)
            if builder:
                node["subgraph"] = builder
            elif isinstance(target, ast.Name):
                if target.id in local_builders:
                    node["subgraph"] = local_builders[target.id]
                else:
                    node["fn"] = target.id
            nodes[name] = node
        elif attr == "add_edge" and len(call.args) >= 2:
            src = resolve_endpoint(call.args[0], env)
            dst = resolve_endpoint(call.args[1], env)
            if src and dst:
                edges.append({"src": src, "dst": dst, "label": "", "conditional": False})
        elif attr == "add_conditional_edges" and len(call.args) >= 2:
            src = resolve_endpoint(call.args[0], env)
            mapping = dict_mapping(call.args[2], module) if len(call.args) >= 3 else {}
            if not src:
                return
            for label, dst in mapping.items():
                edges.append({"src": src, "dst": dst, "label": label, "conditional": True})

    def walk(stmts: List[ast.stmt], env: Dict[str, str]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.For):
                target = stmt.target.id if isinstance(stmt.target, ast.Name) else None
                values = resolve_iterable(stmt.iter, module)
                if target and values:
                    for value in values:
                        walk(stmt.body, dict(env, **{target: value}))
                else:
                    walk(stmt.body, env)
                walk(stmt.orelse, env)
            elif isinstance(stmt, ast.If):
                walk(stmt.body, env)
                walk(stmt.orelse, env)
            elif isinstance(stmt, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                walk(stmt.body, env)
                for handler in stmt.handlers:
                    walk(handler.body, env)
                walk(stmt.orelse, env)
                walk(stmt.finalbody, env)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk(stmt.body, env)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                handle_call(stmt.value, env)

    walk(fn.body, {})

    if graph_id == "turn":
        # 顶层图的 Agent 节点由 specs.py 派生，AST 里看不到 add_node 字面量，
        # 这里按清单补回节点与两条边（classify → agent → finalize）。
        for agent_id, subgraph in (module.get("agent_specs") or {}).items():
            nodes.setdefault(agent_id, {"id": agent_id, "fn": "", "subgraph": subgraph})
            edges.append({"src": "classify", "dst": agent_id, "label": agent_id, "conditional": True})
            edges.append({"src": agent_id, "dst": "finalize", "label": "", "conditional": False})

    if not nodes:
        return None
    return {
        "id": graph_id,
        "module": module["path"],
        "lineno": fn.lineno,
        "nodes": nodes,
        "edges": edges,
    }


def parse_graphs(modules: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    agent_specs = parse_agent_specs(modules)
    for module in modules.values():
        module["agent_specs"] = agent_specs
    graphs: Dict[str, Dict[str, Any]] = {}
    for module in modules.values():
        for fn in module["tree"].body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (fn.name.startswith("build_") and fn.name.endswith("_graph")):
                continue
            parsed = parse_graph(fn, module)
            if parsed:
                graphs[parsed["id"]] = parsed
    return graphs


# ---------------------------------------------------------------------------
# 布局
# ---------------------------------------------------------------------------


NODE_W, NODE_H = 232, 72
TERM_W, TERM_H = 88, 34
MAX_COLS, GAP_X, GAP_Y = 4, 28, 46


def compute_layout(graph: Dict[str, Any]) -> Dict[str, Any]:
    node_ids = set(graph["nodes"])
    edges = list(graph["edges"])
    if any(edge["src"] == "__start__" for edge in edges):
        node_ids.add("__start__")
    if any(edge["dst"] == "__end__" for edge in edges):
        node_ids.add("__end__")

    sizes: Dict[str, Tuple[int, int]] = {}
    for node_id in node_ids:
        sizes[node_id] = (TERM_W, TERM_H) if node_id in {"__start__", "__end__"} else (NODE_W, NODE_H)

    adj: Dict[str, List[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        adj.setdefault(edge["src"], []).append(edge["dst"])
    state = {node_id: 0 for node_id in node_ids}
    back: Set[Tuple[str, str]] = set()

    def dfs(node: str) -> None:
        state[node] = 1
        for nxt in adj.get(node, []):
            if nxt not in state:
                continue
            if state[nxt] == 1:
                back.add((node, nxt))
            elif state[nxt] == 0:
                dfs(nxt)
        state[node] = 2

    starts: List[str] = []
    if "__start__" in node_ids:
        starts.append("__start__")
    starts += sorted(n for n in node_ids if n not in {"__start__"} and not any(e["dst"] == n for e in edges))
    starts += sorted(n for n in node_ids if n not in starts)
    for node_id in starts:
        if state.get(node_id, 0) == 0:
            dfs(node_id)

    forward = [edge for edge in edges if (edge["src"], edge["dst"]) not in back]
    indeg = {node_id: 0 for node_id in node_ids}
    out: Dict[str, List[str]] = {node_id: [] for node_id in node_ids}
    for edge in forward:
        indeg[edge["dst"]] = indeg.get(edge["dst"], 0) + 1
        out.setdefault(edge["src"], []).append(edge["dst"])

    layer = {node_id: 0 for node_id in node_ids}
    queue = [node_id for node_id in node_ids if indeg.get(node_id, 0) == 0]
    remaining = dict(indeg)
    guard = 0
    while queue and guard < 1000:
        guard += 1
        node = queue.pop(0)
        for nxt in out.get(node, []):
            layer[nxt] = max(layer.get(nxt, 0), layer[node] + 1)
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                queue.append(nxt)

    layers: Dict[int, List[str]] = {}
    for node_id in node_ids:
        layers.setdefault(layer.get(node_id, 0), []).append(node_id)
    insertion = {node_id: index for index, node_id in enumerate(graph["nodes"])}
    for items in layers.values():
        items.sort(key=lambda item: (
            item != "__start__", item != "__end__", insertion.get(item, 999)
        ))

    rows_by_layer: Dict[int, List[List[str]]] = {}
    layer_h: Dict[int, int] = {}
    for depth, items in layers.items():
        rows = [items[index:index + MAX_COLS] for index in range(0, len(items), MAX_COLS)]
        rows_by_layer[depth] = rows
        layer_h[depth] = (
            sum(max(sizes[node][1] for node in row) for row in rows)
            + GAP_Y * max(0, len(rows) - 1)
        )

    row_widths = [
        sum(sizes[node][0] for node in row) + GAP_X * max(0, len(row) - 1)
        for rows in rows_by_layer.values() for row in rows
    ]
    canvas_w = int(max((max(row_widths) if row_widths else NODE_W) + 180, 640))
    canvas_h = int(sum(layer_h.values()) + GAP_Y * max(0, len(layers) - 1) + 100)

    positions: Dict[str, Dict[str, Any]] = {}
    y = 50.0
    for depth in sorted(layers):
        for row in rows_by_layer[depth]:
            row_w = sum(sizes[node][0] for node in row) + GAP_X * max(0, len(row) - 1)
            row_h = max(sizes[node][1] for node in row)
            x = (canvas_w - row_w) / 2
            for node in row:
                width, height = sizes[node]
                positions[node] = {
                    "x": round(x, 1),
                    "y": round(y + (row_h - height) / 2, 1),
                    "w": width,
                    "h": height,
                }
                x += width + GAP_X
            y += row_h + GAP_Y

    return {
        "positions": positions,
        "canvas_w": canvas_w,
        "canvas_h": canvas_h,
        "back": back,
        "layers": layer,
    }


def edge_geometry(src: Dict[str, Any], dst: Dict[str, Any], back: bool,
                  canvas_w: int, skip: bool = False) -> Dict[str, Any]:
    if skip and not back:
        x1, y1 = src["x"], src["y"] + src["h"] / 2
        x2, y2 = dst["x"], dst["y"] + dst["h"] / 2
        lane = 26
        path = (f"M {x1:.1f} {y1:.1f} L {lane:.1f} {y1:.1f} "
                f"L {lane:.1f} {y2:.1f} L {x2:.1f} {y2:.1f}")
        return {"path": path, "label_x": lane + 40, "label_y": (y1 + y2) / 2}
    if not back:
        x1, y1 = src["x"] + src["w"] / 2, src["y"] + src["h"]
        x2, y2 = dst["x"] + dst["w"] / 2, dst["y"]
        off = min(64, max(22, (y2 - y1) / 2)) if y2 > y1 else 28
        p1, p2 = (x1, y1 + off), (x2, y2 - off)
        path = f"M {x1:.1f} {y1:.1f} C {p1[0]:.1f} {p1[1]:.1f}, {p2[0]:.1f} {p2[1]:.1f}, {x2:.1f} {y2:.1f}"
        # 标签贴近目标端，避免所有分支的标签在同一个高度排成一条虚线
        t = 0.82
        mt = 1 - t
        label_x = (mt ** 3) * x1 + 3 * (mt ** 2) * t * p1[0] + 3 * mt * (t ** 2) * p2[0] + (t ** 3) * x2
        label_y = (mt ** 3) * y1 + 3 * (mt ** 2) * t * p1[1] + 3 * mt * (t ** 2) * p2[1] + (t ** 3) * y2
    else:
        x1, y1 = src["x"] + src["w"], src["y"] + src["h"] / 2
        x2, y2 = dst["x"] + dst["w"], dst["y"] + dst["h"] / 2
        lane = canvas_w - 26
        path = (f"M {x1:.1f} {y1:.1f} L {lane:.1f} {y1:.1f} "
                f"L {lane:.1f} {y2:.1f} L {x2:.1f} {y2:.1f}")
        label_x = lane - 34
        label_y = (y1 + y2) / 2
    return {"path": path, "label_x": label_x, "label_y": label_y}


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_graph_svg(graph: Dict[str, Any], layout: Dict[str, Any]) -> str:
    positions = layout["positions"]
    paths, labels = [], []
    for edge in graph["edges"]:
        src, dst = positions.get(edge["src"]), positions.get(edge["dst"])
        if not src or not dst:
            continue
        is_back = (edge["src"], edge["dst"]) in layout["back"]
        layers = layout.get("layers", {})
        skip = (layers.get(edge["dst"], 0) - layers.get(edge["src"], 0)) >= 3
        geo = edge_geometry(src, dst, is_back, layout["canvas_w"], skip)
        cls = "edge back" if is_back else ("edge cond" if edge["conditional"] else "edge")
        paths.append(f'<path class="{cls}" d="{geo["path"]}" marker-end="url(#arrow)"/>')
        if edge["label"] and edge["label"] != edge["dst"] and edge["label"] not in {"__end__", "end", "done"}:
            labels.append(
                f'<span class="edge-label" style="left:{geo["label_x"]:.0f}px;top:{geo["label_y"]:.0f}px">'
                f'{esc(edge["label"])}</span>'
            )
    return (
        f'<svg class="edges" width="{layout["canvas_w"]}" height="{layout["canvas_h"]}" '
        f'viewBox="0 0 {layout["canvas_w"]} {layout["canvas_h"]}" aria-hidden="true">'
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#b6ae9c"/></marker></defs>'
        + "".join(paths) + "</svg>" + "".join(labels)
    )


def render_graph_nodes(graph: Dict[str, Any], layout: Dict[str, Any],
                       prompt_counts: Optional[Dict[str, int]] = None) -> str:
    prompt_counts = prompt_counts or {}
    parts = []
    for node_id, node in graph["nodes"].items():
        pos = layout["positions"].get(node_id)
        if not pos:
            continue
        kind = "sub" if node.get("subgraph") else "node"
        title = NODE_LABELS.get(node_id, node_id)
        sub = node.get("subgraph") and f"子图 · {node['subgraph']}" or node.get("fn", "")
        parts.append(
            f'<button class="node kind-{kind}" data-graph="{esc(graph["id"])}" '
            f'data-id="{esc(node_id)}" data-sub="{esc(node.get("subgraph", ""))}" '
            f'style="left:{pos["x"]}px;top:{pos["y"]}px;width:{pos["w"]}px;height:{pos["h"]}px">'
            f'<span class="node-top"><span class="badge">{esc("子图" if kind == "sub" else "节点")}</span>'
            + (f'<span class="pbadge">P{prompt_counts[node_id]}</span>' if prompt_counts.get(node_id) else "")
            + '<span class="chev">›</span></span>'
            f'<span class="node-title">{esc(title)}</span>'
            f'<span class="node-sub">{esc(sub)}</span></button>'
        )
    for term in ("__start__", "__end__"):
        if term not in layout["positions"]:
            continue
        pos = layout["positions"][term]
        parts.append(
            f'<span class="terminal" style="left:{pos["x"]}px;top:{pos["y"]}px;'
            f'width:{pos["w"]}px;height:{pos["h"]}px">{esc("START" if term == "__start__" else "END")}</span>'
        )
    return "".join(parts)


def render_graph_section(graph: Dict[str, Any], active: bool,
                         prompt_counts: Optional[Dict[str, int]] = None) -> Tuple[str, str]:
    layout = compute_layout(graph)
    section = (
        f'<section class="graph{" active" if active else ""}" data-graph="{esc(graph["id"])}">'
        f'<div class="graph-meta"><code>{esc(graph["module"])}:{graph["lineno"]}</code>'
        f'<span class="muted">{len(graph["nodes"])} 节点 · {len(graph["edges"])} 条边</span></div>'
        f'<div class="canvas-scroll"><div class="canvas" style="width:{layout["canvas_w"]}px;'
        f'height:{layout["canvas_h"]}px">{render_graph_svg(graph, layout)}'
        f'{render_graph_nodes(graph, layout, prompt_counts)}</div></div></section>'
    )
    return section, layout


def render_steps(steps: List[Dict[str, Any]]) -> str:
    if not steps:
        return '<div class="muted">无可展开语句</div>'
    rows = []
    last_branch = ""
    for step in steps:
        kind = step["kind"]
        branch = step.get("branch", "")
        marker = '<span class="else">else</span>' if branch == "else" and last_branch != "else" else ""
        last_branch = branch
        test = step.get("test", "")
        action = step.get("action", "")
        body = f"<code>{esc(test)}</code>"
        if kind in {"if", "elif"} and action:
            body += f'<span class="arrow">→</span><span class="act">{esc(action)}</span>'
        rows.append(
            f'<tr class="{esc(kind)}"><td class="ln">L{step["lineno"]}</td>'
            f'<td class="flow" style="--d:{step.get("depth", 0)}">'
            f'{marker}<span class="kind">{esc(STEP_LABELS.get(kind, kind))}</span>{body}</td></tr>'
        )
    return f'<div class="steps"><table><tbody>{"".join(rows)}</tbody></table></div>'


def render_chips(values: Iterable[Any]) -> str:
    items = [f'<span class="chip">{esc(value)}</span>' for value in values]
    return '<div class="chips">' + "".join(items) + "</div>" if items else '<span class="muted">—</span>'


def render_constant(item: Dict[str, Any], with_head: bool = True) -> str:
    kind = item["kind"]
    if kind == "regex":
        body = f'<pre class="code regexp">{esc(item["pattern"])}</pre>'
    elif kind == "scalar":
        body = f'<div class="chips"><span class="chip num">{esc(item["value"])}</span></div>'
    elif kind == "dict":
        rows = []
        for entry in item["entries"]:
            value = entry["value"]
            cell = render_chips(value) if isinstance(value, list) else f'<span class="chip">{esc(value)}</span>'
            rows.append(f'<tr><td class="k">{esc(entry["key"])}</td><td>{cell}</td></tr>')
        body = ('<table class="tbl"><thead><tr><th style="width:120px">键</th><th>内容</th></tr></thead><tbody>'
                + "".join(rows) + "</tbody></table>")
    else:
        body = render_chips(item["entries"])
    head = (
        f'<div class="const-head"><code>{esc(item["name"])}</code>'
        f'<span class="muted">L{item["lineno"]} · {item.get("size", 1)} 项</span></div>'
        if with_head else ""
    )
    return f'<div class="const-block">{head}{body}</div>' 


def render_prompt(prompt: Dict[str, Any]) -> str:
    label = prompt["name"]
    if label == "prompt" and prompt.get("owner"):
        label = f'{prompt["owner"]}.prompt'
    messages = []
    for message in prompt["messages"]:
        text = esc(message["text"])
        text = re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", lambda m: f'<span class="ph">{m.group(0)}</span>', text)
        messages.append(
            f'<div class="msg"><span class="role {esc(message["role"])}">{esc(message["role"])}</span>'
            f'<pre class="prompt-text">{text}</pre></div>'
        )
    return (
        f'<div class="prompt-block"><div class="prompt-head"><code>{esc(label)}</code>'
        f'<span class="muted">{esc(prompt["module"])}:L{prompt["lineno"]}</span></div>'
        + "".join(messages) + "</div>"
    )


def resolve_fn(modules: Dict[str, Dict[str, Any]], graphs: Dict[str, Dict[str, Any]],
               fn_index: Dict[str, List[Tuple[str, Dict[str, Any]]]],
               graph_id: str, name: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    if not name:
        return None
    graph_module = graphs[graph_id]["module"]
    candidates = fn_index.get(name, [])
    for path, fn in candidates:
        if path == graph_module:
            return path, fn
    module = modules.get(graph_module, {})
    imported = module.get("imports", {}).get(name)
    if imported:
        for path, fn in candidates:
            if path == imported:
                return path, fn
    return candidates[0] if candidates else None


def node_prompt_count(modules: Dict[str, Dict[str, Any]], graphs: Dict[str, Dict[str, Any]],
                      fn_index: Dict[str, List[Tuple[str, Dict[str, Any]]]],
                      prompt_index: Dict[str, List[Dict[str, Any]]],
                      graph_id: str, node_id: str, node: Dict[str, Any]) -> int:
    if node.get("subgraph"):
        return 0
    seen = set()
    for name in PROMPT_LINKS.get((graph_id, node_id), []):
        for prompt in prompt_index.get(name, []):
            seen.add((prompt["module"], prompt["lineno"]))
    resolved = resolve_fn(modules, graphs, fn_index, graph_id, node.get("fn", ""))
    if resolved:
        path, _ = resolved
        for prompt in modules[path]["prompts"]:
            seen.add((prompt["module"], prompt["lineno"]))
    return len(seen)


def render_panels(modules: Dict[str, Dict[str, Any]], graphs: Dict[str, Dict[str, Any]],
                  fn_index: Dict[str, List[Tuple[str, Dict[str, Any]]]],
                  prompt_index: Dict[str, List[Dict[str, Any]]]) -> str:
    panels = []

    def resolve(graph_id: str, name: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        return resolve_fn(modules, graphs, fn_index, graph_id, name)

    producer_cache: Dict[str, Dict[str, List[Tuple[str, str]]]] = {}

    def producers_of(graph_id: str) -> Dict[str, List[Tuple[str, str]]]:
        if graph_id in producer_cache:
            return producer_cache[graph_id]
        result: Dict[str, List[Tuple[str, str]]] = {}
        for nid, item in graphs[graph_id]["nodes"].items():
            if item.get("subgraph"):
                continue
            resolved_item = resolve(graph_id, item.get("fn", ""))
            if not resolved_item:
                continue
            for key, expr in resolved_item[1].get("write_exprs", {}).items():
                result.setdefault(key, []).append((nid, expr))
        producer_cache[graph_id] = result
        return result

    for graph_id, graph in graphs.items():
        graph_producers = producers_of(graph_id)
        for node_id, node in graph["nodes"].items():
            panel_id = f"{graph_id}__{node_id}"
            title = NODE_LABELS.get(node_id, node_id)
            kind = "sub" if node.get("subgraph") else "node"
            parts = [
                f'<article class="panel" data-id="{esc(panel_id)}">',
                f'<div class="panel-head"><span class="badge kind-{kind}">'
                f'{"子图" if kind == "sub" else "节点"}</span><h3>{esc(title)}</h3></div>',
                f'<div class="panel-sub"><code>{esc(node_id)}</code></div>',
            ]
            if node.get("subgraph"):
                sub_id = node["subgraph"]
                sub_graph = graphs.get(sub_id)
                parts.append(
                    f'<p class="note">该节点挂载子图 <code>{esc(GRAPH_TITLES.get(sub_id, sub_id))}</code>'
                    f'{"（" + str(len(sub_graph["nodes"])) + " 个节点）" if sub_graph else ""}。</p>'
                    f'<button class="btn jump" data-jump="{esc(sub_id)}">进入子图 →</button>'
                )
            else:
                resolved = resolve(graph_id, node.get("fn", ""))
                if resolved:
                    path, fn = resolved
                    parts.append(
                        f'<div class="fn-head"><code>{esc(fn["qualname"])}</code>'
                        f'<span class="muted">{esc(path)} · L{fn["lineno"]} · {len(fn["steps"])} 步</span></div>'
                    )
                    if fn["doc"]:
                        parts.append(f'<p class="doc">{esc(fn["doc"])}</p>')
                    if fn.get("reads") or fn.get("writes"):
                        node_label = NODE_LABELS.get(node_id, node_id)

                        def field_rows(keys: List[str], kind: str) -> str:
                            rows = []
                            for key in keys:
                                where = ""
                                graph_title = GRAPH_TITLES.get(graph_id, graph_id)
                                if kind == "read":
                                    if key in graph_producers:
                                        names = [pid for pid, _ in graph_producers[key]]
                                        where = "本节点" if node_id in names else f"{graph_title} / " + "、".join(
                                            NODE_LABELS.get(pid, pid) for pid in names[:2]
                                        )
                                    else:
                                        where = PARENT_SOURCES.get(key, "")
                                else:
                                    where = f"{graph_title} / {node_label}"
                                rows.append(
                                    f'<div class="frow"><span class="field" data-key="{esc(key)}" '
                                    f'data-kind="{esc(kind)}" data-where="{esc(where)}">{esc(key)}</span>'
                                    f'<span class="fnote">{esc(FIELD_NOTES.get(key, ""))}</span></div>'
                                )
                            return "".join(rows)
                        parts.append('<div class="section-label">state 读写</div>')
                        if fn.get("reads"):
                            parts.append(f'<div class="fgroup"><span class="k">读</span><div>{field_rows(fn["reads"], "read")}</div></div>')
                        if fn.get("writes"):
                            parts.append(f'<div class="fgroup"><span class="k">写</span><div>{field_rows(fn["writes"], "write")}</div></div>')
                    parts.append(render_steps(fn["steps"]))
                    if fn["thresholds"]:
                        parts.append(
                            '<div class="row"><span class="label">阈值</span><div class="chips">'
                            + "".join(
                                f'<span class="chip num">{esc(item["value"])}'
                                f'{" ×" + str(item["count"]) if item["count"] > 1 else ""}</span>'
                                for item in fn["thresholds"]
                            ) + "</div></div>"
                        )
                    module = modules[path]
                    refs = [c for c in module["constants"] if c["name"] in set(fn["refs"])]
                    for item in refs:
                        parts.append(render_constant(item))
                    lines = fn["source"].splitlines()
                    numbered = "".join(
                        f'<span class="ln">{fn["source_start"] + index}</span>{esc(line)}\n'
                        for index, line in enumerate(lines)
                    )
                    parts.append(
                        f'<details><summary>源码原文 · {len(lines)} 行</summary>'
                        f'<pre class="code">{numbered}</pre></details>'
                    )

            linked = PROMPT_LINKS.get((graph_id, node_id), [])
            prompts = []
            for name in linked:
                prompts.extend(prompt_index.get(name, []))
            if not node.get("subgraph") and resolved:
                path, _ = resolved
                prompts.extend(
                    record for record in modules[path]["prompts"]
                    if not any(record is item for item in prompts)
                )
            seen_prompt = set()
            unique_prompts = []
            for item in prompts:
                key = (item["module"], item["lineno"], item["name"])
                if key not in seen_prompt:
                    seen_prompt.add(key)
                    unique_prompts.append(item)
            if unique_prompts:
                parts.append('<div class="section-label">提示词</div>')
                parts.extend(render_prompt(item) for item in unique_prompts)
            parts.append("</article>")
            panels.append("".join(parts))
    return "".join(panels)


def render_constants_panel(modules: Dict[str, Dict[str, Any]]) -> str:
    kinds = {"dict", "list", "tuple", "set", "regex"}
    total = sum(1 for module in modules.values() for item in module["constants"] if item["kind"] in kinds)
    parts = [
        '<article class="panel" data-id="__constants__">',
        '<div class="panel-head"><span class="badge kind-note">常量</span><h3>全部词表与规则表</h3></div>',
        f'<p class="note">共 {total} 张表/正则，按模块分组；点开看全量内容。关键词、替换词、约束、阈值都在这里。</p>',
    ]
    for path, module in modules.items():
        items = [item for item in module["constants"] if item["kind"] in kinds]
        if not items:
            continue
        parts.append(f'<div class="section-label">{esc(path)}</div>')
        for item in items:
            parts.append(
                f'<details class="const-fold"><summary><code>{esc(item["name"])}</code>'
                f'<span class="muted">{item["kind"]} · {item.get("size", 1)} 项</span></summary>'
                f'{render_constant(item, with_head=False)}</details>'
            )
    parts.append("</article>")
    return "".join(parts)


def render_prompts_panel(modules: Dict[str, Dict[str, Any]]) -> str:
    parts = [
        '<article class="panel" data-id="__prompts__">',
        '<div class="panel-head"><span class="badge kind-note">提示词</span><h3>全部提示词</h3></div>',
    ]
    total = 0
    for path, module in modules.items():
        if not module["prompts"]:
            continue
        parts.append(f'<div class="section-label">{esc(path)}</div>')
        for prompt in module["prompts"]:
            prompt = dict(prompt, module=path)
            total += 1
            parts.append(render_prompt(prompt))
    parts.append("</article>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


CSS = """
:root{
  --bg:#f7f5f0; --canvas:#fbfaf6; --ink:#1f2328; --muted:#6b7280; --line:#e5e1d7;
  --accent:#5b5bd6; --accent-soft:#eeeefb;
  --rule:#0f766e; --rule-soft:#e6f4f1;
  --model:#7c3aed; --model-soft:#f1e9fe;
  --return:#15803d; --return-soft:#e7f5ec;
  --warn:#a16207; --warn-soft:#fdf3d7;
  --error:#b42318; --error-soft:#fdeceb;
  --note:#475569; --note-soft:#eef2f6;
  --fix:#c2410c; --fix-soft:#fdece3;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;}
code{font-family:var(--mono)}
.wrap{max-width:1220px;margin:0 auto;padding:24px 24px 60px}
.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px}
h1{font-size:23px;margin:0;letter-spacing:-.01em}
.subtle{color:var(--muted);font-size:12.5px;margin-top:5px}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.search{padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;font-size:13px;outline:none;width:210px}
.search:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.btn{padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;font-size:12.5px;cursor:pointer;color:var(--ink)}
.btn:hover{border-color:#cfc8b8;background:#fdfcf9}
.tabs{display:flex;gap:6px;flex-wrap:nowrap;overflow-x:auto;margin:16px 0 6px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.tab{padding:6px 11px;white-space:nowrap;border:1px solid transparent;border-radius:999px;background:transparent;font-size:12.5px;
  color:#5b6270;cursor:pointer}
.tab:hover{background:#f1eee6}
.tab.active{background:#fff;border-color:#d9d3c4;color:var(--ink);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.tab .n{color:var(--muted);font-weight:400;font-size:11px;margin-left:4px}
.graph{display:none}
.graph.active{display:block}
.graph-meta{display:flex;gap:12px;align-items:baseline;margin:10px 0 2px;font-size:12px;color:var(--muted)}
.graph-meta code{color:#6b5f4d}
.canvas-scroll{overflow-x:auto;padding:4px 0}
.canvas{position:relative;margin:6px auto 0;background:
  radial-gradient(#e3dfd4 1px, transparent 1px) 0 0/22px 22px, var(--canvas);
  border:1px solid var(--line);border-radius:16px;
  box-shadow:0 1px 0 rgba(0,0,0,.02),0 10px 30px -18px rgba(40,40,30,.25)}
.edges{position:absolute;left:0;top:0;pointer-events:none}
.edge{fill:none;stroke:#bcb4a2;stroke-width:1.6}
.edge.cond{stroke:#a9a08a}
.edge.back{stroke:#b98b5e;stroke-dasharray:5 5}
.edge-label{position:absolute;transform:translate(-50%,-50%);background:#fffdf8;border:1px solid #e8e2d4;
  border-radius:999px;padding:1px 7px;font-size:10.5px;color:#6b6355;white-space:nowrap;
  box-shadow:0 1px 2px rgba(0,0,0,.04)}
.node{position:absolute;display:flex;flex-direction:column;gap:2px;text-align:left;padding:9px 12px 8px 14px;
  background:#fff;border:1px solid #ded8c9;border-left:4px solid #c9c2b1;border-radius:11px;cursor:pointer;
  font:inherit;color:inherit;box-shadow:0 1px 2px rgba(40,40,30,.06);
  transition:transform .12s ease,box-shadow .12s ease,border-color .12s ease}
.node:hover{transform:translateY(-1px);box-shadow:0 6px 18px -8px rgba(40,40,30,.35)}
.node.active{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft),0 8px 20px -10px rgba(40,40,30,.4)}
.node.dim{opacity:.2;filter:saturate(.3)}
.node.kind-node{border-left-color:var(--rule)}
.node.kind-sub{border-left-color:var(--model);background:#fdfcff}
.node-top{display:flex;align-items:center;justify-content:space-between}
.badge{display:inline-block;padding:0 6px;border-radius:999px;font-size:10px;font-weight:600;
  letter-spacing:.03em;background:#f1efe8;color:#6b7280;line-height:17px}
.node.kind-sub .badge{background:var(--model-soft);color:var(--model)}
.node.kind-node .badge{background:var(--rule-soft);color:var(--rule)}
.chev{color:#c5bdac;font-size:14px;line-height:1}
.pbadge{font-size:9px;font-weight:600;color:#7a5b00;background:#fff3c4;border-radius:999px;padding:0 5px;
  line-height:16px;margin-left:auto;margin-right:4px}
.node-title{font-size:13px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.node-sub{font-family:var(--mono);font-size:10px;color:#8a8273;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.terminal{position:absolute;display:flex;align-items:center;justify-content:center;border-radius:999px;
  background:#f1efe8;border:1px solid #ded8c9;color:#8a8273;font-size:10px;font-weight:700;letter-spacing:.08em}
.foot{margin-top:12px;font-size:12px;color:var(--muted)}
.foot code{background:#f1efe8;border-radius:4px;padding:1px 5px}
.scrim{position:fixed;inset:0;background:rgba(30,28,22,.28);opacity:0;pointer-events:none;transition:opacity .18s ease;z-index:40}
.scrim.show{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;height:100vh;width:min(660px,94vw);background:#fff;z-index:50;
  transform:translateX(102%);transition:transform .22s cubic-bezier(.22,.61,.36,1);
  box-shadow:-20px 0 60px -30px rgba(0,0,0,.4);display:flex;flex-direction:column}
.drawer.open{transform:translateX(0)}
.drawer-bar{display:flex;justify-content:space-between;align-items:center;padding:11px 16px;border-bottom:1px solid var(--line);background:#fbfaf6}
.drawer-bar .t{font-size:11.5px;letter-spacing:.08em;color:var(--muted);text-transform:uppercase}
.drawer-close{border:1px solid var(--line);background:#fff;border-radius:8px;width:30px;height:30px;cursor:pointer;color:#6b7280}
.drawer-close:hover{background:#f4f1ea}
.drawer-body{overflow:auto;padding:18px 20px 60px;flex:1}
.panel{display:none}
.panel.active{display:block}
.panel-head{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.panel-head h3{margin:0;font-size:17px;letter-spacing:-.01em}
.badge.kind-node{background:var(--rule-soft);color:var(--rule)}
.badge.kind-sub{background:var(--model-soft);color:var(--model)}
.badge.kind-note{background:var(--note-soft);color:var(--note)}
.panel-sub{margin:2px 0 8px}
.panel-sub code{font-size:11.5px;color:#6b5f4d;background:#f7f4ec;border-radius:5px;padding:1px 6px}
.note{color:#4b5563;font-size:12.5px;margin:8px 0 12px;padding:9px 11px;background:#faf8f2;border:1px solid #eee8da;border-radius:8px}
.btn.jump{margin:2px 0 10px;border-color:#d9d0fb;color:#5b3fd6;background:var(--accent-soft)}
.fn-head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:4px}
.fn-head code{font-size:12.5px;font-weight:600}
.muted{color:var(--muted);font-size:11px}
.doc{color:#3d4450;font-size:12px;margin:4px 0 8px}
.steps{border:1px solid var(--line);border-radius:9px;overflow:hidden;margin:8px 0}
.steps table{width:100%;border-collapse:collapse;font-size:11.5px}
.steps td{padding:4px 8px;border-bottom:1px solid #f2efe7;vertical-align:top}
.steps tr:last-child td{border-bottom:0}
.steps .ln{width:46px;color:#9aa0a6;font-family:var(--mono);font-size:10.5px;text-align:right;
  border-right:1px solid #f2efe7;white-space:nowrap}
.steps .flow{padding-left:calc(8px + var(--d,0) * 12px)}
.steps .kind{display:inline-block;width:38px;font-size:10px;font-weight:600;color:var(--muted)}
.steps tr.if .kind,.steps tr.elif .kind{color:var(--model)}
.steps tr.return .kind{color:var(--return)}
.steps tr.except .kind,.steps tr.raise .kind{color:var(--error)}
.steps tr.assign .kind{color:#b9b2a2}
.steps tr.assign code{color:#5f6670}
.steps code{font-size:11px;background:none;color:var(--ink);word-break:break-word}
.steps .arrow{color:#b7b2a5;margin:0 5px}
.steps .act{color:#5a6270}
.steps .else{font-size:9.5px;color:var(--warn);background:var(--warn-soft);border-radius:4px;padding:0 4px;margin-right:5px}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{font-family:var(--mono);font-size:11px;background:#f6f4ee;border:1px solid var(--line);
  border-radius:5px;padding:0 5px;color:#3d4450;white-space:nowrap}
.chip.num{background:var(--warn-soft);border-color:#f0e0b4;color:var(--warn);font-weight:600}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start;margin:6px 0}
.row .label{color:var(--muted);font-size:11px;min-width:36px;padding-top:4px}
.tbl{width:100%;border-collapse:collapse;font-size:11px;margin:4px 0 2px}
.tbl th{text-align:left;color:var(--muted);font-weight:600;font-size:10.5px;padding:4px 6px;
  border-bottom:1px solid var(--line);background:#fbfaf6}
.tbl td{padding:5px 6px;border-bottom:1px solid #f3f0e8;vertical-align:top}
.tbl tr:last-child td{border-bottom:0}
.tbl td.k{font-family:var(--mono);white-space:nowrap;color:var(--model)}
.const-block{border:1px solid var(--line);border-radius:9px;padding:9px 11px;margin:10px 0;background:#fff}
.const-head{display:flex;gap:10px;align-items:baseline;margin-bottom:1px}
.const-head code{font-size:12px;font-weight:600}
pre.code{margin:6px 0 0;background:#1f2328;color:#e6e6e6;border-radius:8px;padding:10px 12px;
  overflow:auto;font-size:11px;line-height:1.5;max-height:340px}
pre.code .ln{color:#6e7681;user-select:none;display:inline-block;width:30px;text-align:right;margin-right:9px}
pre.regexp{background:#2b2318;color:#f3d9a4;white-space:pre-wrap;word-break:break-all}
details{margin-top:6px}
details summary{cursor:pointer;color:var(--muted);font-size:11px}
details summary:hover{color:var(--ink)}
.const-fold{border:1px solid var(--line);border-radius:9px;margin:8px 0;background:#fff;padding:0 10px}
.const-fold summary{display:flex;gap:10px;align-items:baseline;padding:8px 0;cursor:pointer;font-size:12px}
.const-fold summary code{font-weight:600}
.const-fold .const-block{border:0;padding:0 0 4px;margin:0}
.section-label{margin:16px 0 6px;font-size:10.5px;letter-spacing:.08em;color:var(--muted);
  text-transform:uppercase;border-top:1px dashed var(--line);padding-top:12px}
.prompt-block{border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden;background:#fff}
.prompt-head{display:flex;gap:10px;align-items:baseline;padding:8px 11px;background:#fbfaf6;border-bottom:1px solid var(--line)}
.prompt-head code{font-size:12px;font-weight:600}
.msg{display:grid;grid-template-columns:64px 1fr;gap:8px;padding:9px 11px;border-bottom:1px solid #f3f0e8}
.msg:last-child{border-bottom:0}
.role{font-family:var(--mono);font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;
  color:#6b7280;background:#f1efe8;border-radius:4px;padding:2px 5px;height:18px;text-align:center}
.role.system{color:#5b3fd6;background:var(--accent-soft)}
.role.user{color:var(--rule);background:var(--rule-soft)}
.role.placeholder{color:var(--warn);background:var(--warn-soft)}
.prompt-text{margin:0;white-space:pre-wrap;word-break:break-word;font-family:var(--mono);
  font-size:11px;line-height:1.55;color:#2b3038;max-height:320px;overflow:auto}
.ph{background:#fff3c4;border-radius:3px;padding:0 2px;color:#7a5b00}


.scenario{max-width:1180px;margin:0 auto}
.table-tools{display:flex;gap:10px;align-items:center;margin:10px 0}
.flowtbl{width:100%;border-collapse:separate;border-spacing:0;background:#fff;
  border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:12.5px}
.flowtbl th{position:sticky;top:0;background:#fbfaf6;text-align:left;font-size:10.5px;
  color:var(--muted);padding:7px 9px;border-bottom:1px solid var(--line);z-index:2;letter-spacing:.04em}
.flowtbl td{padding:8px 10px;border-bottom:1px solid #f1eee6;vertical-align:top}
.flowtbl tbody tr:not(.branch):nth-child(4n+3){background:#fdfcf9}
.flowtbl tr:last-child td{border-bottom:0}
.flowtbl tr.branch{background:#fdfcf8}
.flowtbl tr.branch.hidden{display:none}
.flowtbl .c-num{width:38px}
.flowtbl .num{color:#9aa0a6;font-family:var(--mono);font-size:10.5px;text-align:right}
.flowtbl .c-step{width:160px}
.flowtbl .stepname{font-weight:600;white-space:nowrap}
.flowtbl .do{color:#4b5563}
.flowtbl .c-read,.flowtbl .c-write{width:24%}
.row-toggle{display:inline-flex;align-items:center;justify-content:center;
  border:1px solid var(--line);background:#fff;border-radius:5px;width:16px;height:16px;
  font-size:11px;line-height:1;cursor:pointer;color:#6b7280;padding:0;margin-right:6px;vertical-align:1px}
.row-toggle.open{background:#f1efe8;color:#111}
.detail-row td{background:#fbf9f2;padding:0 9px 9px}
.detail-inner{border:1px dashed #e3dccb;border-radius:8px;padding:10px 12px;font-size:12px}
.detail-title{font-size:10.5px;color:var(--muted);letter-spacing:.05em;margin-bottom:6px}
.rules-ol{margin:4px 0 6px 18px;padding:0}
.rules-ol li{margin:2px 0}
.field.flash{background:#ffe08a;box-shadow:0 0 0 2px #f0b429}

.story{display:flex;flex-direction:column;gap:11px}
.story-intro{background:#fbf8ef;border:1px solid #ece3cc;border-radius:10px;padding:11px 14px;
  font-size:12.5px;color:#5b5136}
.step{background:#fff;border:1px solid var(--line);border-left:4px solid #94a3b8;border-radius:11px;
  padding:12px 15px}
.step.lane-user{border-left-color:#64748b}
.step.lane-main{border-left-color:#94a3b8}
.step.lane-composite{border-left-color:var(--model)}
.step.lane-merge{border-left-color:var(--warn)}
.step-head{display:flex;align-items:baseline;gap:9px;margin-bottom:7px}
.step-no{width:20px;height:20px;border-radius:6px;background:#f1efe8;color:#6b7280;font-size:11px;
  display:inline-flex;align-items:center;justify-content:center;font-weight:700}
.step h4{margin:0;font-size:13.5px}
.tag-demo{font-size:10px;color:#a16207;background:#fdf3d7;border-radius:999px;padding:1px 7px}
.io{display:grid;grid-template-columns:64px minmax(0,1fr);gap:5px 10px;font-size:12px}
.io .k{color:var(--muted);font-size:11px;padding-top:2px;white-space:nowrap}
.io code{font-size:11.5px;background:#f6f4ee;border:1px solid var(--line);border-radius:5px;
  padding:1px 6px;margin:0 4px 3px 0;display:inline-block}
.io code.demo{background:#fff8e1;border-color:#efe0b0}
.branch-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.branch{border:1px solid var(--line);border-radius:11px;padding:10px 12px;background:#fdfcf9}
.branch.lane-shopping{border-top:3px solid var(--rule)}
.branch.lane-travel{border-top:3px solid #2563eb}
.branch-h{font-size:11px;color:var(--muted);letter-spacing:.04em;margin-bottom:6px}
.mini{display:block;width:100%;text-align:left;font:inherit;color:inherit;cursor:pointer;
  border:1px solid var(--line);border-radius:8px;padding:7px 9px;margin:6px 0;background:#fff}
.mini:hover{border-color:#cfc8b8;background:#fdfcf9}
.mini-h{display:flex;gap:7px;align-items:baseline;font-size:12px;font-weight:600}
.mini-h b{width:16px;height:16px;border-radius:50%;background:#f1efe8;color:#6b7280;font-size:10px;
  display:inline-flex;align-items:center;justify-content:center}
.mini-h code{font-family:var(--mono);font-size:10.5px;color:#8a8273;font-weight:400;margin-left:auto}
.mini-do{margin:4px 0 5px;font-size:11px;color:#5b6270;line-height:1.5}
.mini-io{margin-top:4px}
.fgroup{display:grid;grid-template-columns:16px minmax(0,1fr);gap:4px;margin:3px 0}
.fgroup .k{font-size:10px;color:var(--muted);padding-top:2px}
.frow{display:flex;gap:7px;align-items:baseline;font-size:10.5px;margin:1px 0}
.frow code{font-size:10px;background:#f6f4ee;border:1px solid var(--line);border-radius:4px;
  padding:0 4px;white-space:nowrap;color:#5b3fd6}
.frow span{color:#6b7280}
.frow .expr{color:#a16207;font-family:var(--mono);font-size:10px}
.field{font-family:var(--mono);font-size:11px;background:#f6f4ee;border:1px solid var(--line);
  border-radius:4px;padding:0 4px;cursor:help;transition:background .12s,box-shadow .12s}
.field.hl{background:#fff3c4;border-color:#e6c35c;box-shadow:0 0 0 2px rgba(230,195,92,.45)}
.fnote{color:#6b7280;font-size:10.5px}
.tip{position:fixed;z-index:95;max-width:560px;background:#23282f;color:#fff;font-size:11.5px;
  line-height:1.5;padding:6px 9px;border-radius:7px;pointer-events:none;opacity:0;
  transform:translateY(2px);transition:opacity .1s;box-shadow:0 8px 24px -8px rgba(0,0,0,.5)}
.tip.show{opacity:1}
.frow .src{color:#2563eb;font-size:9.5px;white-space:nowrap}
.mini-vars{margin-top:4px;display:grid;grid-template-columns:52px minmax(0,1fr);gap:4px}
.mini-vars .k{font-size:10px;color:var(--muted);padding-top:2px}
.mini .io{grid-template-columns:26px minmax(0,1fr);gap:2px 6px;font-size:11px;margin-top:4px}
.mini .io .k{font-size:10px}
.mini .io code{font-size:10.5px;margin:0 3px 2px 0}
pre.block{margin:6px 0;background:#1f2328;color:#dfe3e8;border-radius:8px;padding:10px 12px;
  overflow:auto;font-size:11px;line-height:1.55;max-height:260px}
.step .tbl{font-size:11px}
details.rules{margin-top:2px}
details.rules summary{font-size:11.5px;color:#5b6270}
details.rules .tbl{margin-top:6px}
@media (max-width:900px){.wrap{padding:16px 12px 50px}.drawer{width:100vw}.branch-grid{grid-template-columns:1fr}}
@media print{.drawer,.scrim,.toolbar,.tabs{display:none}.graph{display:block}}
"""

JS = """
(function(){
  var drawer = document.getElementById('drawer');
  var scrim = document.getElementById('scrim');
  var panels = Array.prototype.slice.call(document.querySelectorAll('.panel'));
  var nodes = Array.prototype.slice.call(document.querySelectorAll('.node'));
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
  var graphs = Array.prototype.slice.call(document.querySelectorAll('.graph'));

  function activate(id){
    tabs.forEach(function(t){ t.classList.toggle('active', t.dataset.graph === id); });
    graphs.forEach(function(g){ g.classList.toggle('active', g.dataset.graph === id); });
  }
  function open(id){
    panels.forEach(function(p){ p.classList.toggle('active', p.dataset.id === id); });
    drawer.classList.add('open'); scrim.classList.add('show');
    document.body.style.overflow = 'hidden';
    drawer.querySelector('.drawer-body').scrollTop = 0;
  }
  function close(){
    drawer.classList.remove('open'); scrim.classList.remove('show');
    document.body.style.overflow = '';
    nodes.forEach(function(n){ n.classList.remove('active'); });
  }
  tabs.forEach(function(t){ t.addEventListener('click', function(){ activate(t.dataset.graph); }); });
  nodes.forEach(function(n){
    n.addEventListener('click', function(){
      if(document.querySelector('.graph.active')) activate(n.dataset.graph);
      nodes.forEach(function(x){ x.classList.toggle('active', x === n); });
      open(n.dataset.graph + '__' + n.dataset.id);
    });
  });
  document.addEventListener('click', function(e){
    var mini = e.target.closest('[data-open]');
    if(mini){ open(mini.dataset.open); }
    var jump = e.target.closest('[data-jump]');
    if(jump){ activate(jump.dataset.jump); close(); window.scrollTo({top:0, behavior:'smooth'}); }
  });
  document.getElementById('prompts').addEventListener('click', function(){ open('__prompts__'); });
  document.getElementById('constants').addEventListener('click', function(){ open('__constants__'); });
  document.getElementById('close').addEventListener('click', close);
  scrim.addEventListener('click', close);
  document.addEventListener('keydown', function(e){ if(e.key === 'Escape') close(); });

  var tip = document.getElementById('tip');
  function moveTip(e){
    var x = e.clientX + 14, y = e.clientY + 16;
    if(x + 320 > window.innerWidth) x = window.innerWidth - 330;
    if(y + 60 > window.innerHeight) y = e.clientY - 44;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  document.addEventListener('mousemove', moveTip);
  document.addEventListener('mouseover', function(e){
    var f = e.target.closest('.field');
    if(!f) return;
    var key = f.dataset.key;
    var all = Array.prototype.slice.call(document.querySelectorAll('.field'))
      .filter(function(x){ return x.offsetParent !== null; });
    all.forEach(function(x){ if(x.dataset.key === key) x.classList.add('hl'); });
    var writers = [], readers = 0;
    all.forEach(function(x){
      if(x.dataset.key !== key) return;
      if(x.dataset.kind === 'read'){ readers += 1; return; }
      var w = x.dataset.where || '';
      if(w && writers.indexOf(w) === -1) writers.push(w);
    });
    var parts = [];
    if(writers.length){
      parts.push('写入：' + (writers.length > 3 ? writers.slice(0,3).join('、') + ' 等' : writers.join('、')));
    }
    if(readers) parts.push('读取 ' + readers + ' 处');
    tip.textContent = key + (parts.length ? '　' + parts.join('　') : '');
    tip.classList.add('show');
    moveTip(e);
  });
  document.addEventListener('mouseout', function(e){
    var f = e.target.closest('.field');
    if(!f) return;
    if(e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('.field') === f) return;
    Array.prototype.forEach.call(document.querySelectorAll('.field.hl'), function(x){ x.classList.remove('hl'); });
    tip.classList.remove('show');
  });


  var toggleBranch = document.getElementById('toggle-branch');
  if(toggleBranch){
    toggleBranch.addEventListener('click', function(){
      var rows = Array.prototype.slice.call(document.querySelectorAll('tr.branch'));
      var show = rows.length && rows[0].classList.contains('hidden');
      rows.forEach(function(r){ r.classList.toggle('hidden', !show); });
      toggleBranch.textContent = show
        ? '收起 ' + rows.length + ' 个子图步骤'
        : '展开 ' + rows.length + ' 个子图步骤';
    });
  }
  document.addEventListener('click', function(e){
    var t = e.target.closest('.row-toggle');
    if(t){
      var row = document.querySelector('tr[data-detail="' + t.dataset.target + '"]');
      if(row){ row.hidden = !row.hidden; t.classList.toggle('open', !row.hidden); }
      return;
    }
    var f = e.target.closest('.field');
    if(f){
      var key = f.dataset.key;
      var target = document.querySelector('.field[data-kind="write"][data-key="' + key + '"]');
      if(target){
        target.scrollIntoView({block:'center', behavior:'smooth'});
        target.classList.add('flash');
        setTimeout(function(){ target.classList.remove('flash'); }, 1200);
      }
    }
  });

  var search = document.getElementById('search');
  var hint = document.getElementById('hint');
  search.addEventListener('input', function(){
    var q = search.value.trim().toLowerCase();
    var active = document.querySelector('.graph.active');
    if(!active) return;
    var list = Array.prototype.slice.call(active.querySelectorAll('.node'));
    var hit = 0;
    list.forEach(function(n){
      var ok = !q || n.textContent.toLowerCase().indexOf(q) !== -1;
      n.classList.toggle('dim', !ok);
      if(ok) hit++;
    });
    hint.textContent = q ? (hit + ' / ' + list.length) : '';
  });
})();
"""


def render_scenario(modules: Dict[str, Dict[str, Any]], graphs: Dict[str, Dict[str, Any]],
                    fn_index: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> str:
    """路由总览：五个 Agent 怎么分流。

    原来的"复合意图并行"演练已随 composite 一起删掉——购物 + 行程现在由
    trip_gear 一次算完，不再有两段正文合并的路径。
    """
    rows = [
        ("shopping", "纯购物", "关键词：品类/品牌 + 购买动作", "商品 3 个"),
        ("travel", "旅行问答 / 纯行程规划", "关键词：地名、旅游词；或 travel + plan_trip", "天气、路线、活动、时间"),
        ("trip_gear", "行程 + 装备协同", "gear_plan，或复合意图，或出现露营/装备/清单", "天气、空气、汇率、假日、路线、时差（内部检索商品）"),
        ("general", "通用问答兜底", "既不是购物、旅行也不是客服", "天气、路线、时间"),
        ("customer_service", "订单客服", "关键词：订单、退款、物流…（命中两个词）", "订单"),
    ]
    body = "".join(
        f"<tr><td class='k'><code>{esc(name)}</code></td><td>{esc(title)}</td>"
        f"<td>{esc(rule)}</td><td class='muted'>{esc(tools)}</td></tr>"
        for name, title, rule, tools in rows
    )
    return (
        "<section>"
        "<h2>场景 · 五个 Agent 怎么分流</h2>"
        "<p class='muted' style='margin:0 0 8px'>顶层图只有五个业务节点；"
        "travel 与 trip_gear 共用同一张行程图，差别只有装备段（<code>with_gear</code>）。</p>"
        "<table class='tbl'><thead><tr><th style='width:150px'>节点</th>"
        "<th style='width:190px'>什么时候进</th><th>判据</th><th style='width:300px'>工具面</th>"
        f"</tr></thead><tbody>{body}</tbody></table>"
        "<p class='muted' style='margin:10px 0 0'>"
        "购物 + 旅行同时就绪时：要排行程就进 <code>trip_gear</code>（它本来就含商品检索），"
        "旅行那半只是顺口一问则按主意图走。</p>"
        "</section>"
    )


def render_html(modules: Dict[str, Dict[str, Any]], graphs: Dict[str, Dict[str, Any]],
                panels_html: str, fn_index: Dict[str, List[Tuple[str, Dict[str, Any]]]],
                prompt_index: Dict[str, List[Dict[str, Any]]]) -> str:
    order = ["turn", "memory", "shopping", "travel", "trip_gear",
             "trip_gear_plan", "customer_service"]
    ordered = [graphs[key] for key in order if key in graphs]
    ordered += [graph for key, graph in graphs.items() if graph not in ordered]

    tabs, sections = [
        '<button class="tab active" data-graph="scenario">场景 · 购物+旅行</button>'
    ], [render_scenario(modules, graphs, fn_index)]
    for index, graph in enumerate(ordered):
        title = GRAPH_TITLES.get(graph["id"], graph["id"])
        tabs.append(
            f'<button class="tab" data-graph="{esc(graph["id"])}">'
            f'{esc(title)}<span class="n">{len(graph["nodes"])}</span></button>'
        )
        counts = {
            node_id: node_prompt_count(modules, graphs, fn_index, prompt_index, graph["id"], node_id, node)
            for node_id, node in graph["nodes"].items()
        }
        section, _ = render_graph_section(graph, False, counts)
        sections.append(section)

    prompt_count = sum(len(module["prompts"]) for module in modules.values())
    constant_count = sum(
        1 for module in modules.values() for item in module["constants"]
        if item["kind"] in {"dict", "list", "tuple", "set", "regex"}
    )
    node_count = sum(len(graph["nodes"]) for graph in graphs.values())
    edge_count = sum(len(graph["edges"]) for graph in graphs.values())
    generated = time.strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>全链路规格 · {len(graphs)} 张图</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <h1>全链路规格</h1>
      <div class="subtle">{len(graphs)} 张图 · {node_count} 个节点 · {edge_count} 条边 · {prompt_count} 段提示词 ·
        全部从源码 AST 抽取，生成于 {generated}</div>
    </div>
    <div class="toolbar">
      <input class="search" id="search" type="search" placeholder="查找当前图节点…">
      <span class="muted" id="hint"></span>
      <button class="btn" id="constants">常量 · {constant_count}</button>
      <button class="btn" id="prompts">提示词 · {prompt_count}</button>
    </div>
  </div>

  <div class="tabs">{"".join(tabs)}</div>
  {"".join(sections)}

  <div class="foot">
    场景页：悬停字段高亮所有读写位置，点行首「+」看该步细节。
    其他页：实线是固定边，带标签的是条件边，橙色虚线是回边；点节点看规则、常量与提示词，子图节点可继续下钻。
  </div>
</div>

<div class="tip" id="tip"></div>
<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer">
  <div class="drawer-bar"><span class="t">详情</span>
    <button class="drawer-close" id="close" aria-label="关闭">✕</button></div>
  <div class="drawer-body">{panels_html}</div>
</aside>

<script>{JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_fn_index(modules: Dict[str, Dict[str, Any]]) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
    index: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for path, module in modules.items():
        for fn in module["functions"]:
            index.setdefault(fn["name"], []).append((path, fn))
    return index


def check_notes(modules: Dict[str, Dict[str, Any]], graphs: Dict[str, Dict[str, Any]],
                fn_index: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> List[str]:
    """字段/节点的人工注释是否跟得上代码。"""
    used: Set[str] = set()
    node_ids: Set[str] = set()
    for graph_id, graph in graphs.items():
        for node_id, node in graph["nodes"].items():
            node_ids.add(node_id)
            if node.get("subgraph"):
                continue
            resolved = resolve_fn(modules, graphs, fn_index, graph_id, node.get("fn", ""))
            if resolved:
                used.update(resolved[1].get("reads", []))
                used.update(resolved[1].get("writes", []))

    errors: List[str] = []
    warnings: List[str] = []
    for key in sorted(used - set(FIELD_NOTES)):
        errors.append(f"字段 {key} 没有含义说明（FIELD_NOTES）")
    for key in sorted(set(FIELD_NOTES) - used):
        warnings.append(f"FIELD_NOTES 里的 {key} 当前没有被节点直接读写（可能被 helper 间接使用）")
    for key in sorted(set(NODE_NOTES) - node_ids):
        warnings.append(f"NODE_NOTES 里的 {key} 已不是任何图的节点")
    return errors + ["[warn] " + item for item in warnings]


def check(modules: Dict[str, Dict[str, Any]], graphs: Dict[str, Dict[str, Any]],
          fn_index: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> List[str]:
    problems: List[str] = []
    for graph_id, graph in graphs.items():
        ids = set(graph["nodes"])
        if "__start__" in ids or "__end__" in ids:
            problems.append(f"{graph_id}: 节点名与 START/END 冲突")
        for edge in graph["edges"]:
            for side in ("src", "dst"):
                target = edge[side]
                if target in {"__start__", "__end__"}:
                    continue
                if target not in ids:
                    problems.append(f"{graph_id}: 边指向不存在的节点 {target}")
        for node_id, node in graph["nodes"].items():
            if node.get("subgraph") and node["subgraph"] not in graphs:
                problems.append(f"{graph_id}:{node_id} 子图不存在 {node['subgraph']}")
            if node.get("fn") and node["fn"] not in fn_index:
                problems.append(f"{graph_id}:{node_id} 函数不存在 {node['fn']}")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="从源码生成全链路规格")
    parser.add_argument("--out", default=DEFAULT_HTML)
    parser.add_argument("--json", default=DEFAULT_JSON)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    modules = scan_modules()
    graphs = parse_graphs(modules)
    fn_index = build_fn_index(modules)
    prompt_index: Dict[str, List[Dict[str, Any]]] = {}
    for path, module in modules.items():
        for prompt in module["prompts"]:
            prompt_index.setdefault(prompt["name"], []).append(dict(prompt, module=path))

    problems = check(modules, graphs, fn_index)
    note_all = check_notes(modules, graphs, fn_index)
    note_issues = [item for item in note_all if not item.startswith("[warn]")]
    note_warnings = [item for item in note_all if item.startswith("[warn]")]
    panels_html = render_panels(modules, graphs, fn_index, prompt_index)
    panels_html += render_constants_panel(modules)
    panels_html += render_prompts_panel(modules)
    html_text = render_html(modules, graphs, panels_html, fn_index, prompt_index)

    if args.check:
        if problems:
            print("[check] 结构问题：")
            for item in problems:
                print("  -", item)
        if note_issues:
            print("[check] 注释与代码不同步：")
            for item in note_issues:
                print("  -", item)
        if note_warnings:
            print("[check] 提醒：")
            for item in note_warnings:
                print("  -", item[7:])
        if problems or note_issues:
            return 1
        print(f"[check] {len(graphs)} 张图、{sum(len(g['nodes']) for g in graphs.values())} 个节点、"
              f"{len(FIELD_NOTES)} 条字段说明全部对得上")
        return 0

    # 默认输出已改到工作区根的 docs/generated；显式传入的相对路径仍按代码目录解析。
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    json_path = Path(args.json)
    if not json_path.is_absolute():
        json_path = ROOT / json_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")
    json_path.write_text(
        json.dumps({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "graphs": [
                {"id": g["id"], "module": g["module"], "nodes": g["nodes"], "edges": g["edges"]}
                for g in graphs.values()
            ],
            "prompts": [
                {"name": p["name"], "module": path, "lineno": p["lineno"], "messages": p["messages"]}
                for path, m in modules.items() for p in m["prompts"]
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        shown = out_path.relative_to(ROOT.parent)
    except ValueError:
        shown = out_path
    print(f"[ok] {shown} ({out_path.stat().st_size // 1024} KB)")
    print(f"     图 {len(graphs)} · 节点 {sum(len(g['nodes']) for g in graphs.values())}"
          f" · 提示词 {sum(len(m['prompts']) for m in modules.values())}")
    if problems:
        for item in problems:
            print("[warn]", item, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
