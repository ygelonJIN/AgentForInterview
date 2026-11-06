"""按品类定义"选购参数"，而不是用一套通用字段套所有商品。

羽绒服看蓬松度、充绒量；手机看 CPU、内存；帐篷看季节和打包重量——它们的
"好"根本不是同一组数字。所以参数表按品类分开，每个字段声明方向和量纲：

- ``higher``：越大越好（蓬松度、流明、防水毫米数）
- ``lower``：越小越好（温度标、重量、打包重量）

选品打分只读这里声明过的字段；没声明的字段不参与排序，避免拿无关数字凑分。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# key -> (中文名, 方向, 单位)。方向只影响归一化，不影响硬门槛。
SPEC_SCHEMA: Dict[str, Dict[str, tuple]] = {
    "睡袋": {
        "comfort_temp_c": ("舒适温标", "lower", "℃"),
        "fill_power": ("蓬松度", "higher", "FP"),
        "fill_weight_g": ("充绒量", "higher", "g"),
        "down_ratio": ("绒占比", "higher", ""),
        "weight_g": ("重量", "lower", "g"),
    },
    "帐篷": {
        "capacity_persons": ("容纳人数", "higher", "人"),
        "season": ("适用季节", "higher", "季"),
        "packed_weight_g": ("打包重量", "lower", "g"),
        "waterproof_mm": ("防水", "higher", "mm"),
    },
    "防潮垫": {
        "r_value": ("保温值", "higher", "R"),
        "weight_g": ("重量", "lower", "g"),
        "thickness_mm": ("厚度", "higher", "mm"),
    },
    "头灯": {
        "lumen": ("亮度", "higher", "lm"),
        "runtime_h": ("续航", "higher", "h"),
        "weight_g": ("重量", "lower", "g"),
    },
    "营地灯": {
        "lumen": ("亮度", "higher", "lm"),
        "runtime_h": ("续航", "higher", "h"),
        "weight_g": ("重量", "lower", "g"),
    },
    "背包": {
        "volume_l": ("容量", "higher", "L"),
        "load_kg": ("舒适负重", "higher", "kg"),
        "weight_g": ("自重", "lower", "g"),
    },
    "登山鞋": {
        "waterproof_mm": ("防水", "higher", "mm"),
        "weight_g": ("单只重量", "lower", "g"),
        "support": ("支撑等级", "higher", "级"),
    },
    "冲锋衣": {
        "waterproof_mm": ("防水", "higher", "mm"),
        "breathability_g": ("透气", "higher", "g/m²/24h"),
        "weight_g": ("重量", "lower", "g"),
    },
    "保暖层": {
        "fill_power": ("蓬松度", "higher", "FP"),
        "fill_weight_g": ("充绒量", "higher", "g"),
        "down_ratio": ("绒占比", "higher", ""),
        "weight_g": ("重量", "lower", "g"),
    },
    "速干衣": {
        "weight_g": ("重量", "lower", "g"),
        "dry_time_min": ("速干时间", "lower", "min"),
    },
    "雨伞": {
        "ribs": ("伞骨数", "higher", "骨"),
        "weight_g": ("重量", "lower", "g"),
        "wind_level": ("抗风等级", "higher", "级"),
    },
    "儿童衣物": {
        "weight_g": ("重量", "lower", "g"),
        "softness": ("柔软度", "higher", "级"),
    },
    "手机": {
        "cpu_score": ("处理器跑分", "higher", "分"),
        "ram_gb": ("内存", "higher", "GB"),
        "storage_gb": ("存储", "higher", "GB"),
        "battery_mah": ("电池容量", "higher", "mAh"),
        "weight_g": ("重量", "lower", "g"),
    },
    "电脑": {
        "cpu_score": ("处理器跑分", "higher", "分"),
        "ram_gb": ("内存", "higher", "GB"),
        "storage_gb": ("存储", "higher", "GB"),
        "screen_inch": ("屏幕尺寸", "higher", "英寸"),
        "weight_g": ("重量", "lower", "g"),
    },
}

# 装备名归到某个品类，检索时用来排除跨品类误命中
# （"羽绒"能搜到羽绒睡袋，但保暖层只该在服装里找）。
GEAR_SUBCATEGORY = {
    "帐篷": "露营",
    "睡袋": "露营",
    "防潮垫": "露营",
    "头灯": "露营",
    "营地灯": "露营",
    "背包": "登山",
    "登山鞋": "登山",
    "防水鞋": "登山",
    "冲锋衣": "登山",
    "保暖层": "服装",
    "速干衣": "服装",
    "儿童衣物": "童装",
    "雨伞": "日用",
    "雨具": "日用",
}


def schema_for(category: str) -> Dict[str, tuple]:
    """返回该品类的参数表；没定义就返回空，表示不参与打分。"""
    return SPEC_SCHEMA.get(str(category or ""), {})


def subcategory_for(gear_name: str) -> str:
    return GEAR_SUBCATEGORY.get(str(gear_name or ""), "")


def numeric_specs(category: str, specs: Any) -> Dict[str, float]:
    """取出打分用得上的数值字段，忽略品类没声明的键。

    商品行来自 SQLite 时 ``specs`` 是 JSON 字符串，这里统一解析。
    """
    from app.trip_gear.spec_match import as_specs

    schema = schema_for(category)
    resolved = as_specs(specs)
    values: Dict[str, float] = {}
    for key in schema:
        try:
            values[key] = float(resolved.get(key))
        except (TypeError, ValueError):
            continue
    return values


def quality_score(
    category: str,
    specs: Dict[str, Any],
    ranges: Optional[Dict[str, tuple]] = None,
) -> Optional[float]:
    """参数质量分（0~1）：按品类内 min-max 归一化，方向由 schema 决定。

    ``ranges`` 为空时只按"有没有值"给分，用于单件商品的兜底。
    """
    schema = schema_for(category)
    values = numeric_specs(category, specs)
    if not values:
        return None

    parts: List[float] = []
    for key, value in values.items():
        direction = schema[key][1]
        low, high = (ranges or {}).get(key, (None, None))
        if low is None or high is None or high <= low:
            continue
        ratio = (value - low) / (high - low)
        parts.append(ratio if direction == "higher" else 1 - ratio)
    if not parts:
        return None
    return round(sum(parts) / len(parts), 4)


def ranges_from(candidates: Iterable[Dict[str, Any]], category: str) -> Dict[str, tuple]:
    """从候选集合里算每个字段的 min/max，用于同批比较。"""
    schema = schema_for(category)
    values: Dict[str, List[float]] = {key: [] for key in schema}
    for product in candidates:
        for key, value in numeric_specs(category, product.get("specs") or {}).items():
            values[key].append(value)
    ranges: Dict[str, tuple] = {}
    for key, items in values.items():
        if len(items) >= 2:
            ranges[key] = (min(items), max(items))
    return ranges
