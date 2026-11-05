"""商品规格目录（纯数据，无依赖）。

商品表原本只有价格、库存和一段自然语言描述。规格匹配要比较数值
（温标、容量、防水），所以需要结构化的 ``specs``。本文件是**人工核对过**
的权威值，种子数据与迁移脚本都读它，不要在两处各写一份。

没有收录的商品不填规格，由校验器判"规格未知"并如实披露——不猜。

``CATEGORY_LEAD_TIME`` 是采购时效（天），为"到货赶不赶得上出发"预留，
当前只落库、还没有消费方。
"""

# 键必须与商品名完全一致；改名时要同步，否则规格会静默丢失。
PRODUCT_SPECS = {
    "四季保暖睡袋": {"comfort_temp_c": -5},
    "双人防水帐篷": {"capacity_persons": 2},
    "户外登山背包40L": {"volume_l": 40},
    "登山冲锋衣": {"waterproof_mm": 28000},
    "防水登山鞋": {"waterproof_mm": 20000},
    "防水透气跑步鞋": {"waterproof_mm": 15000},
    # 补档：露营规则要求、但库里一直缺的品类；同时给帐篷/睡袋/防潮垫
    # 一个更高档的备选，预算有余额时才有东西可升级。
    "蛋巢防潮垫": {"r_value": 2.2},
    "充气加厚防潮垫": {"r_value": 4.0},
    "户外头灯": {"lumen": 300},
    "高亮营地灯": {"lumen": 800},
    "三人自动帐篷": {"capacity_persons": 3},
    "冬季羽绒睡袋": {"comfort_temp_c": -15},
}

# 大件户外要调货，电子多为同城仓，服装居中。
CATEGORY_LEAD_TIME = {
    "电子": 1,
    "服装": 3,
    "户外": 5,
}
DEFAULT_LEAD_TIME = 3


def lead_time_for(category):
    """按品类给采购时效；认不出的品类按默认值，不当作 0 天。"""
    return CATEGORY_LEAD_TIME.get(str(category or ""), DEFAULT_LEAD_TIME)


def specs_for(name):
    """只查目录；没收录返回 None，便于区分"没收录"和"收录了但为空"。"""
    return PRODUCT_SPECS.get(str(name or ""))
