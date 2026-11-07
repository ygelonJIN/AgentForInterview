"""本地活动资料的解析。

`data/activities/*.txt` 只有一种写法：

```
【露营活动】
1. 千岛湖星空露营 - 380元/人含帐篷和烧烤
   - 需提前一天预订
```

这个格式原来在两处各写了一套正则：`app/ui/content.py` 的目录页用它渲染卡片，
`app/tools/travel_tools.py` 用另一套正则取条目名。两套规则迟早会漂，
所以解析只留这一份，两边都从这里取。

注意：这里**只解析本地资料文件**。在线活动数据走 `app/tools/providers.py`
（Photon），不经过这个模块。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

_HEADING = re.compile(r"^【(.+)】$")
_ITEM = re.compile(r"^\d+[.、]\s+(.*)$")
_DETAIL = re.compile(r"^[-*]\s+(.*)$")


def parse_activity_notes(text: str) -> List[Dict[str, str]]:
    """把活动资料文本解析成 ``[{category, title, detail}]``。

    - `【X】` 开一个新分类，后续条目归到该分类；
    - `1. 标题 - 详情` 是一条，`- 补充` 追加到上一条的详情；
    - 空行忽略；没有编号的行在有当前条目时并入详情。
    """
    notes: List[Dict[str, str]] = []
    category = "活动灵感"
    current: Dict[str, str] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current["title"]:
            notes.append(current)
        current = None

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading = _HEADING.match(line)
        if heading:
            flush()
            category = heading.group(1).strip() or category
            continue

        item = _ITEM.match(line)
        if item:
            flush()
            body = item.group(1).strip()
            title, _, detail = body.partition(" - ")
            current = {
                "category": category,
                "title": title.strip(),
                "detail": detail.strip() or body.strip(),
            }
            continue

        detail = _DETAIL.match(line)
        if detail and current is not None:
            extra = detail.group(1).strip()
            current["detail"] = "；".join(part for part in (current["detail"], extra) if part)
            continue

        if current is not None:
            current["detail"] = "；".join(
                part for part in (current["detail"], line) if part
            )

    flush()
    return notes


def activity_titles(text: str) -> List[str]:
    """只要条目名（工具返回给模型看的形态）。"""
    return [note["title"] for note in parse_activity_notes(text)]


def as_resources(text: str, *, source: str, id_prefix: str) -> List[Dict[str, Any]]:
    """转成 UI 目录页的资源行；保持与 `app/ui/content.py` 其他资源同构。"""
    resources: List[Dict[str, Any]] = []
    for index, note in enumerate(parse_activity_notes(text)):
        resources.append({
            "id": f"{id_prefix}-{index}",
            "type": note["category"],
            "title": note["title"],
            "detail": note["detail"],
            "source": source,
        })
    return resources


__all__ = ["activity_titles", "as_resources", "parse_activity_notes"]
