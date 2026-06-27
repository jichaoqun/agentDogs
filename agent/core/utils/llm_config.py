"""LLM configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "llm.yaml"
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when the LLM configuration is invalid."""


@dataclass(slots=True)
class ProviderConfig:
    enabled: bool = True
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout: float = 60.0
    temperature: float = 0.7
    max_tokens: int = 1024
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchConfig:
    enabled: bool = False
    provider: str = "duckduckgo"
    max_results: int = 5
    fetch_pages: int = 3
    timeout: float = 10.0
    user_agent: str = "AgentDogs/0.1"


@dataclass(slots=True)
class AppConfig:
    system_prompt: str
    max_history_messages: int
    providers: dict[str, ProviderConfig]
    source: Path
    default_provider: str = "builtin"
    default_model: str = "builtin"
    search: SearchConfig = field(default_factory=SearchConfig)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"环境变量 {name} 未设置")

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _provider_config(name: str, data: dict[str, Any], source: Path) -> ProviderConfig:
    known = {
        "enabled", "model", "base_url", "api_key", "timeout",
        "temperature", "max_tokens",
    }
    model = str(data.get("model", ""))
    if name == "builtin" and model:
        model_path = Path(model)
        if not model_path.is_absolute():
            model = str((source.parent / model_path).resolve())
    return ProviderConfig(
        enabled=bool(data.get("enabled", True)),
        model=model,
        base_url=str(data.get("base_url", "")).rstrip("/"),
        api_key=str(data.get("api_key", "")),
        timeout=float(data.get("timeout", 60)),
        temperature=float(data.get("temperature", 0.7)),
        max_tokens=int(data.get("max_tokens", 1024)),
        extra={key: value for key, value in data.items() if key not in known},
    )


def _search_config(data: dict[str, Any] | None) -> SearchConfig:
    source = data or {}
    if not isinstance(source, dict):
        raise ConfigError("search 必须是对象")
    return SearchConfig(
        enabled=bool(source.get("enabled", False)),
        provider=str(source.get("provider", "duckduckgo")),
        max_results=max(1, min(int(source.get("max_results", 5)), 20)),
        fetch_pages=max(0, min(int(source.get("fetch_pages", 3)), 10)),
        timeout=max(1.0, min(float(source.get("timeout", 10.0)), 60.0)),
        user_agent=str(source.get("user_agent", "AgentDogs/0.1")),
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    source = Path(path or os.getenv("AGENT_LLM_CONFIG", DEFAULT_CONFIG_PATH)).resolve()
    if not source.is_file():
        raise ConfigError(f"配置文件不存在: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 格式错误: {exc}") from exc
    raw = _expand_env(raw)
    if not isinstance(raw, dict):
        raise ConfigError("配置文件根节点必须是对象")

    provider_data = raw.get("providers", {})
    if not isinstance(provider_data, dict):
        raise ConfigError("providers 必须是对象")
    providers = {
        name: _provider_config(name, provider_data.get(name, {}), source)
        for name in ("api", "ollama", "builtin")
    }
    if not any(provider.enabled for provider in providers.values()):
        raise ConfigError("至少需要启用一个模型后端")

    default_data = raw.get("default_model", {}) or {}
    if not isinstance(default_data, dict):
        raise ConfigError("default_model 必须是对象")
    default_provider = str(default_data.get("provider", "builtin"))
    if default_provider not in providers:
        raise ConfigError(f"默认 Provider 不存在: {default_provider}")
    if not providers[default_provider].enabled:
        raise ConfigError(f"默认 Provider 未启用: {default_provider}")
    default_model = str(default_data.get("model", "builtin"))
    if not default_model:
        default_model = providers[default_provider].model

    history = raw.get("history", {}) or {}
    max_history = int(history.get("max_messages", 40))
    if max_history < 2:
        raise ConfigError("history.max_messages 不能小于 2")
    return AppConfig(
        system_prompt=str(raw.get("system_prompt", "你是一个专业、可靠的智能助手。")),
        max_history_messages=max_history,
        providers=providers,
        source=source,
        default_provider=default_provider,
        default_model=default_model,
        search=_search_config(raw.get("search")),
    )
