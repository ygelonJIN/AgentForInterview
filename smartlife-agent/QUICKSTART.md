# 🚀 SmartLife Agent 快速启动指南

## 一、新 Mac 环境配置

### 1. 安装 Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 2. 安装 Python

```bash
# 安装 Python 3.12（推荐，稳定）
brew install python@3.12
```

### 3. 配置 PATH

在 `~/.zshrc` 中添加：

```bash
export PATH="/opt/homebrew/bin:$PATH"
```

然后运行：

```bash
source ~/.zshrc
python3 --version   # 应显示 3.12.x
pip3 --version
```

### 4. 配置 pip 镜像（可选，加速下载）

```bash
mkdir -p ~/.pip
cat > ~/.pip/pip.conf << 'CONF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
CONF
```

---

## 二、项目启动

### 方案 A：使用虚拟环境（推荐）

```bash
cd '/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent'

# 创建并激活虚拟环境
python3 -m venv venv312
source venv312/bin/activate

# 安装已锁定依赖（可复现环境）
pip install -r requirements.lock -i https://pypi.tuna.tsinghua.edu.cn/simple

# 设置 API Key（可写入本地 data/config.json；该文件已忽略，不要提交 Git）
export OPENAI_API_KEY="your-api-key-here"
# 也可以分别设置：
# export SMARTLIFE_MAIN_API_KEY="..."
# export SMARTLIFE_SMALL_API_KEY="..."
# export SMARTLIFE_EMBEDDING_API_KEY="..."

# 初始化数据库
python data/init_db.py

# 运行
streamlit run app/main.py
```

访问 http://localhost:8501

### 方案 B：使用简化版本（无需 LangChain）

```bash
pip3 install openai streamlit pydantic python-dotenv -i https://pypi.tuna.tsinghua.edu.cn/simple
streamlit run app/simple_main.py
```

### 运行测试

```bash
pytest tests/ -v
```

---

## 三、常见问题

### SSL 证书错误
```bash
pip3 install --trusted-host pypi.org --trusted-host files.pythonhosted.org <package>
```

### PyYAML 安装失败
```bash
pip3 install PyYAML --no-build-isolation
```

### 多个 Python 版本冲突
使用虚拟环境隔离，确保 `/opt/homebrew/bin` 在 PATH 最前面：

```bash
which python3    # 应指向 /opt/homebrew/bin/python3
python3 --version
```

### pip 安装很慢
确认已配置国内镜像源（见第一步第 4 节）。

---

## 四、推荐的 Python 版本管理方式

| 方案 | 适合人群 | 命令 |
|------|---------|------|
| 只用 Homebrew | 新手 | `brew install python@3.12` |
| pyenv | 进阶用户 | `brew install pyenv && pyenv install 3.12.7` |
| conda | 数据科学 | `brew install --cask miniconda` |

---

## 五、功能演示

### 购物场景
```
用户：帮我找一双500块以内的跑步鞋
助手：我来帮您搜索合适的跑步鞋...
```

### 旅游场景
```
用户：我和女朋友周末去杭州玩两天，预算3000
助手：我来为您规划杭州两日游行程...
```
