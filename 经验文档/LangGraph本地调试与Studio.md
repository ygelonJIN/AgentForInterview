# LangGraph 本地调试与 Studio

> **什么时候读**：要起 LangGraph Studio / Agent Server / 改了 `langgraph.json` / 图在 Studio 里报 checkpointer、context、KeyError、BlockingError 之类 / 要在本机跑图而不是跑 Streamlit 界面。
>
> **不用读**：只是想用应用界面聊天，直接 `streamlit run app/main.py`。

---

## 1. 先分清三件事

| 要做的事 | 命令 | 端口 | 产物 |
|---|---|---|---|
| 用应用界面 | `streamlit run app/main.py` | 8501 | 聊天 UI |
| 验证改动没坏 | `python -m pytest tests -q` | — | 测试结果 |
| 看图的内部流转 | `langgraph dev --allow-blocking` | 2024 | Studio 可视化 |

三者互不替代。界面能跑不代表图在 Studio 里能跑，反之亦然——Studio 是独立进程加载图，不经过 Streamlit 的初始化链路。

---

## 2. 日常运行：应用界面

一条命令（脚本内部已含 `cd` + 激活）：

```zsh
bash "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent/run.sh"
```

手动等价三步：

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent"
```
```zsh
source venv312/bin/activate
```
```zsh
streamlit run app/main.py
```

浏览器打开 `http://localhost:8501`，`Ctrl+C` 停止。

端口被占用时指定其他端口：

```zsh
streamlit run app/main.py --server.port 8502
```

第一次启动要加载 embedding 模型和向量库，慢十几秒属正常。

---

## 3. 跑测试

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && venv312/bin/python -m pytest tests -q
```

关注最后一行 `N passed`。中间出现的 `missing ScriptRunContext` 是 Streamlit 裸跑时的正常警告，可以忽略。

**每次动依赖、改 `context.py` / `state.py` / 图结构之后必须重跑。**

---

## 4. 起 LangGraph Studio

### 前提：CLI 是独立包

`langgraph` 这个包**不含任何命令行工具**。`langgraph` 命令来自另一个包 `langgraph-cli`，且必须带 `[inmem]` 附加项：

```zsh
source venv312/bin/activate && python -m pip install -U "langgraph-cli[inmem]"
```

判断是否装好：

```zsh
ls venv312/bin | grep langgraph
```

### 配置文件 `langgraph.json`

放在项目根，三个键：

```json
{
  "dependencies": ["."],
  "graphs": {
    "turn": "./app/graphs/studio_entry.py:turn_graph"
  },
  "env": ".env"
}
```

`graphs` 的值是 `路径:函数名`，函数**必须零参**（原因见 §5）。

### 启动

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && source venv312/bin/activate && langgraph dev --allow-blocking
```

`--allow-blocking` 不能省（原因见 §6）。启动后终端会打印三个地址：

- API：`http://127.0.0.1:2024`
- Studio UI：`https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024`
- API 文档：`http://127.0.0.1:2024/docs`

用 Chrome 打开 Studio 链接。**Safari 会拦 localhost 连接**，要加 `--tunnel` 并手动允许来源，麻烦，不建议。

### 停止与重启

```zsh
pkill -f "langgraph dev"
```

**改了 `langgraph.json` 必须重启。** 热重载（watchfiles）只监视 `.py` 文件，配置是启动时读入的，改配置不重启会一直用旧路径——现象是明明改了入口，日志里还在 import 旧文件。

### 健康检查

```zsh
curl -s http://127.0.0.1:2024/ok
```

返回 `{"ok":true,...}` 即服务就绪。查图是否加载成功：

```zsh
curl -s -X POST http://127.0.0.1:2024/assistants/search -H "Content-Type: application/json" -d '{"limit":5}'
```

返回里能看到 `"graph_id":"turn"` 就说明图注册成功。

---

## 5. 接入一张图的四条硬约束

每一条都对应一个真实报错，按症状记。

### 5.1 工厂函数必须零参

