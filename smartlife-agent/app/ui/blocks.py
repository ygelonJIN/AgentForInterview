"""回答块构造：把各 Agent 的结构化结果翻译成 ``theme.answer_blocks_html`` 的输入。

**这里只能读真实数据**：商品行来自商品库检索结果，订单行来自订单表，
正文来自模型输出。任何统计数字（价格区间、平均评分、合计）都从这些行算出，
不允许模型自己填——否则就是第二个"编数据"的入口。

块类型与 ``app/ui/theme.py`` 的 ``answer_block_html`` 一一对应：
text / lead / stats / cards / table / timeline / panel。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def _money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    return f"¥{amount:,.0f}" if amount.is_integer() else f"¥{amount:,.2f}"


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def text_block(text: str, title: str = "") -> Dict[str, Any]:
    return {"type": "text", "text": str(text or ""), "title": title}


def lead_block(text: str, em: str = "", note: str = "") -> Dict[str, Any]:
    return {"type": "lead", "text": str(text or ""), "em": str(em or ""), "note": str(note or "")}


def stats_block(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "stats", "items": [item for item in items if item.get("value") not in (None, "")]}


def cards_block(title: str, meta: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "cards", "title": title, "meta": meta, "items": items}


def table_block(title: str, meta: str, columns: List[Dict[str, Any]], rows: List[List[Any]]) -> Dict[str, Any]:
    return {"type": "table", "title": title, "meta": meta, "columns": columns, "rows": rows}


def timeline_block(title: str, meta: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "timeline", "title": title, "meta": meta, "items": items}


def clean(blocks: Iterable[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """丢掉空块和空文本，保证前端拿到的都是能渲染的。"""
    result: List[Dict[str, Any]] = []
    for block in blocks or []:
        if not block:
            continue
        kind = str(block.get("type") or "")
        if kind == "text" and not str(block.get("text") or "").strip():
            continue
        if kind == "lead" and not str(block.get("text") or "").strip():
            continue
        if kind in {"cards", "table", "timeline", "stats"} and not block.get("items") and not block.get("rows"):
            continue
        result.append(block)
    return result


def shopping_blocks(retrieval: Dict[str, Any], response: str, query: str = "") -> List[Dict[str, Any]]:
    """商品推荐：stat 条 + 商品卡 + 预算表，最后接正文。"""
    products = [item for item in (retrieval or {}).get("products") or [] if isinstance(item, dict)]
    blocks: List[Dict[str, Any]] = []
    if products:
        prices = [price for price in (_number(item.get("price")) for item in products) if price is not None]
        ratings = [rate for rate in (_number(item.get("rating")) for item in products) if rate is not None]
        stocks = [_number(item.get("stock")) for item in products]
        stats = [
            {"label": "命中", "value": f"{len(products)} 件"},
            {"label": "价格区间", "value": f"{_money(min(prices))}–{_money(max(prices))}" if prices else ""},
            {"label": "平均评分", "value": f"{sum(ratings) / len(ratings):.1f}" if ratings else ""},
            {"label": "有货", "value": f"{sum(1 for s in stocks if s and s > 0)} 件" if any(stocks) else ""},
        ]
        blocks.append(lead_block(f"按「{query}」筛出 {len(products)} 件，" if query else f"检索到 {len(products)} 件，", em="价格与库存来自商品库", note="不含运费。"))
        blocks.append(stats_block(stats))
        cards = []
        for item in products[:4]:
            tags = []
            if item.get("stock") is not None:
                tags.append(f"库存 {item.get('stock')}")
            if item.get("rating") is not None:
                tags.append(f"评分 {item.get('rating')}")
            if item.get("brand"):
                tags.append(str(item.get("brand")))
            cards.append({
                "kicker": str(item.get("category") or ""),
                "name": str(item.get("name") or ""),
                "price": _money(item.get("price")),
                "note": str(item.get("specs") or "")[:40] if isinstance(item.get("specs"), str) else "",
                "tags": tags,
            })
        blocks.append(cards_block("推荐清单", "按匹配度 × 口碑 × 预算占用排序", cards))
        rows = []
        total = 0.0
        for item in products[:4]:
            price = _number(item.get("price")) or 0.0
            total += price
            rows.append([str(item.get("name") or ""), _money(price), "1", _money(price)])
        if rows:
            rows.append(["合计", "—", str(len(rows)), _money(total)])
            blocks.append(table_block("预算占用", f"按检索结果前 {len(rows) - 1} 件估算", [
                {"label": "商品"}, {"label": "单价", "num": True}, {"label": "数量", "num": True}, {"label": "小计", "num": True},
            ], rows))
    blocks.append(text_block(response))
    return clean(blocks)


def order_blocks(orders: List[Dict[str, Any]], response: str) -> List[Dict[str, Any]]:
    """订单客服：指标格 + 订单轨迹行 + 正文。"""
    rows = [item for item in orders or [] if isinstance(item, dict)]
    if not rows:
        return clean([text_block(response)])
    total = sum(_number(item.get("total_price")) or 0.0 for item in rows)
    latest = rows[0]
    blocks = [
        lead_block(
            f"当前共 {len(rows)} 笔订单，最近一笔 ",
            em=f"{latest.get('product_name') or '订单'} · {latest.get('status') or '状态未知'}",
            note="数据来自订单表，只读。",
        ),
        stats_block([
            {"label": "订单数", "value": f"{len(rows)} 笔"},
            {"label": "金额合计", "value": _money(total)},
            {"label": "最近状态", "value": str(latest.get("status") or "未知")},
        ]),
        timeline_block("订单记录", "最近 5 笔", [
            {
                "no": "",
                "title": str(item.get("product_name") or item.get("id") or "订单"),
                "meta": str(item.get("status") or ""),
                "rows": [{
                    "time": str(item.get("created_at") or "")[:10],
                    "main": f"id={item.get('id')} · {item.get('product_name') or ''}",
                    "sub": str(item.get("status") or ""),
                    "right": _money(item.get("total_price")),
                }],
            }
            for item in rows[:5]
        ]),
        text_block(response),
    ]
    return clean(blocks)


def text_only(response: str) -> List[Dict[str, Any]]:
    """没有结构化数据就只画正文块：同一张纸面，不造字段。"""
    return clean([text_block(response)])
