"""采购可履约：买得到、来得及。

"推荐一件商品"和"出发前能到手"是两件事。后者由两个硬事实决定：

- ``stock``：为 0 就是买不到，换替代品才有意义；
- ``lead_time_days`` 与"距出发还有几天"：到货 5 天、明天出发，方案就是废的。

结论四态：``ok`` / ``late``（赶不上）/ ``out_of_stock``（没货）/
``unknown``（缺出发日期或缺时效——不猜，如实披露）。

纯函数，不读库不联网；日期从外部传入，测试才能定点复现。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from app.trip_gear.models import GearItem, TripFacts

STATUS_OK = "ok"
STATUS_LATE = "late"
STATUS_OUT_OF_STOCK = "out_of_stock"
STATUS_UNKNOWN = "unknown"

# 收货日与出发日同一天算来得及，所以比较用"到货日 <= 出发日"。
DELIVERY_MARGIN_DAYS = 0


@dataclass
class PurchaseResult:
    """一件待购装备的履约结论。"""

    gear: str
    product: str
    status: str
    lead_time_days: Optional[int] = None
    days_left: Optional[int] = None
    stock: Optional[int] = None
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            "gear": self.gear,
            "product": self.product,
            "status": self.status,
            "lead_time_days": self.lead_time_days,
            "days_left": self.days_left,
            "stock": self.stock,
            "hint": self.hint,
        }


def _parse_date(raw: Any) -> Optional[date]:
    """只认明确日期；认不出返回 None，让上层说"不知道"，而不是当作今天。"""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    text = str(raw or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def departure_date(facts: TripFacts) -> Optional[date]:
    return _parse_date(facts.start_date)


def days_until(departure: Optional[date], today: Optional[date] = None) -> Optional[int]:
    """距出发还有几天；已出发或日期缺失都返回 None（不做负数外推）。"""
    if departure is None:
        return None
    reference = today or date.today()
    delta = (departure - reference).days
    return delta if delta >= 0 else None


def _as_int(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def delivery_check(
    gear: str,
    product: Dict[str, Any],
    days_left: Optional[int],
) -> PurchaseResult:
    """单件装备的履约判定。"""
    name = str(product.get("name") or "")
    stock = _as_int(product.get("stock"))
    lead = _as_int(product.get("lead_time_days"))

    if stock is not None and stock <= 0:
        return PurchaseResult(
            gear=gear,
            product=name,
            status=STATUS_OUT_OF_STOCK,
            lead_time_days=lead,
            days_left=days_left,
            stock=stock,
            hint=f"{gear} 选中的商品无货，需要换现货或到店自提",
        )

    if days_left is None:
        return PurchaseResult(
            gear=gear,
            product=name,
            status=STATUS_UNKNOWN,
            lead_time_days=lead,
            days_left=None,
            stock=stock,
            hint=f"没有出发日期，无法判断 {gear} 是否来得及到货",
        )

    if lead is None:
        return PurchaseResult(
            gear=gear,
            product=name,
            status=STATUS_UNKNOWN,
            lead_time_days=None,
            days_left=days_left,
            stock=stock,
            hint=f"{gear} 选中的商品没有到货时效，无法判断是否来得及",
        )

    if lead > days_left + DELIVERY_MARGIN_DAYS:
        return PurchaseResult(
            gear=gear,
            product=name,
            status=STATUS_LATE,
            lead_time_days=lead,
            days_left=days_left,
            stock=stock,
            # 自提由调用方按"有没有货"追加，这里不重复说，免得一句话里出现两遍。
            hint=f"{gear} 到货需 {lead} 天，距出发只有 {days_left} 天",
        )

    return PurchaseResult(
        gear=gear,
        product=name,
        status=STATUS_OK,
        lead_time_days=lead,
        days_left=days_left,
        stock=stock,
    )


def check_purchases(
    gear: Iterable[GearItem],
    products: Dict[str, Dict[str, Any]],
    facts: TripFacts,
    today: Optional[date] = None,
) -> List[PurchaseResult]:
    """逐条核对"待购件"能不能在出发前到手。已有/自备的件不参与。"""
    left = days_until(departure_date(facts), today)
    results: List[PurchaseResult] = []
    for item in gear:
        if item.status != "need_buy":
            continue
        product = products.get(item.name)
        if not product:
            # 没匹配到商品由校验器的"库未覆盖"负责，这里不重复报。
            continue
        results.append(delivery_check(item.name, product, left))
    return results


# ---- 替代链：赶不上之后换什么 -------------------------------------------
# 只报"赶不上"是不负责任的——用户还得自己去翻商品库。这里按同一个检索
# 能力找同品类的替代款，并且只留"真的能在出发前到"的那些。


@dataclass
class Substitute:
    """一个可达的替代款。"""

    name: str
    price: float
    lead_time_days: int
    stock: int
    delta_price: float
    rating: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "price": self.price,
            "lead_time_days": self.lead_time_days,
            "stock": self.stock,
            "delta_price": self.delta_price,
            "rating": self.rating,
        }


FindAlternatives = Any  # 与 repair 共用同一个检索回调


def _rows(find_alternatives: FindAlternatives, product: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """先按子品类找同类款，找不到再退回按名称检索。"""
    subcategory = str(product.get("subcategory") or "")
    rows: List[Dict[str, Any]] = []
    if subcategory:
        try:
            rows = list(find_alternatives(subcategory=subcategory, limit=limit) or [])
        except Exception:
            rows = []
    if not rows:
        query = str(product.get("name") or "")
        try:
            rows = list(find_alternatives(query=query, limit=limit) or [])
        except Exception:
            rows = []
    return [row for row in rows if isinstance(row, dict) and row.get("name")]


def find_substitutes(
    product: Dict[str, Any],
    days_left: Optional[int],
    find_alternatives: Optional[FindAlternatives] = None,
    limit: int = 3,
) -> List[Substitute]:
    """同一品类里能赶在出发前到货的替代款，按价格升序。

    只用"到货天数"和"库存"两个硬事实筛，价格只用来排序——
    便宜但赶不上的款不该出现在推荐里。
    """
    if not find_alternatives or days_left is None:
        return []

    current_name = str(product.get("name") or "")
    current_price = float(product.get("price") or 0)
    found: List[Substitute] = []
    for row in _rows(find_alternatives, product, limit=max(limit * 4, 12)):
        name = str(row.get("name") or "")
        if not name or name == current_name:
            continue
        stock = _as_int(row.get("stock"))
        lead = _as_int(row.get("lead_time_days"))
        if stock is None or stock <= 0 or lead is None:
            continue
        if lead > days_left + DELIVERY_MARGIN_DAYS:
            continue
        price = float(row.get("price") or 0)
        found.append(
            Substitute(
                name=name,
                price=price,
                lead_time_days=lead,
                stock=stock,
                delta_price=round(price - current_price, 2),
                rating=float(row.get("rating") or 0),
            )
        )

    found.sort(key=lambda item: (item.price, -item.rating))
    return found[:limit]


def pickup_available(product: Dict[str, Any]) -> bool:
    """本地有货就能到店自提——这是极端缺货时的最后一条路。

    目前不建模地理位置，只把"有货"当作可自提的必要条件，结论里如实标注。
    """
    stock = _as_int(product.get("stock"))
    return stock is not None and stock > 0


def days_between(departure: date, today: date) -> int:
    """辅助函数：给测试与展示算天数差，负值表示已出发。"""
    return (departure - today).days


def add_days(base: date, days: int) -> date:
    return base + timedelta(days=days)
