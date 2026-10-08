"""
SmartLife Agent - 统一配置模块

API Key 只允许存在于当前进程内存或环境变量中。磁盘配置仅保存非敏感的
base_url/model，避免再次把凭据写入 Git 跟踪文件。
"""
import os
import json
from langchain_openai import ChatOpenAI

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")

_PERSISTED_FIELDS = ("base_url", "model")
_RUNTIME_KEYS = ("api_key", "base_url", "model")
_SECTIONS = ("main", "small", "embedding")

# 主模型配置
_main = dict.fromkeys(_RUNTIME_KEYS, "")
# 小模型配置
_small = dict.fromkeys(_RUNTIME_KEYS, "")
# Embedding模型配置
_embedding = dict.fromkeys(_RUNTIME_KEYS, "")


def _secrets_file() -> str:
    return os.path.join(os.path.dirname(CONFIG_FILE), "config.secrets.json")


def _public_config(config: dict) -> dict:
    return {field: config.get(field, "") for field in _PERSISTED_FIELDS}


def _secret_config(config: dict) -> dict:
    return {"api_key": config.get("api_key", "")}


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
    if section not in set(_SECTIONS):
        raise ValueError("section 必须是 main、small 或 embedding")
    config = {"main": _main, "small": _small, "embedding": _embedding}[section]
    return bool(_env_api_key(section, config.get("api_key", "")))


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    if path == _secrets_file():
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _save_secrets() -> None:
    payload = {
        section: _secret_config(config)
        for section, config in (
            ("main", _main),
            ("small", _small),
            ("embedding", _embedding),
        )
    }
    if any(item.get("api_key") for item in payload.values()):
        _write_json(_secrets_file(), payload)


def _load_config():
    """加载非敏感配置，并把旧 config.json 中的密钥迁移到 secrets 文件。"""
    global _main, _small, _embedding
    runtime = {"main": _main, "small": _small, "embedding": _embedding}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError):
            data = {}
        migrated = False
        for section in _SECTIONS:
            values = data.get(section, {}) if isinstance(data, dict) else {}
            if not isinstance(values, dict):
                continue
            runtime[section].update(_public_config(values))
            legacy_key = values.get("api_key", "")
            if legacy_key:
                runtime[section]["api_key"] = str(legacy_key)
                values.pop("api_key", None)
                migrated = True
        if migrated:
            _save_secrets()
            try:
                _write_json(CONFIG_FILE, data)
            except OSError:
                pass

    secrets_path = _secrets_file()
    if os.path.exists(secrets_path):
        try:
            with open(secrets_path, "r", encoding="utf-8") as handle:
                secrets = json.load(handle)
            for section in _SECTIONS:
                values = secrets.get(section, {}) if isinstance(secrets, dict) else {}
                if isinstance(values, dict) and values.get("api_key"):
                    runtime[section]["api_key"] = str(values["api_key"])
        except (OSError, ValueError, TypeError):
            return


def _save_config():
    """保存非敏感配置和权限隔离的本地 secrets；均不进入 Git。"""
    try:
        payload = {
            section: _public_config(config)
            for section, config in (
                ("main", _main),
                ("small", _small),
                ("embedding", _embedding),
            )
        }
        _write_json(CONFIG_FILE, payload)
        _save_secrets()
    except OSError:
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

def get_embedding_identity() -> dict:
    """返回非敏感的 Embedding 身份，用于阻止不同模型的向量混用。"""
    eff = _get_effective_embedding()
    return {
        "provider": "dashscope" if "dashscope" in eff.get("base_url", "") else "openai-compatible",
        "base_url": eff.get("base_url", ""),
        "model": eff.get("model", ""),
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
    try:
        request_timeout = max(
            1.0,
            float(os.environ.get("SMARTLIFE_SMALL_MODEL_TIMEOUT_SECONDS", "8")),
        )
    except ValueError:
        request_timeout = 8.0
    kwargs = {
        "model": eff["model"],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "api_key": eff["api_key"],
        # 小模型只负责短 SQL/分类任务；单次 8s、无客户端重试，
        # 确保不会与 RetrievalService 的数据源截止时间相互放大。
        "timeout": request_timeout,
        "max_retries": 0,
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
