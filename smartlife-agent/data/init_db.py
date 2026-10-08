"""
SmartLife Agent - 数据库初始化脚本
创建 SQLite 数据库并插入种子数据
"""
import argparse
import os
import sqlite3
from datetime import datetime

def init_database(force: bool = False):
    data_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(data_dir, "products.db")
    
    backup_path = None
    if os.path.exists(db_path):
        if not force:
            raise RuntimeError(
                f"数据库已存在: {db_path}。该脚本会重建种子数据库；"
                "增量导入请使用 scripts/import_data.py db。"
                "确认重建时请显式传入 --force。"
            )
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{db_path}.backup-{timestamp}"
        os.replace(db_path, backup_path)
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    # 创建表
    c.executescript("""
    CREATE TABLE products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        subcategory TEXT,
        price REAL NOT NULL,
        waterproof BOOLEAN DEFAULT 0,
        brand TEXT,
        stock INTEGER DEFAULT 100,
        description TEXT,
        rating REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        content TEXT NOT NULL,
        rating INTEGER CHECK(rating >= 1 AND rating <= 5),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (product_id) REFERENCES products(id)
    );
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER DEFAULT 1,
        status TEXT DEFAULT 'pending',
        total_price REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (product_id) REFERENCES products(id)
    );
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        age INTEGER,
        preferences TEXT,
        budget REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    # 插入商品 (30+)
    products = [
        # 服装-男装
        ("纯棉圆领T恤", "服装", "男装", 89.0, 0, "优衣库", 200, "100%纯棉面料，透气舒适，经典百搭款", 4.5),
        ("商务休闲衬衫", "服装", "男装", 199.0, 0, "海澜之家", 150, "免烫面料，修身版型，商务休闲两相宜", 4.3),
        ("弹力修身牛仔裤", "服装", "男装", 259.0, 0, "Levi's", 120, "弹力面料修身剪裁，经典直筒版型", 4.4),
        ("轻薄羽绒外套", "服装", "男装", 599.0, 0, "波司登", 80, "90%白鹅绒填充，轻薄保暖，可收纳便携", 4.7),
        # 服装-女装
        ("碎花雪纺连衣裙", "服装", "女装", 299.0, 0, "ZARA", 100, "法式碎花设计，雪纺面料飘逸灵动，春夏必备", 4.6),
        ("针织修身上衣", "服装", "女装", 159.0, 0, "H&M", 180, "弹力针织面料，修身显瘦，多色可选", 4.2),
        ("高腰A字半裙", "服装", "女装", 229.0, 0, "UR", 90, "高腰设计显腿长，A字版型遮肉显瘦", 4.5),
        ("羊毛呢大衣", "服装", "女装", 899.0, 0, "MaxMara", 50, "100%羊毛呢料，经典翻领设计，优雅大气", 4.8),
        # 服装-运动
        ("防水透气跑步鞋", "服装", "运动", 499.0, 1, "Nike", 200, "GORE-TEX防水科技，React缓震中底，适合各种路面", 4.7),
        ("速干运动长裤", "服装", "运动", 189.0, 0, "Adidas", 160, "Climacool速干面料，弹性束脚设计，运动自如", 4.3),
        ("瑜伽服套装", "服装", "运动", 199.0, 0, "Lululemon", 130, "四面弹力面料，排汗速干，高腰收腹设计", 4.6),
        ("专业竞速跑鞋", "服装", "运动", 899.0, 0, "Asics", 80, "GEL缓震胶科技，碳板助推，专业马拉松竞速", 4.8),
        # 户外-露营
        ("双人防水帐篷", "户外", "露营", 599.0, 1, "牧高笛", 60, "双层防水设计，抗风8级，搭建简单快速", 4.5),
        ("四季保暖睡袋", "户外", "露营", 299.0, 1, "黑冰", 80, "鸭绒填充，舒适温度-5°C，压缩收纳便携", 4.6),
        ("便携式卡式炉", "户外", "露营", 129.0, 0, "火枫", 100, "3500W大火力，防风设计，适配标准气罐", 4.4),
        ("户外折叠桌椅套装", "户外", "露营", 359.0, 0, "挪客", 45, "铝合金轻量化设计，折叠便携，承重100kg", 4.3),
        # 户外-登山
        ("碳纤维登山杖", "户外", "登山", 199.0, 0, "BlackDiamond", 150, "碳纤维材质超轻，三节可调节，EVA舒适手柄", 4.5),
        ("防水登山鞋", "户外", "登山", 459.0, 1, "Salomon", 100, "GORE-TEX防水内衬，Contagrip大底抓地力强", 4.7),
        ("户外登山背包40L", "户外", "登山", 329.0, 1, "Osprey", 70, "AirScape背负系统透气，多仓位设计，防雨罩附带", 4.6),
        ("登山冲锋衣", "户外", "登山", 699.0, 1, "始祖鸟", 40, "GORE-TEX Pro面料，全压胶防水，腋下通风拉链", 4.9),
        # 户外-水上
        ("女士连体泳衣", "户外", "水上", 169.0, 1, "Speedo", 120, "抗氯面料持久耐用，UPF50+防晒，修身显瘦", 4.3),
        ("浮潜三件套", "户外", "水上", 259.0, 1, "迪卡侬", 60, "防雾面镜+干式呼吸管+浮潜鞋，适合入门", 4.4),
        # 电子-手机
        ("iPhone 15 Pro Max", "电子", "手机", 6999.0, 0, "Apple", 30, "A17 Pro芯片，钛金属边框，4800万像素主摄", 4.8),
        ("小米14", "电子", "手机", 3999.0, 0, "小米", 80, "骁龙8Gen3处理器，徕卡光学镜头，90W快充", 4.5),
        ("华为Mate 60 Pro", "电子", "手机", 6499.0, 0, "华为", 50, "麒麟9000S芯片，卫星通话，XMAGE影像系统", 4.7),
        # 电子-电脑
        ("MacBook Pro 14", "电子", "电脑", 12999.0, 0, "Apple", 25, "M3 Pro芯片，Liquid Retina XDR显示屏，18小时续航", 4.9),
        ("联想小新Pro 16", "电子", "电脑", 5499.0, 0, "联想", 60, "i7-13700H处理器，2.5K 120Hz屏幕，70Wh大电池", 4.4),
        ("iPad Air", "电子", "电脑", 4799.0, 0, "Apple", 45, "M2芯片，10.9英寸Liquid Retina屏，支持Apple Pencil", 4.6),
        # 额外商品
        ("防晒霜SPF50+", "户外", "水上", 89.0, 1, "安耐晒", 300, "高倍防晒不油腻，防水防汗，适合户外运动", 4.5),
        ("户外保温杯", "户外", "露营", 139.0, 0, "膳魔师", 200, "316不锈钢内胆，保温24小时，500ml大容量", 4.6),
    ]
    
    c.executemany(
        "INSERT INTO products (name, category, subcategory, price, waterproof, brand, stock, description, rating) VALUES (?,?,?,?,?,?,?,?,?)",
        products
    )
    
    # 插入用户 (10)
    users = [
        ("user_001", "张三", 28, "户外运动爱好者，喜欢露营和登山", 5000.0),
        ("user_002", "李四", 25, "时尚达人，关注新品和潮流", 3000.0),
        ("user_003", "王五", 35, "科技发烧友，喜欢尝鲜数码产品", 15000.0),
        ("user_004", "赵六", 30, "健身爱好者，注重运动装备品质", 4000.0),
        ("user_005", "钱七", 22, "大学生，预算有限追求性价比", 1500.0),
        ("user_006", "孙八", 40, "家庭主妇，注重家庭出游和亲子活动", 8000.0),
        ("user_007", "周九", 27, "旅行博主，经常全国各地打卡", 10000.0),
        ("user_008", "吴十", 33, "程序员，偏好电子产品和极简生活", 6000.0),
        ("user_009", "郑十一", 29, "瑜伽教练，关注运动服饰和健康生活", 3000.0),
        ("user_010", "陈十二", 45, "企业高管，注重品质和效率", 20000.0),
    ]
    c.executemany("INSERT INTO users (id, name, age, preferences, budget) VALUES (?,?,?,?,?)", users)
    
    # 插入评价 (50+)
    reviews = [
        # 跑步鞋评价
        ("user_001", "防水性能一流，下雨天跑步完全没问题，脚感舒适", 5),
        ("user_004", "缓震效果很好，跑完半马膝盖不疼，推荐！", 5),
        ("user_005", "鞋子偏小半码，建议买大一号，其他都满意", 4),
        ("user_002", "颜值在线，日常穿搭也好看，GORE-TEX防水确实靠谱", 5),
        ("user_009", "透气性不错，夏天跑10公里脚不闷热", 4),
        ("user_007", "耐磨性还行，跑了300公里鞋底磨损不大", 4),
        # 登山杖评价
        ("user_001", "碳纤维材质真的很轻，长途登山省力不少", 5),
        ("user_007", "手柄握感舒适，调节方便，值得购买", 4),
        ("user_004", "质量不错，已经用了半年没有松动", 4),
        # 帐篷评价
        ("user_001", "双层防水确实靠谱，暴雨中待了一夜没漏水", 5),
        ("user_007", "搭建简单，一个人5分钟搞定，收纳也方便", 5),
        ("user_006", "空间够大，两大一小睡着不挤，透气性好", 4),
        ("user_004", "抗风性不错，山顶露营扛住了大风", 4),
        # 睡袋评价
        ("user_001", "零下5度确实能扛住，鸭绒蓬松度高", 5),
        ("user_007", "压缩后很小巧，登山包里不占地方", 4),
        ("user_004", "面料亲肤舒适，不会起静电", 4),
        # iPhone评价
        ("user_003", "A17 Pro性能强劲，原神全特效流畅运行", 5),
        ("user_008", "钛金属边框手感真好，比不锈钢轻很多", 5),
        ("user_002", "拍照效果一流，夜景模式提升明显", 5),
        ("user_005", "价格太贵了，但确实好用，续航也比上代好", 4),
        # 小米14评价
        ("user_003", "骁龙8Gen3性能拉满，日常使用非常流畅", 5),
        ("user_005", "性价比之王！这个价位能有这个配置太良心了", 5),
        ("user_008", "徕卡影像确实有质感，色彩还原准确", 4),
        ("user_001", "90W快充太方便了，20分钟充80%", 5),
        # MacBook评价
        ("user_008", "M3 Pro芯片剪辑4K视频毫无压力，程序员首选", 5),
        ("user_003", "屏幕素质顶级，XDR显示效果惊艳", 5),
        ("user_010", "续航真的能用一天，出差不用带充电器", 5),
        # 连衣裙评价
        ("user_002", "碎花图案很好看，面料飘逸有质感", 5),
        ("user_009", "版型修身不紧绷，显瘦效果好", 4),
        ("user_006", "做工精细，洗了几次没有褪色", 4),
        # 瑜伽服评价
        ("user_009", "四面弹力做瑜伽动作完全不受限，面料亲肤", 5),
        ("user_004", "速干效果好，高强度训练后很快干", 4),
        ("user_002", "高腰设计收腹效果好，穿着很显身材", 5),
        # 冲锋衣评价
        ("user_001", "始祖鸟品质没得说，GORE-TEX Pro完全防水", 5),
        ("user_007", "透气性比想象中好，高强度运动不闷", 5),
        ("user_004", "做工细节到位，压胶工艺严密", 5),
        # 登山鞋评价
        ("user_001", "Contagrip大底抓地力超强，湿滑岩石上也不打滑", 5),
        ("user_007", "防水性能好，过小溪完全不进水", 5),
        ("user_004", "包裹性强，长时间行走脚不累", 4),
        # 联想电脑评价
        ("user_008", "性价比很高，2.5K屏幕看代码很舒服", 4),
        ("user_003", "性能释放不错，轻薄本里算强劲的", 4),
        ("user_005", "续航一般般，重度使用大概5小时", 3),
        # 保温杯评价
        ("user_001", "保温效果一流，早上装的热水下午还烫嘴", 5),
        ("user_006", "500ml容量刚好，出门带很方便", 4),
        ("user_007", "颜值高，户外拍照也好看", 4),
        # 防晒霜评价
        ("user_009", "防晒效果好，户外瑜伽2小时没晒黑", 5),
        ("user_002", "质地轻薄不油腻，化妆前打底也OK", 4),
        ("user_006", "给孩子涂也放心，温和不刺激", 5),
        # 登山包评价
        ("user_001", "背负系统透气，夏天背也不闷热", 5),
        ("user_007", "40L容量够大，3天露营装备全装下", 5),
        ("user_004", "防雨罩实用，突遇暴雨装备完好", 4),
    ]
    
    # product_id 映射
    review_data = []
    for i, (uid, content, rating) in enumerate(reviews):
        # 循环映射到不同商品
        pid = (i % 30) + 1
        review_data.append((pid, uid, content, rating))
    
    c.executemany(
        "INSERT INTO reviews (product_id, user_id, content, rating) VALUES (?,?,?,?)",
        review_data
    )
    
    # 插入订单 (20)
    orders = [
        ("user_001", 13, 1, 599.0, "completed"),   # 帐篷
        ("user_001", 14, 1, 299.0, "completed"),   # 睡袋
        ("user_001", 17, 1, 199.0, "completed"),   # 登山杖
        ("user_001", 18, 1, 459.0, "shipped"),     # 登山鞋
        ("user_002", 5, 1, 299.0, "completed"),    # 连衣裙
        ("user_002", 7, 1, 229.0, "completed"),    # 半裙
        ("user_002", 9, 1, 499.0, "shipped"),      # 跑步鞋
        ("user_003", 23, 1, 6999.0, "completed"),  # iPhone
        ("user_003", 26, 1, 12999.0, "completed"), # MacBook
        ("user_004", 9, 1, 499.0, "completed"),    # 跑步鞋
        ("user_004", 11, 1, 199.0, "completed"),   # 瑜伽服
        ("user_005", 24, 1, 3999.0, "completed"),  # 小米14
        ("user_005", 1, 2, 178.0, "completed"),    # T恤
        ("user_006", 13, 1, 599.0, "completed"),   # 帐篷
        ("user_006", 16, 1, 359.0, "shipped"),     # 桌椅
        ("user_007", 20, 1, 699.0, "completed"),   # 冲锋衣
        ("user_007", 19, 1, 329.0, "completed"),   # 登山包
        ("user_008", 26, 1, 12999.0, "completed"), # MacBook
        ("user_009", 11, 2, 398.0, "completed"),   # 瑜伽服
        ("user_010", 8, 1, 899.0, "completed"),    # 大衣
    ]
    c.executemany(
        "INSERT INTO orders (user_id, product_id, quantity, total_price, status) VALUES (?,?,?,?,?)",
        orders
    )
    
    conn.commit()
    conn.close()
    if backup_path:
        print(f"原数据库已备份: {backup_path}")
    print(f"数据库初始化完成: {db_path}")
    
    # 验证
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    for table in ["products", "reviews", "orders", "users"]:
        c.execute(f"SELECT COUNT(*) FROM {table}")
        print(f"  {table}: {c.fetchone()[0]} 条记录")
    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="创建带种子数据的全新 products.db；已有数据库需要 --force，且会自动备份。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="备份并重建已有数据库；不要用于增量导入",
    )
    args = parser.parse_args()
    try:
        init_database(force=args.force)
    except RuntimeError as exc:
        parser.exit(2, f"{exc}\n")
