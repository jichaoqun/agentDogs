"""LLM configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .prompt import DEFAULT_SYSTEM_PROMPT


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "llm.yaml"
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
SUPPORTED_PROVIDER_TYPES = {"openai_compatible", "ollama", "builtin"}


class ConfigError(ValueError):
    """Raised when the LLM configuration is invalid."""


@dataclass(slots=True)
class ProviderConfig:
    type: str = "openai_compatible"
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
class OpenSandboxConfig:
    domain: str = "127.0.0.1:8080"
    protocol: str = "http"
    api_key: str = ""
    request_timeout_seconds: int = 60
    use_server_proxy: bool = False
    ready_timeout_seconds: int = 30


@dataclass(slots=True)
class LocalProcessApprovalScope:
    command_execution: str = "always"
    dependency_install: str = "first_time"
    workspace_write: str = "always"
    network_access: str = "always"


@dataclass(slots=True)
class LocalProcessConfig:
    python_executable: str = ""
    runs_dir: str = "runtime/local_runs"
    deps_dir: str = "runtime/local_deps"
    cleanup_runs: bool = True
    require_human_approval: bool = True
    approval_scope: LocalProcessApprovalScope = field(default_factory=LocalProcessApprovalScope)


@dataclass(slots=True)
class CodeExecutionConfig:
    enabled: bool = False
    backend: str = "opensandbox"
    image: str = "python:3.11-slim"
    timeout_seconds: int = 20
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    network_enabled: bool = False
    workspace_readonly: bool = True
    artifacts_dir: str = "runtime/artifacts"
    max_output_chars: int = 12_000
    dependency_install_enabled: bool = False
    allowed_packages: list[str] = field(default_factory=lambda: [
        "pandas",
        "numpy",
        "openpyxl",
        "matplotlib",
        "seaborn",
        "scipy",
        "scikit-learn",
    ])
    install_timeout_seconds: int = 120
    max_artifacts: int = 20
    max_artifact_bytes: int = 25 * 1024 * 1024
    allow_user_script_execution: bool = False
    opensandbox: OpenSandboxConfig = field(default_factory=OpenSandboxConfig)
    local_process: LocalProcessConfig = field(default_factory=LocalProcessConfig)


@dataclass(slots=True)
class AppConfig:
    system_prompt: str
    max_history_messages: int
    providers: dict[str, ProviderConfig]
    source: Path
    default_provider: str = "builtin"
    default_model: str = "builtin"
    search: SearchConfig = field(default_factory=SearchConfig)
    code_execution: CodeExecutionConfig = field(default_factory=CodeExecutionConfig)


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


def _default_provider_type(name: str) -> str:
    if name == "api":
        return "openai_compatible"
    if name in {"ollama", "builtin"}:
        return name
    return "openai_compatible"


def _provider_config(name: str, data: dict[str, Any], source: Path) -> ProviderConfig:
    known = {
        "type", "enabled", "model", "base_url", "api_key", "timeout",
        "temperature", "max_tokens",
    }
    provider_type = str(data.get("type", _default_provider_type(name))).lower()
    if provider_type not in SUPPORTED_PROVIDER_TYPES:
        supported = ", ".join(sorted(SUPPORTED_PROVIDER_TYPES))
        raise ConfigError(f"Provider '{name}' type '{provider_type}' is not supported; supported: {supported}")
    model = str(data.get("model", ""))
    if provider_type == "builtin" and model:
        model_path = Path(model)
        if not model_path.is_absolute():
            model = str((source.parent / model_path).resolve())
    return ProviderConfig(
        type=provider_type,
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


def _code_execution_config(data: dict[str, Any] | None) -> CodeExecutionConfig:
    source = data or {}
    if not isinstance(source, dict):
        raise ConfigError("code_execution must be an object")
    backend = str(source.get("backend", "opensandbox")).lower()
    if backend not in {"opensandbox", "local_process"}:
        raise ConfigError("code_execution.backend only supports opensandbox or local_process")
    timeout = max(1, min(int(source.get("timeout_seconds", 20)), 300))
    cpu_limit = max(0.1, min(float(source.get("cpu_limit", 1.0)), 8.0))
    max_output = max(1_000, min(int(source.get("max_output_chars", 12_000)), 200_000))
    dependency_install = source.get("dependency_install", {}) or {}
    if not isinstance(dependency_install, dict):
        raise ConfigError("code_execution.dependency_install must be an object")
    allowed_packages = dependency_install.get("allowed_packages", source.get("allowed_packages"))
    if allowed_packages is None:
        allowed_packages = [
            "pandas",
            "numpy",
            "openpyxl",
            "matplotlib",
            "seaborn",
            "scipy",
            "scikit-learn",
        ]
    if not isinstance(allowed_packages, list):
        raise ConfigError("code_execution.allowed_packages must be a list")
    opensandbox_source = source.get("opensandbox", {}) or {}
    if not isinstance(opensandbox_source, dict):
        raise ConfigError("code_execution.opensandbox must be an object")
    opensandbox_protocol = str(opensandbox_source.get("protocol", "http")).lower()
    if opensandbox_protocol not in {"http", "https"}:
        raise ConfigError("code_execution.opensandbox.protocol must be http or https")
    request_timeout = max(1, min(int(opensandbox_source.get("request_timeout_seconds", 60)), 600))
    ready_timeout = max(1, min(int(opensandbox_source.get("ready_timeout_seconds", 30)), 300))
    local_process_source = source.get("local_process", {}) or {}
    if not isinstance(local_process_source, dict):
        raise ConfigError("code_execution.local_process must be an object")
    approval_scope_source = local_process_source.get("approval_scope", {}) or {}
    if not isinstance(approval_scope_source, dict):
        raise ConfigError("code_execution.local_process.approval_scope must be an object")
    approval_values = {"always", "first_time", "never"}
    approval_scope = LocalProcessApprovalScope(
        command_execution=str(approval_scope_source.get("command_execution", "always")).lower(),
        dependency_install=str(approval_scope_source.get("dependency_install", "first_time")).lower(),
        workspace_write=str(approval_scope_source.get("workspace_write", "always")).lower(),
        network_access=str(approval_scope_source.get("network_access", "always")).lower(),
    )
    for key, value in {
        "command_execution": approval_scope.command_execution,
        "dependency_install": approval_scope.dependency_install,
        "workspace_write": approval_scope.workspace_write,
        "network_access": approval_scope.network_access,
    }.items():
        if value not in approval_values:
            raise ConfigError(f"code_execution.local_process.approval_scope.{key} must be always, first_time, or never")
    return CodeExecutionConfig(
        enabled=bool(source.get("enabled", False)),
        backend=backend,
        image=str(source.get("image", "python:3.11-slim")),
        timeout_seconds=timeout,
        memory_limit=str(source.get("memory_limit", "512m")),
        cpu_limit=cpu_limit,
        network_enabled=bool(source.get("network_enabled", False)),
        workspace_readonly=bool(source.get("workspace_readonly", True)),
        artifacts_dir=str(source.get("artifacts_dir", "runtime/artifacts")),
        max_output_chars=max_output,
        dependency_install_enabled=bool(dependency_install.get("enabled", source.get("dependency_install_enabled", False))),
        allowed_packages=[str(item) for item in allowed_packages],
        install_timeout_seconds=max(1, min(int(dependency_install.get("timeout_seconds", source.get("install_timeout_seconds", 120))), 600)),
        max_artifacts=max(1, min(int(source.get("max_artifacts", 20)), 200)),
        max_artifact_bytes=max(1024, min(int(source.get("max_artifact_bytes", 25 * 1024 * 1024)), 1024 * 1024 * 1024)),
        allow_user_script_execution=bool(source.get("allow_user_script_execution", False)),
        opensandbox=OpenSandboxConfig(
            domain=str(opensandbox_source.get("domain", "127.0.0.1:8080")),
            protocol=opensandbox_protocol,
            api_key=str(opensandbox_source.get("api_key", "")),
            request_timeout_seconds=request_timeout,
            use_server_proxy=bool(opensandbox_source.get("use_server_proxy", False)),
            ready_timeout_seconds=ready_timeout,
        ),
        local_process=LocalProcessConfig(
            python_executable=str(local_process_source.get("python_executable", "")),
            runs_dir=str(local_process_source.get("runs_dir", "runtime/local_runs")),
            deps_dir=str(local_process_source.get("deps_dir", "runtime/local_deps")),
            cleanup_runs=bool(local_process_source.get("cleanup_runs", True)),
            require_human_approval=bool(local_process_source.get("require_human_approval", True)),
            approval_scope=approval_scope,
        ),
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
    providers: dict[str, ProviderConfig] = {}
    for name, data in provider_data.items():
        if not isinstance(data or {}, dict):
            raise ConfigError(f"Provider '{name}' must be an object")
        providers[str(name)] = _provider_config(str(name), data or {}, source)
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
        system_prompt=str(raw.get("system_prompt", DEFAULT_SYSTEM_PROMPT)),
        max_history_messages=max_history,
        providers=providers,
        source=source,
        default_provider=default_provider,
        default_model=default_model,
        search=_search_config(raw.get("search")),
        code_execution=_code_execution_config(raw.get("code_execution")),
    )