**症状**：`Invalid checkpointer provided. Expected an instance of BaseCheckpointSaver ... Received dict.`

**原因**：Agent Server 会按函数签名注入参数。若工厂声明了 `checkpointer` 形参，平台会往里面塞自己的配置 dict，再传给 `graph.compile(checkpointer=dict)` 就炸。

**做法**：单独包一层零参函数，持久化交给平台：

```python
from app.graphs.orchestrator import build_turn_graph

def turn_graph():
    return build_turn_graph(None)
```

### 5.2 `context_schema` 不能有必填字段

**症状**：`TurnContext.__init__() missing 1 required positional argument: 'xxx'`

**原因**：`langgraph.json` 只能传 JSON 上下文，构造不出 Python 对象。dataclass 的第一个字段若是必填，`context_schema(**{})` 直接失败。

**做法**：给默认值，并在 `__post_init__` 里兜底取进程级实例：

```python
@dataclass
class TurnContext:
    services: Any = None

    def __post_init__(self):
        if self.services is None:
            from app.services.turn_services import get_turn_services
            self.services = get_turn_services()
```

显式传 `services` 的老路径行为完全不变，只影响"没传"的情况。

### 5.3 父图必须播种子图要读的状态键

**症状**：`KeyError: 'messages'`，报错位置在某个子图节点里。

**原因**：子图被当节点挂到父图后，输入是**父图的共享状态**。子图 `TypedDict` 里声明了某个键、但父图既没声明也没写入，子图一读就 KeyError。

**做法**：父图状态加字段，并在入口节点播种：

```python
messages = list(state.get("messages") or [])
if not messages:
    messages = [HumanMessage(content=state["user_message"])]
```

注意父图 `TurnState` 也必须声明该字段，否则写入会被丢弃。

### 5.4 装饰器别乱加在异步方法上

**症状**：`XxxService.method() missing 2 required positional arguments: 'a' and 'b'`，明明调用时传了参数。

**原因**：`@property` 加在带参 `async def` 上。访问 `obj.method` 时 Python 只把 `self` 传进去，属性取值就炸，根本到不了 `await obj.method(a, b)` 那一行。

**做法**：去掉装饰器。这类错误在应用路径同样会中招，不是 Studio 特有的，尽早修。

---

## 6. 同步存储与 `--allow-blocking`

### 症状
`blockbuster.blockbuster.BlockingError: Blocking call to os.mkdir`（或 `os.stat`、`sqlite3.connect` 等）。

### 原因
Agent Server 跑在 ASGI 事件循环上，装了 blockbuster 检测同步阻塞调用。项目里 `ConversationStore` 用同步 SQLite，`__init__` 里 `mkdir` 就被拦了。

### 处置
两种，按代价从小到大：

1. **开发期直接放行**：`langgraph dev --allow-blocking`
2. **要上生产再改**：把阻塞调用包进 `await asyncio.to_thread(...)`，或换异步驱动

本项目现阶段用第 1 种，够用。

---

## 7. 装 CLI 的风险与验收

`langgraph-cli[inmem]` 会连带拉入 `langgraph-api`、`langgraph-runtime-inmem` 等二十来个包，并把 `protobuf`、`grpcio` 降级——因为它们对 OpenTelemetry / protobuf 有上界约束。

### 必做的验收
装完立刻跑全量测试，确认向量库和 RAG 没被降级影响：

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && venv312/bin/python -m pytest tests -q
```

安装时 pip 会打印 `opentelemetry-*` 的版本冲突警告。这些是**传递依赖的软冲突**，只影响 tracing 上报，不影响图执行和业务逻辑，可以带着用。

### 装坏了怎么回退

```zsh
git checkout -- requirements.lock && python -m pip install -r requirements.lock
```

CLI 本身是开发工具，不属于运行时依赖。介意的话可以先在临时 venv 里验证，再决定要不要装进主环境。

---

## 8. 换模型 / 换供应商

### 三条命令（单行，直接复制）

写入：

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && source venv312/bin/activate && python -c 'from app.config import set_main_config; set_main_config(api_key="apikeyxxxxxxx", base_url="urlxxxxxx", model="modelxxxxxx"); print("已保存")'
```

