"""
SmartLife Agent - 统一配置模块
支持主模型/小模型/embedding模型独立配置 + 持久化 + 自动降级
"""
import os
import json
from langchain_openai import ChatOpenAI

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")

# 主模型配置
_main = {"api_key": "", "base_url": "", "model": ""}
# 小模型配置
_small = {"api_key": "", "base_url": "", "model": ""}
# Embedding模型配置
_embedding = {"api_key": "", "base_url": "", "model": ""}

def _load_config():
    """从文件加载配置"""
    global _main, _small, _embedding
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            if "main" in data:
                _main.update(data["main"])
            if "small" in data:
                _small.update(data["small"])
            if "embedding" in data:
                _embedding.update(data["embedding"])
        except Exception:
            pass

def _save_config():
    """保存配置到文件"""
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump({"main": _main, "small": _small, "embedding": _embedding}, f, indent=2)
    except Exception:
        pass

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
        "api_key": _small["api_key"] or _main["api_key"],
        "base_url": _small["base_url"] or _main["base_url"],
        "model": _small["model"] or _main["model"],
    }

def _get_effective_embedding():
    """获取embedding模型的有效配置（未填则降级到主模型）"""
    return {
        "api_key": _embedding["api_key"] or _main["api_key"],
        "base_url": _embedding["base_url"] or _main["base_url"],
        "model": _embedding["model"] or "",
    }

def get_embedding_status():
    """获取embedding配置状态，返回 (状态码, 消息)
    状态码: "ok", "fallback", "missing"
    """
    if _embedding["api_key"] and _embedding["model"]:
        return "ok", "已配置独立Embedding模型"
    elif _main["api_key"]:
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
        "api_key": _main["api_key"] or os.environ.get("OPENAI_API_KEY", ""),
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
        "api_key": eff["api_key"] or os.environ.get("OPENAI_API_KEY", ""),
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
