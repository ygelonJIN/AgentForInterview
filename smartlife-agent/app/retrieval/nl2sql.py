"""
NL2SQL 模块 - 自然语言到 SQL 转换

改进：
1. 更详细的提示词和示例
2. 空结果时自动重试/改写
3. 用户术语到数据库分类的映射
4. 更好的模糊匹配
"""
import sqlite3
import os
import re
from typing import Dict, List, Any, Optional
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_small_llm
from pydantic import BaseModel, Field

class SQLQuery(BaseModel):
    """NL2SQL 输出"""
    sql: str = Field(description="生成的 SQL 查询")
    explanation: str = Field(description="查询解释")
    needs_rag: list[str] = Field(default=[], description="需要 RAG 检索的语义条件")

class NL2SQLChain:
    """NL2SQL 链"""
    
    DB_SCHEMA = """
数据库表结构：
- products(id, name, category, subcategory, price, waterproof, brand, stock, description, rating, created_at)
- reviews(id, product_id, user_id, content, rating, created_at)
- orders(id, user_id, product_id, quantity, status, total_price, created_at)
- users(id, name, age, preferences, budget, created_at)

商品分类体系：
- 服装: 
  - 男装: T恤, 衬衫, 裤子, 外套
  - 女装: 连衣裙, 上衣, 裙子, 外套
  - 运动: 跑步鞋, 运动裤, 瑜伽服
- 户外: 
  - 露营: 帐篷, 睡袋, 炉具
  - 登山: 登山杖, 登山鞋, 背包
  - 水上: 泳衣, 浮潜装备
- 电子: 
  - 手机: iPhone, 小米, 华为
  - 电脑: 笔记本, 台式机, 平板

商品名称示例：
- 鞋类: 跑步鞋, 登山鞋, 运动鞋, 凉鞋, 拖鞋
- 服装: T恤, 衬衫, 裤子, 外套, 连衣裙, 裙子
- 户外装备: 帐篷, 睡袋, 登山杖, 背包, 炉具
- 电子产品: 手机, 笔记本, 平板, 耳机, 充电宝
"""
    
    # 用户术语到数据库分类的映射
    TERM_MAPPING = {
        # 鞋类
        "鞋子": ["跑步鞋", "登山鞋", "运动鞋", "凉鞋", "拖鞋"],
        "鞋": ["跑步鞋", "登山鞋", "运动鞋", "凉鞋", "拖鞋"],
        "跑步鞋": ["跑步鞋"],
        "登山鞋": ["登山鞋"],
        "运动鞋": ["运动鞋"],
        "凉鞋": ["凉鞋"],
        "拖鞋": ["拖鞋"],
        
        # 服装
        "衣服": ["T恤", "衬衫", "裤子", "外套", "连衣裙", "上衣", "裙子"],
        "T恤": ["T恤"],
        "衬衫": ["衬衫"],
        "裤子": ["裤子"],
        "外套": ["外套"],
        "连衣裙": ["连衣裙"],
        "上衣": ["上衣"],
        "裙子": ["裙子"],
        
        # 户外装备
        "帐篷": ["帐篷"],
        "睡袋": ["睡袋"],
        "登山杖": ["登山杖"],
        "背包": ["背包"],
        "炉具": ["炉具"],
        "泳衣": ["泳衣"],
        "浮潜装备": ["浮潜装备"],
        
        # 电子产品
        "手机": ["手机", "iPhone", "小米", "华为"],
        "电脑": ["笔记本", "台式机", "平板"],
        "笔记本": ["笔记本"],
        "平板": ["平板"],
        "耳机": ["耳机"],
        "充电宝": ["充电宝"],
    }
    
    def __init__(self, db_path: str = None, model_name: str = None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "products.db")
        self.db_path = db_path
        self.llm = create_small_llm()
        
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", f"""你是一个 SQL 生成器。根据用户的自然语言描述，生成对应的 SQL 查询。

{self.DB_SCHEMA}

规则：
1. 只生成 SELECT 语句
2. 使用参数化查询防止 SQL 注入
3. 结构化条件（价格、分类、品牌）用 SQL 处理
4. 语义条件（风格、口碑、耐用程度）放到 needs_rag 列表
5. 模糊匹配用 LIKE
6. 输出严格的 JSON 格式

示例：
用户："我想买一双鞋子"
SQL: SELECT * FROM products WHERE name LIKE '%鞋%' OR subcategory LIKE '%鞋%'

用户："推荐一款500元以下的手机"
SQL: SELECT * FROM products WHERE category = '电子' AND subcategory = '手机' AND price < 500

用户："有什么防水的户外装备"
SQL: SELECT * FROM products WHERE category = '户外' AND waterproof = 1

