# 导入模板说明

## 数据库 CSV

- `products.csv`：商品主表，必填 `name,category,price`。
- `reviews.csv`：商品评价，必填 `product_id,user_id,content`，`rating` 为 1–5。
- `orders.csv`：订单，必填 `user_id,product_id`。
- `users.csv`：用户，必填 `id,name`。

导入命令：

```bash
./venv312/bin/python scripts/import_data.py db --table products --file data/import_templates/products.csv --dry-run
```

去掉 `--dry-run` 才会写入。`data/init_db.py` 是破坏性初始化脚本，不要用于增量导入。

## RAG 文档

RAG 支持 `CSV、TXT、Markdown、JSONL、PDF、DOCX、Excel`。JSONL 每行格式：

```json
{"content":"文档正文","product_id":31,"user_id":"user_001","rating":5}
```

命令：

```bash
./venv312/bin/python scripts/import_data.py rag data/import_templates/reviews.jsonl --source-type reviews --mode sync
```

`sync` 会替换同一来源文件的旧向量，`add` 只追加。导入后会自动切片、Embedding 并写入 Chroma。
