"""装备规格匹配：从"有没有"到"够不够用"。

原来的判定只认名字：清单里有"睡袋"就算通过，哪怕那是件 15 温标的夏季
薄款、而目的地夜间 3℃。本模块把"够不够用"单独建模，给出三种结论：

- ``ok``：规格齐全且达标
- ``insufficient``：规格齐全但不够用（夜里 6℃，睡袋温标 10℃）
- ``unknown``：没有规格信息。既不算通过也不算不通过，单独标出来交给澄清，
  因为"猜它够用"和"猜它不够"都会错。

规格字段刻意保持少而稳，够覆盖露营/徒步场景即可：
``comfort_temp_c`` / ``capacity_persons`` / ``r_value`` / ``volume_l`` /
``waterproof_mm``。新品类先扩展 :data:`SPEC_LABELS`，不要另起一套命名。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from app.trip_gear.models import GearItem, TripFacts

# 规格字段的中文标签与单位，展示与报错文案共用，避免两处写法不一致。
SPEC_LABELS: Dict[str, str] = {
    "comfort_temp_c": "温标",
    "capacity_persons": "可容纳人数",
    "r_value": "保温值",
    "volume_l": "容量",
    "waterproof_mm": "防水",
}
SPEC_UNITS: Dict[str, str] = {
    "comfort_temp_c": "℃",
    "capacity_persons": "人",
    "r_value": "R",
    "volume_l": "L",
    "waterproof_mm": "mm",
}

# 夜间温度留的余量，与约束派生规则保持同一个口径（见 constraints.py）。
TEMPERATURE_MARGIN_C = 5.0

# 防潮垫保温值下限：冷天需要更厚的垫子，分两档即可，不再细分。
R_VALUE_COLD = 2.0
R_VALUE_MILD = 1.0
COLD_THRESHOLD_C = 5.0

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class SpecRequirement:
    """一条规格要求：哪件装备、哪个字段、要求多少。"""

    gear: str
    key: str
    op: str  # "<=" 越小越强；">=" 越大越强
    value: float
    reason: str = ""

    @property
    def label(self) -> str:
        return SPEC_LABELS.get(self.key, self.key)

    @property
    def unit(self) -> str:
        return SPEC_UNITS.get(self.key, "")

    def describe(self) -> str:
        symbol = "≤" if self.op == "<=" else "≥"
        return f"{self.gear}{self.label}{symbol}{self.value:g}{self.unit}"


@dataclass
class SpecResult:
    """一条规格核对的结果。"""

    requirement: SpecRequirement
    status: str
    actual: Optional[float] = None
    hint: str = ""

    @property
    def passed(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> Dict[str, Any]:
        req = self.requirement
        return {
            "gear": req.gear,
            "key": req.key,
            "required": req.value,
            "actual": self.actual,
            "unit": req.unit,
            "status": self.status,
            "hint": self.hint,
        }


def spec_requirements(
    facts: TripFacts,
    gear_names: Optional[Iterable[str]] = None,
) -> List[SpecRequirement]:
    """按整趟事实派生规格要求。

    只为"清单里真的有这件东西"生成，避免对着没带的装备报缺规格；
    ``gear_names`` 为空时按事实推断（露营一定会有睡袋和帐篷）。
    """
    names = set(gear_names or ())
    if not names:
        if facts.is_camping:
            names = {"睡袋", "帐篷", "防潮垫"}

    requirements: List[SpecRequirement] = []
    min_temp = facts.min_temp_c

    if "睡袋" in names and min_temp is not None:
        target = float(min_temp) - TEMPERATURE_MARGIN_C
        requirements.append(
            SpecRequirement(
                gear="睡袋",
                key="comfort_temp_c",
                op="<=",
                value=round(target, 1),
                reason=f"夜间最低 {min_temp:g}℃，温标留 {TEMPERATURE_MARGIN_C:g}℃ 余量",
            )
        )

    if "帐篷" in names and facts.people is not None:
        requirements.append(
            SpecRequirement(
                gear="帐篷",
                key="capacity_persons",
                op=">=",
                value=float(facts.effective_people),
                reason=f"同行 {facts.people} 人，帐篷要睡得下",
            )
        )

    if "防潮垫" in names and min_temp is not None:
        cold = float(min_temp) <= COLD_THRESHOLD_C
        requirements.append(
            SpecRequirement(
                gear="防潮垫",
                key="r_value",
                op=">=",
                value=R_VALUE_COLD if cold else R_VALUE_MILD,
                reason=f"夜间最低 {min_temp:g}℃，{'需要' if cold else '基本'}保温",
            )
        )

    return requirements


def _to_float(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def check(requirement: SpecRequirement, specs: Optional[Dict[str, Any]]) -> SpecResult:
    """按需求核对一组规格。没有数值一律算 unknown，不替用户下结论。"""
    actual = _to_float((specs or {}).get(requirement.key))
    if actual is None:
        return SpecResult(
            requirement,
            STATUS_UNKNOWN,
            hint=f"{requirement.gear}没有{requirement.label}信息，需要确认",
        )

    ok = actual <= requirement.value if requirement.op == "<=" else actual >= requirement.value
    if ok:
        return SpecResult(requirement, STATUS_OK, actual=actual)

    needed = f"{requirement.describe()}"
    return SpecResult(
        requirement,
        STATUS_INSUFFICIENT,
        actual=actual,
        hint=(
            f"{requirement.gear}现有{requirement.label} {actual:g}{requirement.unit}，"
            f"本次要求 {needed}（{requirement.reason}）"
        ),
    )


def resolve_specs(item: GearItem, product: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总一件装备的规格：已有物品看自身，待购看商品，两处都有则以自身为准。"""
    specs: Dict[str, Any] = {}
    if item.status == "need_buy" and product:
        specs.update(as_specs(product.get("specs")))
    specs.update(as_specs(item.specs))
    return specs