用户："我想买一个背包，预算300元"
SQL: SELECT * FROM products WHERE name LIKE '%背包%' AND price <= 300"""),
            ("user", "{user_query}")
        ])
        
        self.chain = self.prompt | self.llm.with_structured_output(SQLQuery)
    
    def _expand_terms(self, user_query: str) -> List[str]:
        """扩展用户术语为数据库中的具体名称"""
        expanded = []
        for term, db_terms in self.TERM_MAPPING.items():
            if term in user_query:
                expanded.extend(db_terms)
        return list(set(expanded))
    
    def _build_fuzzy_sql(self, user_query: str) -> str:
        """构建模糊匹配 SQL"""
        # 提取可能的商品关键词
        keywords = []
        
        # 检查术语映射
        expanded_terms = self._expand_terms(user_query)
        if expanded_terms:
            keywords.extend(expanded_terms)
        
        # 提取数字（可能是价格）
        numbers = re.findall(r'\d+', user_query)
        
        # 构建 SQL
        conditions = []
        
        if keywords:
            # 对每个关键词进行模糊匹配
            keyword_conditions = []
            for kw in keywords:
                keyword_conditions.append(f"name LIKE '%{kw}%'")
            conditions.append(f"({' OR '.join(keyword_conditions)})")
        
        # 如果有数字，可能是价格范围
        if numbers:
            for num in numbers:
                num_val = int(num)
                if num_val > 10 and num_val < 10000:  # 合理的价格范围
                    conditions.append(f"price <= {num_val}")
                    break
        
        if not conditions:
            # 如果没有提取到条件，返回所有商品
            return "SELECT * FROM products LIMIT 20"
        
        return f"SELECT * FROM products WHERE {' AND '.join(conditions)} LIMIT 20"
    
    def query(self, user_query: str, max_retries: int = 2) -> Dict[str, Any]:
        """
        执行 NL2SQL 查询
        
        Args:
            user_query: 用户查询
            max_retries: 最大重试次数
            
        Returns:
            查询结果
        """
        for attempt in range(max_retries + 1):
            try:
                # 使用 LLM 生成 SQL
                sql_result = self.chain.invoke({"user_query": user_query})
                
                # 安全检查
                sql_upper = sql_result.sql.upper().strip()
                if not sql_upper.startswith("SELECT"):
                    return {"error": "只允许 SELECT 查询", "sql_result": sql_result}
                
                # 执行查询
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(sql_result.sql)
                rows = cursor.fetchall()
                results = [dict(row) for row in rows]
                conn.close()
                
                # 如果结果为空，尝试模糊匹配
                if not results and attempt < max_retries:
                    fuzzy_sql = self._build_fuzzy_sql(user_query)
                    conn = sqlite3.connect(self.db_path)
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(fuzzy_sql)
                    rows = cursor.fetchall()
                    results = [dict(row) for row in rows]
                    conn.close()
                    
                    if results:
                        return {
                            "sql": fuzzy_sql,
                            "explanation": f"LLM 查询无结果，使用模糊匹配: {sql_result.explanation}",
                            "needs_rag": sql_result.needs_rag,
                            "results": results,
                            "count": len(results),
                            "fallback": True
                        }
                
                return {
                    "sql": sql_result.sql,
                    "explanation": sql_result.explanation,
                    "needs_rag": sql_result.needs_rag,
                    "results": results,
                    "count": len(results)
                }
                
            except Exception as e:
                if attempt < max_retries:
                    # 尝试模糊匹配
                    try:
                        fuzzy_sql = self._build_fuzzy_sql(user_query)
                        conn = sqlite3.connect(self.db_path)
                        conn.row_factory = sqlite3.Row
                        cursor = conn.cursor()
                        cursor.execute(fuzzy_sql)
                        rows = cursor.fetchall()
                        results = [dict(row) for row in rows]
                        conn.close()
                        
                        return {
                            "sql": fuzzy_sql,
                            "explanation": f"LLM 查询出错，使用模糊匹配: {str(e)}",
                            "needs_rag": [],
                            "results": results,
                            "count": len(results),
                            "fallback": True
                        }
                    except Exception as fallback_error:
                        continue
                else:
                    return {"error": str(e), "sql": sql_result.sql if 'sql_result' in locals() else "", "needs_rag": []}
        
        return {"error": "查询失败", "sql": "", "needs_rag": []}
    
    def query_with_context(self, user_query: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        带上下文的查询
        
        Args:
            user_query: 用户查询
            context: 上下文信息（如之前的对话）
            
        Returns:
            查询结果
        """
        # 如果有上下文，可以增强查询
        enhanced_query = user_query
        if context:
            # 从上下文中提取相关信息
            if "previous_products" in context:
                enhanced_query += f" 之前看过: {', '.join(context['previous_products'])}"
        
        return self.query(enhanced_query)
