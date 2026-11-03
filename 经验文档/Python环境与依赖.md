# Python 环境与依赖

> **什么时候读**：装包报 `command not found` / 新建项目要建环境 / 升级或回退依赖 / 换电脑或外接盘路径变了 / 怀疑机器上有多个重复的 venv / 需要复现别人或过去的精确环境。
>
> **不用读**：日常只要 `pip install` 一个包，且已确认自己在项目 venv 内。

本工作区对应环境：`smartlife-agent/venv312`（Python 3.12.15，来自 `/opt/homebrew/bin/python3.12`）。

---

## 1. `pip: command not found` 但 Python 明明装着

### 现象
`pip install xxx` 报 `zsh: command not found: pip`，而 `python3 --version` 正常。

### 原因
pip 一直存在，只是以**模块**形式存在，没有对应的**可执行文件**在 PATH 里。macOS 系统自带的 `/usr/bin/python3` 不提供 `pip` 这个命令名；即便用 `python3 -m pip install -U pip` 升级，脚本也是落到 `~/Library/Python/3.x/bin`，那个目录默认不在 PATH。

### 诊断三条

```zsh
command -v python3 && python3 -V
```

```zsh
python3 -m pip --version
```

```zsh
echo $PATH | tr ':' '\n' | grep -i library
```

第二条能出版本号，就说明 pip 模块在，问题只是可执行文件路径。

### 正确姿势
一律用 `<解释器> -m pip`：

- `python -m pip ...`（在 venv 内）
- `python3 -m pip ...`（用系统解释器，仅限排查）
- `/opt/homebrew/bin/python3.12 -m pip ...`（指定版本）

好处是把"用哪个 Python"和"用哪个 pip"锁在同一行，不会装错解释器。

### 铁律
不往 `/usr/bin/python3` 装任何项目依赖。它是 Xcode 工具链的一部分，升级会被覆盖，装进去的东西会散落在用户目录，且和 brew 的 Python 完全隔离。

---

## 2. 一个项目一个 venv：位置、命名、忽略

### 位置
**放在项目根目录里**，不要集中在 `~/venvs/`。项目删了环境跟着删，IDE 也能自动识别。

### 命名
新项目统一 `.venv`（生态通用约定，工具和 `.gitignore` 模板都认识）。已有的 `venv312` 是历史命名，不用改。

### 忽略
项目根 `.gitignore` 确认有：

```gitignore
.venv/
venv*/
```

### 不能做的事
**venv 不能移动，也不能改名。** 里面的 `bin/python`、`bin/pip`、`bin/streamlit` 等脚本头部写的是绝对路径，一动就全废。要换位置只能删掉重建。

**回退**：重建成本很低，`pip install -r requirements.lock` 几分钟搞定，前提是 lock 文件在仓库里。

---

## 3. 建新项目环境的标准步骤

```zsh
mkdir -p ~/projects/项目名 && cd ~/projects/项目名
```

```zsh
/opt/homebrew/bin/python3.12 -m venv .venv
```

```zsh
source .venv/bin/activate
```

```zsh
python -m pip install -U pip
```

先写 `requirements.txt`，再装：

```zsh
python -m pip install -r requirements.txt
```

装完立刻固化：

```zsh
python -m pip freeze > requirements.lock
```

国内加速可一次性写全局源，省得每条命令带 `-i`：

```zsh
mkdir -p ~/.pip && printf '[global]\nindex-url = https://pypi.tuna.tsinghua.edu.cn/simple\ntrusted-host = pypi.tuna.tsinghua.edu.cn\n' > ~/.pip/pip.conf
```

---

## 4. 外接盘或换机器后环境失效

### 症状
`run.sh` 一跑就报 `bad interpreter: No such file or directory`，或者 `python` 直接不存在。

### 原因
venv 里所有脚本的 shebang 是绝对路径。项目挂在 `/Volumes/盘名/...` 上时，盘名、挂载点、机器不同都会让路径对不上。这不是环境坏了，是路径变了。

