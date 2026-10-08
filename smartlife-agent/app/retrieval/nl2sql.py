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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Dict, List, Any, Optional
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_small_llm
from app.retrieval.sql_safety import SafeSQLExecutor, SQLSafetyError
from pydantic import BaseModel, Field

class SQLQuery(BaseModel):
    """NL2SQL 输出"""
    sql: str = Field(description="生成的 SQL 查询")
    explanation: str = Field(description="查询解释")
    needs_rag: list[str] = Field(default=[], description="需要 RAG 检索的语义条件")

class NL2SQLChain:
    """NL2SQL 链"""

    MODEL_ENRICHMENT_TIMEOUT_SECONDS = 2.0
    
    DB_SCHEMA = """
数据库表结构：
- products(id, name, category, subcategory, price, waterproof, brand, stock, description, rating, created_at)
- reviews(id, product_id, user_id, content, rating, created_at)

订单和用户数据不通过自由 NL2SQL 访问，只能调用带用户权限的固定 Repository API。

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
        self.sql_executor = SafeSQLExecutor(self.db_path)
        
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
    
    def _build_fuzzy_query(self, user_query: str) -> tuple[str, List[Any]]:
        """构建参数化模糊匹配 SQL。"""
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
        params: List[Any] = []
        
        if keywords:
            # 对每个关键词进行模糊匹配
            keyword_conditions = []
            for kw in keywords:
                keyword_conditions.append("name LIKE ?")
                params.append(f"%{kw}%")
            conditions.append(f"({' OR '.join(keyword_conditions)})")
        
        # 如果有数字，可能是价格范围
        if numbers:
            for num in numbers:
                num_val = int(num)
                if num_val > 10 and num_val < 10000:  # 合理的价格范围
                    conditions.append("price <= ?")
                    params.append(num_val)
                    break
        
        if not conditions:
            # 如果没有提取到条件，返回所有商品
            return "SELECT * FROM products LIMIT 20", []
        
        return f"SELECT * FROM products WHERE {' AND '.join(conditions)} LIMIT 20", params

    def _build_fuzzy_sql(self, user_query: str) -> str:
        """兼容旧接口：返回参数化 SQL 文本。"""
        return self._build_fuzzy_query(user_query)[0]
    
    def _build_keyword_query(self, user_query: str) -> tuple[str, List[Any]]:
        """为明确商品词构建权威参数化查询。

        只使用商品词映射，不把价格数字自动当成商品价格，避免把旅行预算误判为
        商品筛选条件。命中关键词时必须直接查询数据库。
        """
        terms = list(dict.fromkeys(self._expand_terms(user_query)))
        if not terms:
            return "", []
        clauses = []
        params: List[Any] = []
        for term in terms:
            clauses.append("(name LIKE ? OR subcategory LIKE ?)")
            params.extend((f"%{term}%", f"%{term}%"))
        return (
            f"SELECT * FROM products WHERE {' OR '.join(clauses)} LIMIT 200",
            params,
        )

    @staticmethod
    def _deduplicate_products(products: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int]:
        unique: List[Dict[str, Any]] = []
        seen = set()
        duplicates = 0
        for product in products:
            if not isinstance(product, dict):
                continue
            identity = (
                ("id", str(product.get("id")))
                if product.get("id") is not None
                else (
                    "fingerprint",
                    str(product.get("name", "")),
                    str(product.get("brand", "")),
                    str(product.get("price", "")),
                )
            )
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            unique.append(product)
        return unique, duplicates

    def _invoke_model_enrichment(self, payload: Dict[str, Any]) -> SQLQuery:
        """关键词结果 已有权威商品时，给 NL2SQL 增强一个很短的总预算。"""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nl2sql-enrichment")
        future = executor.submit(self.chain.invoke, payload)
        try:
            return future.result(timeout=self.MODEL_ENRICHMENT_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            raise TimeoutError(
                f"NL2SQL 增强超过 {self.MODEL_ENRICHMENT_TIMEOUT_SECONDS:.3g}s 未返回"
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def query(self, user_query: str, max_retries: int = 1) -> Dict[str, Any]:
        """执行商品查询：关键词直查与 NL2SQL 都执行，并对商品结果去重。

        关键词命中时直接查询数据库；无论是否命中，都继续运行一次小模型，
        以免遗漏用户提到的其他条件或商品。返回的每个商品行都来自数据库。
        """
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数")

        keyword_sql, keyword_params = self._build_keyword_query(user_query)
        keyword_results: List[Dict[str, Any]] = []
        keyword_error = ""
        if keyword_sql:
            try:
                keyword_results = self.sql_executor.execute(keyword_sql, keyword_params)["rows"]
            except Exception as exc:
                keyword_error = f"{type(exc).__name__}: {exc}"

        model_results: List[Dict[str, Any]] = []
        model_sql = ""
        model_explanation = ""
        model_needs_rag: List[str] = []
        model_error = ""
        failure_stage = "nl2sql_model"
        model_execution_succeeded = False
        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                if keyword_results:
                    sql_result = self._invoke_model_enrichment({"user_query": user_query})
                else:
                    sql_result = self.chain.invoke({"user_query": user_query})
            except Exception as exc:
                last_error = exc
                model_error = f"{type(exc).__name__}: {exc}"
                failure_stage = (
                    "nl2sql_model_timeout"
                    if isinstance(exc, TimeoutError)
                    else "nl2sql_model"
                )
                if keyword_results:
                    break
                continue

            model_sql = sql_result.sql
            model_explanation = sql_result.explanation
            model_needs_rag = list(sql_result.needs_rag or [])
            try:
                execution = self.sql_executor.execute(sql_result.sql)
                model_results = execution["rows"]
                model_execution_succeeded = True
            except Exception as exc:
                last_error = exc
                model_error = f"{type(exc).__name__}: {exc}"
                failure_stage = "sql_execution"
                continue

            if model_results:
                break

        merged_results, duplicate_count = self._deduplicate_products(
            keyword_results + model_results
        )

        errors = [item for item in (keyword_error, model_error) if item]
        if keyword_error and model_error:
            failure_stage = f"keyword_sql_execution+{failure_stage}"
        elif keyword_error:
            failure_stage = "keyword_sql_execution"
        elif model_error:
            pass
        elif not merged_results and not model_execution_succeeded:
            failure_stage = "nl2sql_model"
        elif not merged_results:
            failure_stage = "empty_result"

        query_sources = []
        if keyword_sql:
            query_sources.append("keyword_sql")
        # 小模型路径每次都强制尝试；失败也属于已执行的查询来源。
        query_sources.append("nl2sql")

        warnings: List[str] = []
        if merged_results and errors:
            warnings.append(
                "关键词查询已返回可验证商品；NL2SQL 增强未完成，结构化约束证据可能不完整"
            )
            errors = []

        result = {
            "results": merged_results,
            "count": len(merged_results),
            "authoritative": bool(merged_results),
            "fallback": False,
            "query_sources": query_sources,
            "keyword_sql": keyword_sql,
            "model_sql": model_sql,
            "keyword_count": len(keyword_results),
            "model_count": len(model_results),
            "duplicate_count": duplicate_count,
            "needs_rag": model_needs_rag,
            "explanation": model_explanation or "关键词直接查询",
            "failure_stage": failure_stage if errors or not merged_results else "",
        }
        if errors:
            result["error"] = "；".join(errors)
            result["warnings"] = [
                "部分查询失败，但已返回可验证的数据库商品行；请在回答中披露证据不完整"
            ] if merged_results else []
        elif warnings:
            result["warnings"] = warnings
        if last_error and not model_results:
            result["sql"] = model_sql or keyword_sql
        else:
            result["sql"] = "\n-- keyword_sql\n{}\n-- model_sql\n{}".format(
                keyword_sql,
                model_sql,
            ).strip()
        return result

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
