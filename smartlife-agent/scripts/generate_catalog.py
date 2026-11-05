#!/usr/bin/env python3
"""按矩阵生成商品目录与评价（确定性、零 token）。

为什么用模板而不是让模型逐条写：商品目录是结构化数据，名称由"品牌 + 品类 +
档位"决定，价格由档位决定，规格由品类决定。两万条也只是一次批量 INSERT，
不需要调用模型。

评价不是随机拼接——那样一千条长一个样，检索排序会退化成随机。这里的做法是
**属性驱动**：

- 每条评价绑定一个方面（防水/保暖/耐用/做工/性价比…），同一商品的 5 条不撞方面；
- 情感分布由评分决定（4.8 分不会全是好评，3.5 分也不会全是差评）；
- 负面评价必须能对上属性（防水差的商品才有人说渗水），矛盾的评价会毁掉检索；
- 证据里带上商品真实规格（防水 20000mm、温标 -5℃），让"问答"和"推荐理由"有据可依。

改库前自动备份；按名称判重，重复执行安全。

用法（项目根目录）::

    python smartlife-agent/scripts/generate_catalog.py --dry-run --sample 20
    python smartlife-agent/scripts/generate_catalog.py --target 2000 --reviews 5
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.product_specs import lead_time_for  # noqa: E402

DB = ROOT / "data" / "products.db"

# ---- 品类矩阵 -------------------------------------------------------------
# (品类, 子品类, 档位标签, 价格, 规格) —— 价格随档位走，不随机乱给。

CATEGORY_DEFS = {
    "帐篷": {
        "subcategory": "露营", "unit": "顶", "waterproof": 1,
        "aspects": ["防水", "搭建", "空间", "重量", "做工"],
        "tiers": [
            ("单人", 199.0, {"capacity_persons": 1}),
            ("双人", 599.0, {"capacity_persons": 2}),
            ("三人", 1299.0, {"capacity_persons": 3}),
            ("四人", 1899.0, {"capacity_persons": 4}),
            ("轻量双人", 899.0, {"capacity_persons": 2}),
            ("四季双人", 1599.0, {"capacity_persons": 2}),
        ],
    },
    "睡袋": {
        "subcategory": "露营", "unit": "条", "waterproof": 1,
        "aspects": ["保暖", "重量", "做工", "性价比", "收纳"],
        "tiers": [
            ("15℃", 129.0, {"comfort_temp_c": 15}),
            ("5℃", 199.0, {"comfort_temp_c": 5}),
            ("-5℃", 299.0, {"comfort_temp_c": -5}),
            ("-15℃", 899.0, {"comfort_temp_c": -15}),
            ("-25℃", 1599.0, {"comfort_temp_c": -25}),
            ("羽绒 -10℃", 1299.0, {"comfort_temp_c": -10}),
        ],
    },
    "防潮垫": {
        "subcategory": "露营", "unit": "张", "waterproof": 0,
        "aspects": ["保暖", "重量", "收纳", "做工", "性价比"],
        "tiers": [
            ("薄款", 39.0, {"r_value": 1.0}),
            ("蛋巢", 89.0, {"r_value": 2.2}),
            ("加厚", 159.0, {"r_value": 2.8}),
            ("充气", 259.0, {"r_value": 4.0}),
            ("冬季款", 399.0, {"r_value": 5.5}),
        ],
    },
    "头灯": {
        "subcategory": "露营", "unit": "个", "waterproof": 1,
        "aspects": ["亮度", "续航", "佩戴", "防水", "做工"],
        "tiers": [
            ("基础", 59.0, {"lumen": 150}),
            ("标准", 99.0, {"lumen": 300}),
            ("高亮", 199.0, {"lumen": 600}),
            ("专业", 349.0, {"lumen": 900}),
        ],
    },
    "营地灯": {
        "subcategory": "露营", "unit": "个", "waterproof": 0,
        "aspects": ["亮度", "续航", "便携", "做工", "性价比"],
        "tiers": [
            ("便携", 89.0, {"lumen": 300}),
            ("标准", 199.0, {"lumen": 800}),
            ("大功率", 359.0, {"lumen": 1500}),
        ],
    },
    "背包": {
        "subcategory": "登山", "unit": "个", "waterproof": 1,
        "aspects": ["容量", "背负", "防水", "重量", "做工"],
        "tiers": [
            ("20L", 159.0, {"volume_l": 20}),
            ("30L", 259.0, {"volume_l": 30}),
            ("40L", 329.0, {"volume_l": 40}),
            ("55L", 599.0, {"volume_l": 55}),
            ("65L", 899.0, {"volume_l": 65}),
        ],
    },
    "登山鞋": {
        "subcategory": "登山", "unit": "双", "waterproof": 1,
        "aspects": ["防水", "防滑", "包裹", "重量", "耐用"],
        "tiers": [
            ("入门", 299.0, {"waterproof_mm": 5000}),
            ("中端", 459.0, {"waterproof_mm": 10000}),
            ("高帮", 799.0, {"waterproof_mm": 20000}),
            ("专业", 1299.0, {"waterproof_mm": 28000}),
        ],
    },
    "冲锋衣": {
        "subcategory": "登山", "unit": "件", "waterproof": 1,
        "aspects": ["防水", "透气", "做工", "保暖", "重量"],
        "tiers": [
            ("两件套", 399.0, {"waterproof_mm": 10000}),
            ("单层", 699.0, {"waterproof_mm": 20000}),
            ("硬壳", 1299.0, {"waterproof_mm": 28000}),
            ("轻量", 899.0, {"waterproof_mm": 15000}),
        ],
    },
    "保暖层": {
        "subcategory": "服装", "unit": "件", "waterproof": 0,
        "aspects": ["保暖", "重量", "做工", "版型", "性价比"],
        # 档位名必须带"羽绒"：装备名"保暖层"经 ALIASES 映射成"羽绒"再检索，
        # 出现"抓绒衣"这类名字就会匹配不上。
        "tiers": [
            ("轻薄羽绒服", 599.0, {}),
            ("中厚羽绒服", 799.0, {}),
            ("厚羽绒服", 999.0, {}),
        ],
    },
    "速干衣": {
        "subcategory": "服装", "unit": "件", "waterproof": 0,
        "aspects": ["速干", "透气", "版型", "做工", "性价比"],
        "tiers": [("短袖", 89.0, {}), ("长袖", 139.0, {}), ("长裤", 189.0, {})],
    },
    "雨伞": {
        "subcategory": "日用", "unit": "把", "waterproof": 1,
        "aspects": ["抗风", "便携", "防水", "做工", "性价比"],
        "tiers": [("折叠", 49.0, {}), ("加固", 89.0, {}), ("长柄", 69.0, {})],
    },
    "儿童衣物": {
        "subcategory": "童装", "unit": "件", "waterproof": 0,
        "aspects": ["舒适", "版型", "做工", "面料", "性价比"],
        "tiers": [("T恤", 59.0, {}), ("外套", 159.0, {}), ("长裤", 99.0, {})],
    },
}

BRANDS = {
    "帐篷": ["牧高笛", "挪客", "黑鹿", "探路者"],
    "睡袋": ["黑冰", "挪客", "迪卡侬", "探路者"],
    "防潮垫": ["挪客", "黑冰", "迪卡侬"],
    "头灯": ["火枫", "纳丽德", "迪卡侬"],
    "营地灯": ["火枫", "纳丽德", "挪客"],
    "背包": ["Osprey", "挪客", "迪卡侬", "探路者"],
    "登山鞋": ["Salomon", "Columbia", "迪卡侬", "探路者"],
    "冲锋衣": ["始祖鸟", "Columbia", "探路者", "迪卡侬"],
    "保暖层": ["波司登", "优衣库", "迪卡侬"],
    "速干衣": ["迪卡侬", "Adidas", "优衣库"],
    "雨伞": ["天堂", "蕉下", "迪卡侬"],
    "儿童衣物": ["巴拉巴拉", "优衣库", "迪卡侬"],
}

VARIANTS = ["标准款", "升级款", "2026 款"]

# ---- 真实商品名 -----------------------------------------------------------
# 商品名要像电商里真的会出现的名字："牧高笛 冷山2 双人帐篷 卡其"，
# 而不是拿规则里的装备名当商品名（"保暖层""防水鞋"是我们内部的说法）。

SERIES = {
    "帐篷": ["冷山", "云尚", "飘云", "星空"],
    "睡袋": ["B 系列", "冰焰", "极光", "暖冬"],
    "防潮垫": ["云垫", "轻途", "蛋巢"],
    "头灯": ["HL", "夜行者", "星火"],
    "营地灯": ["星耀", "营光", "露营家"],
    "背包": ["云鹰", "轻途", "远行"],
    "登山鞋": ["山脊", "溯溪", "岩途"],
    "冲锋衣": ["风暴", "硬壳", "轻语"],
    "保暖层": ["轻羽", "暖芯", "极地"],
    "速干衣": ["疾风", "透气", "轻速"],
    "雨伞": ["晴雨", "黑胶", "轻伞"],
    "儿童衣物": ["小探险", "童趣", "初行"],
}


def _descriptor(category: str, tier_label: str) -> str:
    """把档位翻译成真实的商品叫法。"""
    if category == "帐篷":
        return f"{tier_label}帐篷"
    if category == "睡袋":
        warm = any(ch in tier_label for ch in ("-", "羽绒"))
        return f"{tier_label}羽绒睡袋" if warm else f"{tier_label}棉睡袋"
    if category == "防潮垫":
        return f"{tier_label}防潮垫"
    if category == "头灯":
        return f"{tier_label}头灯"
    if category == "营地灯":
        return f"{tier_label}营地灯"
    if category == "背包":
        return f"{tier_label} 登山背包"
    if category == "登山鞋":
        return f"{tier_label}防水登山鞋"
    if category == "冲锋衣":
        return f"{tier_label}冲锋衣"
    if category == "保暖层":
        return tier_label if tier_label.endswith("服") else f"{tier_label}羽绒服"
    if category == "速干衣":
        return f"速干{tier_label}"
    if category == "雨伞":
        return f"{tier_label}雨伞"
    if category == "儿童衣物":
        return f"儿童{tier_label}"
    return f"{category}{tier_label}"


def _series(category: str, index: int) -> str:
    pool = SERIES.get(category) or [category]
    return pool[index % len(pool)]


# ---- 评价模板：按品类分组，方面 × 情感，句子带真实规格 ----------------------
# 分组而不是全品类共用：帐篷不该说"背着走五公里不压肩"，那是背包的话。
GROUP_OF = {
    "帐篷": "shelter", "睡袋": "sleep", "防潮垫": "sleep",
    "头灯": "light", "营地灯": "light", "背包": "carry",
    "登山鞋": "footwear", "冲锋衣": "apparel", "保暖层": "apparel",
    "速干衣": "apparel", "儿童衣物": "apparel", "雨伞": "daily",
}

TEXT = {
    "shelter": {
        ("防水", "正"): ["淋了两小时小雨，里层一点没潮。", "接缝压胶扎实，雨天心里有底。"],
        ("防水", "中"): ["小雨没问题，大雨还没试过。"],
        ("防水", "负"): ["中雨下了半小时，边角开始渗。", "防水层偏薄，雨大就得加罩。"],
        ("搭建", "正"): ["第一次搭八分钟就立起来了。", "杆子顺滑，一个人也能撑开。"],
        ("搭建", "中"): ["熟练之后快，生手要摸索一阵。"],
        ("搭建", "负"): ["风大的时候撑杆吃力。", "说明书太简略，摸索了半天。"],
        ("空间", "正"): ["{capacity_persons} 人躺下还能放行李。", "前厅能放两双鞋，不挤。"],
        ("空间", "中"): ["按标称人数睡够用，再塞装备就紧。"],
        ("空间", "负"): ["说是 {capacity_persons} 人，躺下转身就碰壁。", "高度偏低，坐起来要低头。"],
        ("重量", "正"): ["收起来很轻，单肩就能拎走。", "背到营地没觉得压肩。"],
        ("重量", "中"): ["不算轻也不算重，背着能接受。"],
        ("重量", "负"): ["比标称重不少，拎着走一段就累。", "布面和杆都偏厚重。"],
        ("做工", "正"): ["拉链顺滑，走线整齐。", "细节到位，用了一季没开线。"],
        ("做工", "中"): ["中规中矩，能用。"],
        ("做工", "负"): ["拉链用了几次开始涩。", "有两处线头，做工一般。"],
    },
    "sleep": {
        ("保暖", "正"): ["标称 {comfort_temp_c}℃，零下也睡住了。", "夜里温度比标称低两度，还是没冷。"],
        ("保暖", "中"): ["标称 {comfort_temp_c}℃，按季节用刚好。"],
        ("保暖", "负"): ["标称 {comfort_temp_c}℃ 偏乐观，实际要加内胆。", "温度一低就扛不住。"],
        ("重量", "正"): ["压缩后很小，包里不占地方。", "背了三天没觉得多负担。"],
        ("重量", "中"): ["重量正常，收纳后中等大小。"],
        ("重量", "负"): ["收起来还是一大卷，占地方。"],
        ("做工", "正"): ["走线整齐，用了一季没开线。", "拉链顺滑，不夹布。"],
        ("做工", "中"): ["做工一般，不影响用。"],
        ("做工", "负"): ["内衬有两处线头。"],
        ("性价比", "正"): ["这个价位的配置很实在。", "同档里便宜，功能没缩水。"],
        ("性价比", "中"): ["价格合适，谈不上惊喜。"],
        ("性价比", "负"): ["同价能买到更暖的。"],
        ("收纳", "正"): ["自带收纳袋，收起来干净。", "压缩袋一抽就小，好带。"],
        ("收纳", "中"): ["收纳尺寸中等，包能装下。"],
        ("收纳", "负"): ["收纳袋偏小，塞进去费劲。"],
    },
    "light": {
        ("亮度", "正"): ["标称 {lumen} 流明，夜里照得挺远。", "营地一晚够用，找东西不用摸黑。"],
        ("亮度", "中"): ["中档够日常，极暗环境一般。"],
        ("亮度", "负"): ["标称 {lumen} 流明虚高，实际偏弱。"],
        ("续航", "正"): ["低亮度开一整晚还有电。", "两晚没充电撑住了。"],
        ("续航", "中"): ["中亮度能用一晚，带个充电宝更稳。"],
        ("续航", "负"): ["高亮度两小时就掉电。"],
        ("佩戴", "正"): ["头带不勒，走起来不晃。", "重量轻，戴久了也不累。"],
        ("佩戴", "中"): ["戴久了有点印子，调整一下还行。"],
        ("佩戴", "负"): ["头带偏硬，压头发。"],
        ("防水", "正"): ["淋了雨照常亮。", "雾大也没进水。"],
        ("防水", "中"): ["小雨没事，泡水不敢试。"],
        ("防水", "负"): ["进了一点水汽，亮度变暗。"],
        ("做工", "正"): ["外壳结实，摔了一次没坏。"],
        ("做工", "中"): ["做工正常。"],
        ("做工", "负"): ["按键手感松。"],
        ("便携", "正"): ["挂包上不占地方。"],
        ("便携", "中"): ["有点分量，背包侧袋放得下。"],
        ("便携", "负"): ["偏大偏重，不好带。"],
        ("性价比", "正"): ["这个价位亮度算足。"],
        ("性价比", "中"): ["价格合适，功能够用。"],
        ("性价比", "负"): ["同价能买到更亮的。"],
    },
    "carry": {
        ("容量", "正"): ["标称 {volume_l}L，三天装备塞得下。", "侧袋能放水壶，取用顺手。"],
        ("容量", "中"): ["{volume_l}L 装轻装刚好，重装偏紧。"],
        ("容量", "负"): ["标称 {volume_l}L 偏虚，装不满。"],
        ("背负", "正"): ["背着二十斤走了十公里，肩膀没酸。", "腰带传力好，重心稳。"],
        ("背负", "中"): ["十斤以内没问题，重装偏硬。"],
        ("背负", "负"): ["肩带偏窄，装重了压肩。"],
        ("防水", "正"): ["小雨淋了半小时，里面是干的。"],
        ("防水", "中"): ["配了防雨罩才敢淋雨。"],
        ("防水", "负"): ["面料不防水，雨大必须加罩。"],
        ("重量", "正"): ["空包很轻，背着不累。", "轻量化做得不错。"],
        ("重量", "中"): ["自重中等。"],
        ("重量", "负"): ["空包就不轻，加上装备更重。"],
        ("做工", "正"): ["走线整齐，扣件结实。"],
        ("做工", "中"): ["做工正常，用得住。"],
        ("做工", "负"): ["拉链有点涩。"],
    },
    "footwear": {
        ("防水", "正"): ["防水 {waterproof_mm}mm 是实标的，踩水没渗。", "过了小溪，袜子还是干的。"],
        ("防水", "中"): ["小雨没问题，长时间泡水不行。"],
        ("防水", "负"): ["中雨走了一小时，鞋头开始潮。"],
        ("防滑", "正"): ["湿滑石头上没打滑。", "下坡抓地稳，碎石路也敢走。"],
        ("防滑", "中"): ["干地没问题，湿地一般。"],
        ("防滑", "负"): ["青苔石面上滑了一下。"],
        ("包裹", "正"): ["脚踝支撑到位，走久了不晃。", "包裹紧实，长距离脚不累。"],
        ("包裹", "中"): ["按平时尺码买，包裹正常。"],
        ("包裹", "负"): ["鞋楦偏窄，走久了挤脚。"],
        ("重量", "正"): ["上脚比想象轻。"],
        ("重量", "中"): ["重量正常。"],
        ("重量", "负"): ["偏重，走远了累。"],
        ("耐用", "正"): ["走了三百公里，鞋底磨损很小。", "一季下来没开胶。"],
        ("耐用", "中"): ["用了两个月，暂时正常。"],
        ("耐用", "负"): ["两个月鞋头开始起皮。"],
    },
    "apparel": {
        ("保暖", "正"): ["零下也扛住了，风吹不透。", "里面一件打底就够。"],
        ("保暖", "中"): ["春秋够，深冬要加层。"],
        ("保暖", "负"): ["风一吹就透。"],
        ("透气", "正"): ["爬升出汗后不闷。", "腋下开拉链很快就凉。"],
        ("透气", "中"): ["强度不大就还好。"],
        ("透气", "负"): ["出汗后里层有点黏。"],
        ("做工", "正"): ["走线整齐，没有多余线头。", "洗了几次没变形。"],
        ("做工", "中"): ["做工正常。"],
        ("做工", "负"): ["线头多，缝线松。"],
        ("版型", "正"): ["尺码标准，按平时买就行。", "版型宽松，活动不受限。"],
        ("版型", "中"): ["偏正常，不肥不瘦。"],
        ("版型", "负"): ["偏小一码，建议买大。"],
        ("性价比", "正"): ["这个价位配置实在。", "同档里便宜，功能没缩水。"],
        ("性价比", "中"): ["价格合适，谈不上惊喜。"],
        ("性价比", "负"): ["价格偏高，功能没多多少。"],
        ("速干", "正"): ["洗完两小时就干。", "出汗后很快不黏身。"],
        ("速干", "中"): ["晾一晚基本干。"],
        ("速干", "负"): ["汗湿了干得慢。"],
        ("舒适", "正"): ["孩子说穿着不扎。", "贴身穿也舒服。"],
        ("舒适", "中"): ["正常水平，不扎。"],
        ("舒适", "负"): ["领口偏紧，孩子不爱穿。"],
        ("面料", "正"): ["不起球，挺括。"],
        ("面料", "中"): ["手感普通。"],
        ("面料", "负"): ["洗一次就变形。"],
        ("防水", "正"): ["防水 {waterproof_mm}mm，中雨没透。", "压胶严密，淋了半小时里面是干的。"],
        ("防水", "中"): ["小雨够用，大雨要靠雨披。"],
        ("防水", "负"): ["雨大一点肩部就渗。"],
        ("重量", "正"): ["很轻，塞包里几乎无感。"],
        ("重量", "中"): ["重量正常。"],
        ("重量", "负"): ["偏重，带着累。"],
    },
    "daily": {
        ("抗风", "正"): ["大风天没翻面。", "骨架结实，撑得住。"],
        ("抗风", "中"): ["小风没事，大风一般。"],
        ("抗风", "负"): ["风一大就翻。"],
        ("便携", "正"): ["收起来能揣兜里。"],
        ("便携", "中"): ["包里放得下。"],
        ("便携", "负"): ["折叠后还是长，不好带。"],
        ("防水", "正"): ["雨大也没透。"],
        ("防水", "中"): ["小雨够用。"],
        ("防水", "负"): ["雨稍大就渗。"],
        ("做工", "正"): ["伞骨结实，没变形。"],
        ("做工", "中"): ["做工正常。"],
        ("做工", "负"): ["伞骨偏软。"],
        ("性价比", "正"): ["便宜好用。"],
        ("性价比", "中"): ["价格合适。"],
        ("性价比", "负"): ["不值这个价。"],
    },
}

# 兜底：某个方面没有对应句子时，用这类通用说法，避免程序拼出半截话。
FALLBACK = {
    "正": ["整体用下来挺顺，没什么槽点。"],
    "中": ["中规中矩，谈不上惊喜。"],
    "负": ["有点后悔，用着不太顺。"],
}

# 只有 100 多个句子的话，一万条评价会大量重复（实测唯一文本只有 202 条）。
# 所以句子由"情境前缀 + 正文 + 收尾"拼成：同一个正文也能出十几种说法。
CONTEXT = [
    "", "上周用了一次，", "连着用了三晚，", "朋友推荐买的，", "对比之前那款，",
    "给家里人也买了一个，", "第一次买这个牌子，", "看了测评才下手，", "趁着活动入的，",
    "用了一个月，", "带娃出门那次，", "临时补的装备，", "换季清仓时囤的，",
    "同事安利的，", "回购的第二件，", "五一那趟带出去的，", "平时也当日常用，",
    "海边那趟试的，", "山里住了两晚，", "下单前犹豫了很久，",
]
TAIL = {
    "正": ["", "整体满意。", "算是买对了。", "后面还会回购。", "值这个价。", "推荐给朋友了。", "没什么可挑的。"],
    "中": ["", "也就那样。", "谈不上惊喜。", "能用，但不惊艳。", "看需求吧。", "没踩坑也没惊喜。"],
    "负": ["", "不太推荐。", "有点后悔。", "看人品吧。", "这个价位不值。", "下次不买了。", "退货嫌麻烦，凑合用。"],
}

VARIANTS = ["", "升级款", "轻量款", "经典款"]
COLORS = ["", "炭黑", "卡其", "雾蓝"]
REVIEW_RATING = {"正": (5, 5, 5, 4), "中": (3, 3, 4), "负": (2, 2, 3)}


def _sentiment(quality: float, rnd: random.Random) -> str:
    """档位越高，好评概率越高；但任何档位都要有中差评，否则一眼假。"""
    roll = rnd.random()
    positive = 0.45 + 0.40 * quality
    neutral = 0.25 - 0.10 * quality
    if roll < positive:
        return "正"
    if roll < positive + neutral:
        return "中"
    return "负"


def _review_text(category: str, aspect: str, sentiment: str, specs: dict, rnd: random.Random) -> str:
    group = GROUP_OF.get(category, "daily")
    pool = (TEXT.get(group) or {}).get((aspect, sentiment)) or FALLBACK[sentiment]
    candidates = []
    for template in pool:
        if "{" in template:
            try:
                candidates.append(template.format(**specs))
            except (KeyError, IndexError):
                continue
        else:
            candidates.append(template)
    body = rnd.choice(candidates or FALLBACK[sentiment])
    # 情境 + 收尾让同一句正文变出十几种说法，重复率从 1/50 降到接近 1。
    return f"{rnd.choice(CONTEXT)}{body}{rnd.choice(TAIL[sentiment])}"


def _product_name(
    brand: str,
    category: str,
    tier_label: str,
    variant: str,
    color: str,
    series: str,
) -> str:
    """真实电商风格的命名：品牌 + 系列 + 品类叫法 + 颜色/版本。"""
    parts = [brand]
    if series:
        parts.append(series)
    parts.append(_descriptor(category, tier_label))
    for extra in (color, variant):
        if extra:
            parts.append(extra)
    return " ".join(parts)


def build_products(target: int, reviews_per: int, seed: int = 20261105):
    """按矩阵铺满目标数量；名称唯一，规格与评分都由档位推出。"""
    rnd = random.Random(seed)
    products, reviews = [], []
    names = set()

    combos = []
    for category, spec in CATEGORY_DEFS.items():
        tiers = spec["tiers"]
        for tier_index, (tier_label, price, specs) in enumerate(tiers):
            quality = tier_index / max(1, len(tiers) - 1)
            for brand in BRANDS[category]:
                combos.append((category, tier_label, price, specs, brand, quality))

    suffixes = [(variant, color) for variant in VARIANTS for color in COLORS]
    index = 0
    limit = len(combos) * len(suffixes)
    while len(products) < target and index < limit:
        category, tier_label, price, specs, brand, quality = combos[index % len(combos)]
        variant, color = suffixes[index // len(combos)]
        index += 1
        name = _product_name(
            brand, category, tier_label, variant, color,
            _series(category, index + combos.index((category, tier_label, price, specs, brand, quality))),
        )
        if name in names:
            continue
        names.add(name)

        meta = CATEGORY_DEFS[category]
        aspects = meta["aspects"][:]
        rnd.shuffle(aspects)
        chosen = aspects[: min(reviews_per, len(aspects))]
        review_rows = []
        for aspect in chosen:
            sentiment = _sentiment(quality, rnd)
            review_rows.append(
                {
                    "rating": rnd.choice(REVIEW_RATING[sentiment]),
                    "content": _review_text(category, aspect, sentiment, specs, rnd),
                    "aspect": aspect,
                }
            )
        rating = round(sum(row["rating"] for row in review_rows) / len(review_rows), 1)

        products.append(
            {
                "name": name,
                "category": "户外" if meta["subcategory"] in ("露营", "登山") else meta["subcategory"],
                "subcategory": meta["subcategory"],
                "price": round(price * rnd.uniform(0.95, 1.05), 1),
                "waterproof": meta["waterproof"],
                "brand": brand,
                "stock": rnd.randint(5, 120),
                "description": f"{brand}{category}{tier_label}，{meta['subcategory']}场景使用",
                "rating": rating,
                "specs": specs,
                "lead_time_days": lead_time_for(meta["subcategory"]),
                "source": "generated",
            }
        )
        reviews.extend({"product_name": name, **row} for row in review_rows)
    return products, reviews


def _ensure_source_column(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    if "source" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN source TEXT DEFAULT 'seed'")
        conn.execute("UPDATE products SET source = 'seed' WHERE source IS NULL")


def _print_samples(products, reviews, sample: int) -> None:
    by_product = {}
    for review in reviews:
        by_product.setdefault(review["product_name"], []).append(review)
    step = max(1, len(products) // sample)
    shown = products[::step][:sample]
    for product in shown:
        print(
            f"\n【{product['name']}】 {product['brand']} / {product['subcategory']} / "
            f"¥{product['price']:.0f} / 评分 {product['rating']} / 库存 {product['stock']}"
        )
        print(f"  规格：{json.dumps(product['specs'], ensure_ascii=False)}")
        for review in by_product.get(product["name"], []):
            print(f"  · [{review['aspect']}] {review['rating']} 分 —— {review['content']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="按矩阵生成商品目录与评价")
    parser.add_argument("--target", type=int, default=2000, help="目标商品数")
    parser.add_argument("--reviews", type=int, default=5, help="每个商品的评价数")
    parser.add_argument("--sample", type=int, default=20, help="抽样展示数量")
    parser.add_argument("--dry-run", action="store_true", help="只抽样打印，不写库")
    parser.add_argument("--seed", type=int, default=20261105)
    parser.add_argument(
        "--replace-generated",
        action="store_true",
        help="先删除 source='generated' 的旧商品与评价，再灌入新的一批",
    )
    args = parser.parse_args()

    products, reviews = build_products(args.target, args.reviews, args.seed)
    print(f"生成 {len(products)} 件商品 / {len(reviews)} 条评价（零 token，确定性）")

    if args.dry_run:
        _print_samples(products, reviews, args.sample)
        return 0

    if not DB.exists():
        raise SystemExit(f"数据库不存在: {DB}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DB, Path(f"{DB}.backup-catalog-{stamp}"))
    print(f"已备份 -> {DB.name}.backup-catalog-{stamp}")

    conn = sqlite3.connect(DB)
    try:
        _ensure_source_column(conn)
        if args.replace_generated:
            old_ids = [
                row[0] for row in conn.execute("SELECT id FROM products WHERE source = 'generated'")
            ]
            if old_ids:
                conn.executemany("DELETE FROM reviews WHERE product_id = ?", [(i,) for i in old_ids])
                conn.execute("DELETE FROM products WHERE source = 'generated'")
                print(f"已移除上一批生成数据：{len(old_ids)} 件商品及其评价")
        existing = {row[0] for row in conn.execute("SELECT name FROM products")}
        inserted = 0
        for product in products:
            if product["name"] in existing:
                continue
            conn.execute(
                "INSERT INTO products (name, category, subcategory, price, waterproof, brand,"
                " stock, description, rating, specs, lead_time_days, source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    product["name"], product["category"], product["subcategory"], product["price"],
                    product["waterproof"], product["brand"], product["stock"], product["description"],
                    product["rating"], json.dumps(product["specs"], ensure_ascii=False, sort_keys=True),
                    product["lead_time_days"], product["source"],
                ),
            )
            existing.add(product["name"])
            inserted += 1

        id_by_name = {row[0]: row[1] for row in conn.execute("SELECT name, id FROM products")}
        review_rows = [
            (id_by_name[r["product_name"]], f"gen-user-{i % 50:02d}", r["content"], r["rating"])
            for i, r in enumerate(reviews)
            if r["product_name"] in id_by_name
        ]
        conn.executemany(
            "INSERT INTO reviews (product_id, user_id, content, rating) VALUES (?,?,?,?)",
            review_rows,
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        print(f"新增商品 {inserted} 件、评价 {len(review_rows)} 条；库里现有 {total} 件")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
