"""
NL2SQL 模块 - 自然语言到 SQL 转换
"""
import sqlite3
import os
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
- 服装: 男装(T恤/衬衫/裤子/外套), 女装(连衣裙/上衣/裙子/外套), 运动(跑步鞋/运动裤/瑜伽服)
- 户外: 露营(帐篷/睡袋/炉具), 登山(登山杖/登山鞋/背包), 水上(泳衣/浮潜装备)
- 电子: 手机(iPhone/小米/华为), 电脑(笔记本/台式机/平板)
"""
    
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
6. 输出严格的 JSON 格式"""),
            ("user", "{user_query}")
        ])
        
        self.chain = self.prompt | self.llm.with_structured_output(SQLQuery)
    
    def query(self, user_query: str) -> Dict[str, Any]:
        """执行 NL2SQL 查询"""
        sql_result = self.chain.invoke({"user_query": user_query})
        
        # 安全检查
        sql_upper = sql_result.sql.upper().strip()
        if not sql_upper.startswith("SELECT"):
            return {"error": "只允许 SELECT 查询", "sql_result": sql_result}
        
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(sql_result.sql)
            rows = cursor.fetchall()
            results = [dict(row) for row in rows]
            conn.close()
            
            return {
                "sql": sql_result.sql,
                "explanation": sql_result.explanation,
                "needs_rag": sql_result.needs_rag,
                "results": results,
                "count": len(results)
            }
        except Exception as e:
            return {"error": str(e), "sql": sql_result.sql, "needs_rag": sql_result.needs_rag}