def as_specs(raw: Any) -> Dict[str, Any]:
    """商品行来自 SQLite，specs 可能是 JSON 字符串，统一成字典。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def evaluate(
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
    requirements: Optional[Iterable[SpecRequirement]] = None,
    facts: Optional[TripFacts] = None,
) -> List[SpecResult]:
    """对装备清单逐条核对规格要求。"""
    names = {item.name for item in gear if item.status != "skipped"}
    reqs = list(requirements) if requirements is not None else spec_requirements(facts or TripFacts(), names)

    by_name = {item.name: item for item in gear if item.status != "skipped"}
    results: List[SpecResult] = []
    for req in reqs:
        item = by_name.get(req.gear)
        if item is None:
            # 装备本身缺失由校验器的"齐全性"负责，这里不重复报。
            continue
        results.append(check(req, resolve_specs(item, products.get(req.gear))))
    return results


# ---- 从文字里抽规格 -------------------------------------------------------
# 只认带单位的数字，宁可少抽也不误抽：把"5 岁"读成温标会直接判错。

_CN_NUM = {"一": 1, "双": 2, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_TEMP_RE = re.compile(r"(零下\s*)?([-－])?\s*(\d+(?:\.\d+)?)\s*(?:℃|°C|度)")
_PERSONS_RE = re.compile(r"([一双两二三四五六\d]+)\s*个?\s*人")
_VOLUME_L_RE = re.compile(r"(\d+(?:\.\d+)?)\s*L\b", re.IGNORECASE)
_VOLUME_ML_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ml", re.IGNORECASE)
_WATERPROOF_RE = re.compile(r"(\d+)\s*mm", re.IGNORECASE)
_R_VALUE_RE = re.compile(r"(?:R\s*值|保温值)\s*[:：]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

_SLEEPING_BAG_WORDS = ("睡袋", "温标", "舒适温度")


def parse_specs(text: str, name: str = "") -> Dict[str, float]:
    """从一句描述里抽规格。

    ``name`` 用于消歧：只有提到睡袋/温标的温度才算睡袋温标，
    否则"去 3 度的地方"这种句子会被误读成装备规格。
    """
    raw = str(text or "")
    specs: Dict[str, float] = {}
    if not raw.strip():
        return specs

    pool = f"{name} {raw}"

    if any(word in pool for word in _SLEEPING_BAG_WORDS):
        match = _TEMP_RE.search(raw)
        if match:
            value = float(match.group(3))
            # "零下5度"和"-5°C"都要落成负数，否则温标会反着判。
            if match.group(1) or match.group(2):
                value = -value
            specs["comfort_temp_c"] = value

    persons = _PERSONS_RE.search(raw)
    if persons:
        token = persons.group(1)
        count = int(token) if token.isdigit() else _CN_NUM.get(token, 0)
        if count:
            specs["capacity_persons"] = float(count)

    r_value = _R_VALUE_RE.search(raw)
    if r_value:
        specs["r_value"] = float(r_value.group(1))

    volume_l = _VOLUME_L_RE.search(raw)
    if volume_l:
        specs["volume_l"] = float(volume_l.group(1))

    volume_ml = _VOLUME_ML_RE.search(raw)
    if volume_ml and "volume_l" not in specs:
        specs["volume_l"] = round(float(volume_ml.group(1)) / 1000.0, 2)

    waterproof = _WATERPROOF_RE.search(raw)
    if waterproof:
        specs["waterproof_mm"] = float(waterproof.group(1))

    return specs
