# 安全收尾

## 已完成

- `data/config.json` 允许本地持久化 API Key，但该文件已移出 Git 跟踪并被忽略。
- `.env`、`.env.*`、私钥文件已加入忽略规则。
- 应用日志会自动脱敏 API Key、Authorization、Password、Token。
- `scripts/security_check.py` 可扫描工作树和 Git 历史。

## 必须由你完成

1. 在模型服务商控制台轮换所有曾写入过 `data/config.json` 的主模型、小模型和 Embedding Key。
2. 确认旧 Key 已失效后，备份仓库：

   ```bash
   git clone --mirror https://github.com/ygelonJIN/AgentForInterview.git /tmp/AgentForInterview-backup.git
   ```

3. 在仓库副本上清理历史：

   ```bash
   git filter-repo --path smartlife-agent/data/config.json --invert-paths --force
   ```

4. 只有在确认备份可用、协作者知情后，才强推远端：

   ```bash
   git remote add origin https://github.com/ygelonJIN/AgentForInterview.git
   git push --force --all origin
   git push --force --tags origin
   ```

5. 重新扫描：

   ```bash
   ./venv312/bin/python scripts/security_check.py
   ```

历史重写会改变所有提交哈希，必须先完成 Key 轮换，且不能在未备份的情况下执行。

## 外部工具人工验证

```bash
source venv312/bin/activate
python scripts/verify_external_tools.py
```

检查输出中的 `provider_status` 和 `simulated` 字段：

- `simulated: false`：走配置的真实 provider。
- `simulated: true`：当前没有配置 provider，或 provider 调用失败，使用本地模拟。

需要配置的环境变量：

- `SMARTLIFE_WEATHER_API_URL`
- `SMARTLIFE_ROUTE_API_URL`
- `SMARTLIFE_HOTEL_API_URL`
- 对应可选的 `*_API_KEY`
