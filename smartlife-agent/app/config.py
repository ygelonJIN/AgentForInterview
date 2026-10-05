"""
SmartLife Agent - 统一配置模块

API Key 只允许存在于当前进程内存或环境变量中。磁盘配置仅保存非敏感的
base_url/model，避免再次把凭据写入 Git 跟踪文件。
"""
import os
import json
from langchain_openai import ChatOpenAI

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")

_PERSISTED_FIELDS = ("api_key", "base_url", "model")
_RUNTIME_KEYS = ("api_key", "base_url", "model")

# 主模型配置
_main = dict.fromkeys(_RUNTIME_KEYS, "")
# 小模型配置
_small = dict.fromkeys(_RUNTIME_KEYS, "")
# Embedding模型配置
_embedding = dict.fromkeys(_RUNTIME_KEYS, "")


def _public_config(config: dict) -> dict:
    return {field: config.get(field, "") for field in _PERSISTED_FIELDS}


def _env_api_key(section: str, configured_key: str = "") -> str:
    section_env = {
        "main": "SMARTLIFE_MAIN_API_KEY",
        "small": "SMARTLIFE_SMALL_API_KEY",
        "embedding": "SMARTLIFE_EMBEDDING_API_KEY",
    }[section]
    return (
        configured_key
        or os.environ.get(section_env, "")
        or (os.environ.get("SMARTLIFE_MAIN_API_KEY", "") if section != "main" else "")
        or os.environ.get("OPENAI_API_KEY", "")
    )


def has_api_key(section: str) -> bool:
    """检查运行时或环境变量中是否配置了指定模型的密钥。"""
    if section not in {"main", "small", "embedding"}:
        raise ValueError("section 必须是 main、small 或 embedding")
    config = {"main": _main, "small": _small, "embedding": _embedding}[section]
    return bool(_env_api_key(section, config.get("api_key", "")))

def _load_config():
    """从本地 ignored 配置文件加载配置，包括 API Key。"""
    global _main, _small, _embedding
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for section, config in (("main", _main), ("small", _small), ("embedding", _embedding)):
                values = data.get(section, {})
                if isinstance(values, dict):
                    config.update(_public_config(values))
        except (OSError, ValueError, TypeError):
            # 配置损坏时保持环境变量/内存配置可用，不让敏感字段静默落盘。
            return

def _save_config():
    """保存本地配置到 ignored 文件；不会进入 Git。"""
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        payload = {
            "main": _public_config(_main),
            "small": _public_config(_small),
            "embedding": _public_config(_embedding),
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
    except OSError:
        # 运行时配置仍可用；持久化失败不能把密钥写入其他位置。
        return

# 启动时加载
_load_config()

def set_main_config(api_key: str = "", base_url: str = "", model: str = ""):
    _main["api_key"] = api_key
    _main["base_url"] = base_url
    _main["model"] = model
    _save_config()

def set_small_config(api_key: str = "", base_url: str = "", model: str = ""):
    _small["api_key"] = api_key
    _small["base_url"] = base_url
    _small["model"] = model
    _save_config()

def set_embedding_config(api_key: str = "", base_url: str = "", model: str = ""):
    _embedding["api_key"] = api_key
    _embedding["base_url"] = base_url
    _embedding["model"] = model
    _save_config()

def get_config():
    return {"main": _main.copy(), "small": _small.copy(), "embedding": _embedding.copy()}

def _get_effective_small():
    """获取小模型的有效配置（未填则降级到主模型）"""
    return {
        "api_key": _env_api_key("small", _small["api_key"] or _main["api_key"]),
        "base_url": _small["base_url"] or _main["base_url"],
        "model": _small["model"] or _main["model"],
    }

def _get_effective_embedding():
    """获取embedding模型的有效配置（未填则降级到主模型）"""
    return {
        "api_key": _env_api_key("embedding", _embedding["api_key"] or _main["api_key"]),
        "base_url": _embedding["base_url"] or _main["base_url"],
        "model": _embedding["model"] or "",
    }

def get_embedding_status():
    """获取embedding配置状态，返回 (状态码, 消息)
    状态码: "ok", "fallback", "missing"
    """
    embedding_key = _env_api_key("embedding", _embedding["api_key"] or _main["api_key"])
    if embedding_key and (_embedding["model"] or _main["model"]):
        return "ok", "已配置独立Embedding模型"
    elif _env_api_key("main", _main["api_key"]):
        if _main["base_url"] and "openai" not in _main["base_url"].lower():
            return "warning", f"⚠️ 主模型({ _main['base_url'][:30] }...)可能不支持Embedding，建议单独配置"
        return "fallback", "将使用主模型配置（需要主模型支持Embedding）"
    else:
        return "missing", "未配置任何模型，RAG检索不可用"

def create_llm(model_name: str = None, temperature: float = 0, max_tokens: int = 4096) -> ChatOpenAI:
    kwargs = {
        "model": model_name or _main["model"],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "api_key": _env_api_key("main", _main["api_key"]),
    }
    if _main["base_url"]:
        kwargs["base_url"] = _main["base_url"]
    return ChatOpenAI(**kwargs)

def create_small_llm(temperature: float = 0, max_tokens: int = 500) -> ChatOpenAI:
    eff = _get_effective_small()
    kwargs = {
        "model": eff["model"],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "api_key": eff["api_key"],
    }
    if eff["base_url"]:
        kwargs["base_url"] = eff["base_url"]
    return ChatOpenAI(**kwargs)

def create_embeddings():
    eff = _get_effective_embedding()
    
    if not eff["api_key"]:
        raise ValueError("未配置Embedding模型的API Key，请在配置页面填写")
    
    if not eff["model"]:
        raise ValueError("未配置Embedding模型名称，请在配置页面填写（如: text-embedding-3-small）")
    
    # 优先使用 DashScope 直连（绕过 openai SDK 的 auth bug）
    if "dashscope" in eff.get("base_url", ""):
        from app.embeddings_fix import DashScopeEmbeddings
        return DashScopeEmbeddings(
            api_key=eff["api_key"],
            base_url=eff["base_url"],
            model=eff["model"],
        )
    
    # 其他情况用 langchain_openai
    from langchain_openai import OpenAIEmbeddings
    kwargs = {
        "api_key": eff["api_key"],
        "model": eff["model"],
    }
    if eff["base_url"]:
        kwargs["base_url"] = eff["base_url"]
    return OpenAIEmbeddings(**kwargs)