### 处置
不用修，直接重建：

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && rm -rf venv312 && /opt/homebrew/bin/python3.12 -m venv venv312 && source venv312/bin/activate && python -m pip install -r requirements.lock
```

**执行前确认**：`requirements.lock` 存在且在版本控制里，否则删了环境就装不回原样。

### 预防
项目代码和环境尽量同处一盘。如果外接盘经常插拔，把项目整体放本机，外接盘只存数据。

---

## 5. 两份依赖清单：哪份是真值

| 文件 | 作用 | 谁读 |
|---|---|---|
| `requirements.txt` | 你声明的范围，如 `langgraph>=0.1.0` | 人 |
| `requirements.lock` | `pip freeze` 出来的精确版本 | 机器、换机器复现 |

**真正约束版本的是 lock。** `>=0.1.0` 这种写法几乎不约束任何东西，装出来是最新版。

### lock 会过期
只在"改依赖时"重新生成，很容易忘。判断方法：直接对比 `pip freeze` 输出和文件内容，有差异就说明过期。

本项目踩过一次：旧 lock 缺了 `langchain`、`langchain-community`、`langchain-experimental`、`SQLAlchemy` 四项，但环境里实实在在装着。说明是更早一次 freeze 的产物，之后装了东西没重新生成。

### pip 26 的 freeze 行为变化
新版 pip 的 `pip freeze` 不再把 `pip` 自身写进结果，包名也按元数据规范化（`pyjwt` → `PyJWT`、`et-xmlfile` → `et_xmlfile`）。这些是格式差异，没有功能影响，不要当成环境被改动。

---

## 6. 升级依赖的固定五步

顺序不能变，核心是**先看影响面，再动手，最后才改 lock**。

**第一步，预演，看会动几个包**

```zsh
python -m pip install -U 包名 --dry-run
```

看输出末尾的 `Would install ...`。如果里面出现你不认识的、或多个核心包（如 `langchain-core`、`pydantic`）被连带升降，先停一下评估。

**第二步，装**

```zsh
python -m pip install -U 包名
```

**第三步，模块导入冒烟**

比跑测试更快暴露"装了但 import 就炸"的问题。本项目的做法是把 `app/` 下所有模块 import 一遍，统计成功/失败数。

**第四步，跑全量测试**

```zsh
python -m pytest tests -q
```

**第五步，更新 lock**

```zsh
python -m pip freeze > requirements.lock
```

改完用 `git diff` 看一眼有多少变化，只应包含本次涉及的包（外加 §5 说的格式规范化差异）。

### 要不要留备份
默认不留，靠 git 历史回退。想留就 `cp requirements.lock requirements.lock.bak-$(date +%Y%m%d)`，但这类文件不该进仓库，记得加 `.gitignore` 或收工前删掉。

### 升坏了怎么回退

```zsh
git checkout -- requirements.lock && python -m pip install -r requirements.lock
```

lock 是唯一可信的回退依据，所以第五步之前别把旧 lock 覆盖掉。

---

## 7. 找出机器上的重复环境和孤立环境

### 全盘扫

```zsh
find ~ "/Volumes/TUF ESD-T1A Media/xagent" -maxdepth 4 -name pyvenv.cfg 2>/dev/null
```

每个 `pyvenv.cfg` 就是一个 venv。再逐一看包里有什么：

```zsh
du -sh 路径 && 路径/bin/python -V && ls 路径/lib/*/site-packages | grep -ic 关键字
```

### 判断"重复"还是"另一个项目的"

三个问题，任一为否就可能是孤立环境：

1. 有脚本/配置引用它吗（如 `run.sh` 里的 `source ...`）？
2. 有对应的 lock 文件吗？
3. 最近跑过、能跑吗？

三条都不满足，基本就是一次性试验留下的。删之前先 `ls -d` 确认绝对路径就是你要删的那个。

### 注意沙箱权限
会话的写权限只覆盖工作区。删家目录下的东西会被拦（`Operation not permitted`），需要一次性提权，不是命令写错了。

### 缓存是共享的，别为省空间共用环境
- pip wheel 缓存：`python -m pip cache dir` / `python -m pip cache info`
- HuggingFace 模型缓存：`du -sh ~/.cache/huggingface`

同版本的包第二次装几乎不联网；模型权重全局只存一份。真正占空间的是 `torch`、`transformers` 这类，而 langgraph 全家桶只有几十 MB，不值得为它共用环境。

---

## 8. 命令速查

```zsh
cd "/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent" && source venv312/bin/activate
```

```zsh
which python pip
```

```zsh
python -m pip list | grep -iE "关键字"
```

```zsh
python -m pytest tests -q
```

```zsh
deactivate
```

---

## 9. 给命令的格式约定

本机 zsh 多行粘贴只执行第一行，所以交付命令时：

- **一律单行**，能一行写完就不给多行
- 长命令用 `&&` 连接，不要用反斜杠续行
- 需要替换的地方用统一占位符：`apikeyxxxxxxx`、`urlxxxxxx`、`modelxxxxxx`、`TARGET`、`PATH`
- 命令里出现脚本内容时，优先 `python -c '单行代码'` 而不是 heredoc

引号规则：外层 `'...'` 属于 shell，内层 `"..."` 属于 Python，**两种都要保留，只替换引号里的文字**。