核对：

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && source venv312/bin/activate && python -c 'from app.config import get_config; c=get_config()["main"]; print("base_url =", c["base_url"]); print("model    =", c["model"]); print("key 长度 =", len(c["api_key"]))'
```

实测：

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && source venv312/bin/activate && python -c 'import asyncio; from app.graphs.orchestrator import build_turn_graph; from app.graphs.context import TurnContext; g=build_turn_graph(None); o=asyncio.run(g.ainvoke({"user_message":"你好","user_id":"t1","session_id":"s1","scene":"general"}, context=TurnContext().with_extra(actor_id="t1", agent_id="general"))); print("route:", o.get("route")); print("response:", str(o.get("response"))[:200])'
```

### 占位符

| 占位符 | 内容 |
|---|---|
| `apikeyxxxxxxx` | 服务商 API Key |
| `urlxxxxxx` | 接口地址，到 `/v1` 这一层，不要带 `/chat/completions` |
| `modelxxxxxx` | 模型名，必须与服务商文档完全一致 |

引号：外层 `'...'` 属 shell、内层 `"..."` 属 Python，两种都保留，只替换引号里的文字。

### embedding 单独看
换供应商时**只改 `main`**。若新供应商不提供 embedding 接口（如 DeepSeek），`embedding` 必须继续指向有向量能力的服务（本项目现在指 DashScope 的 `text-embedding-v3`）。指错会导致 RAG 和记忆检索全部失败，且已入库的向量会因模型不一致被判为不可用。

### 配置存放位置
- `data/config.json`：非敏感项（`base_url`、`model`），可提交
- `data/config.secrets.json`：密钥，已被 `.gitignore` 忽略，不进 Git
- 也可以用环境变量覆盖：`SMARTLIFE_MAIN_API_KEY`、`SMARTLIFE_SMALL_API_KEY`、`SMARTLIFE_EMBEDDING_API_KEY`

### 报错对照

| 现象 | 指向 |
|---|---|
| `401 Invalid API Key` | key 不对或已失效 |
| `404 Not Found` | url 路径错，注意别带 `/chat/completions` |
| `model not found` / `400` | model 名字错 |
| 分类失败但有兜底回复 | 小模型没配，自动降级到主模型，属正常 |

---

## 9. 排错对照表

| 报错关键字 | 章节 |
|---|---|
| `command not found: langgraph` | §4 前提：装 `langgraph-cli[inmem]` |
| `no langgraph.json found` | §4 配置文件 |
| `Received dict`（checkpointer） | §5.1 工厂零参 |
| `missing 1 required positional argument`（Context） | §5.2 上下文兜底 |
| `KeyError: 'messages'` 之类 | §5.3 父图播种 |
| `missing 2 required positional arguments` | §5.4 装饰器误用 |
| `BlockingError` | §6 `--allow-blocking` |
| 改了 json 不生效 | §4 必须重启 |
| Safari 连不上 | §4 换 Chrome |

---

## 10. 命令速查

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && source venv312/bin/activate
```

```zsh
bash "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent/run.sh"
```

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && venv312/bin/python -m pytest tests -q
```

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && source venv312/bin/activate && langgraph dev --allow-blocking
```

```zsh
curl -s http://127.0.0.1:2024/ok
```

```zsh
pkill -f "langgraph dev"
```

---

## 11. 已知边界

- Studio 里跑图和 Streamlit 里跑图走的是同一套节点代码，但配置初始化链路不同。Studio 侧若报缺 key，先确认 `data/config.secrets.json` 或 `.env` 里有值。
- `langgraph dev` 是**开发服务器**，内存态持久化，重启即丢线程历史，不适合当生产部署。
- 本项目图内用同步 SQLite，`--allow-blocking` 是开发期妥协；真要部署到 Agent Server，需要把存储层改成异步或线程池包装。

