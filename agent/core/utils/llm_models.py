"""Unified access to API, Ollama, and the bundled local chat model.

This module is deliberately limited to model concerns.  Agent planning,
conversation history, tools, and fallback policy belong to higher layers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import re
from threading import RLock
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from .llm_config import AppConfig, ProviderConfig


class LLMError(RuntimeError):
    """Base error raised by the unified model layer."""


class ProviderNotFound(LLMError):
    """The requested provider is not configured or enabled."""


class ModelNotFound(LLMError):
    """The requested model is not available from its provider."""


class ModelInvocationError(LLMError):
    """A configured provider failed to generate a response."""


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """One explicit provider/model choice."""

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    """Provider-independent generation settings.

    ``None`` means that the selected provider's configured default is used.
    """

    temperature: float | None = None
    max_tokens: int | None = None
    thinking_enabled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelInfo:
    provider: str
    model: str
    display_name: str
    supports_thinking: bool = False


@dataclass(slots=True)
class ModelResponse:
    content: str
    message: AIMessage
    provider: str
    model: str
    reasoning: str | None = None
    raw_content: str = ""

    @property
    def backend(self) -> str:
        """Compatibility name used by the current API response."""
        return self.provider

    @property
    def failures(self) -> list[tuple[str, str]]:
        """No implicit fallback means there are no hidden provider failures."""
        return []


def _message_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _message_reasoning(message: AIMessage) -> str | None:
    """Extract provider-native reasoning/thinking text when present."""
    containers = (
        getattr(message, "additional_kwargs", {}) or {},
        getattr(message, "response_metadata", {}) or {},
    )
    for item in containers:
        for key in ("thinking", "reasoning", "reasoning_content", "thought"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(message.content, list):
        parts: list[str] = []
        for block in message.content:
            if isinstance(block, dict) and block.get("type") in {
                "thinking",
                "reasoning",
                "reasoning_content",
            }:
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "".join(parts).strip() or None
    return None


_THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)


def _split_reasoning(
    content: str,
    *,
    thinking_started: bool = False,
) -> tuple[str | None, str, bool]:
    """Normalize tagged reasoning emitted by local reasoning templates."""
    opening = _THINK_OPEN.search(content)
    if opening is None:
        if thinking_started:
            closing = _THINK_CLOSE.search(content)
            if closing is None:
                return content.strip() or None, "", True
            reasoning = content[:closing.start()].strip() or None
            return reasoning, content[closing.end():].strip(), False
        return None, content.strip(), False
    closing = _THINK_CLOSE.search(content, opening.end())
    if closing is None:
        return content[opening.end():].strip() or None, "", True
    reasoning = content[opening.end():closing.start()].strip() or None
    answer = f"{content[:opening.start()]}{content[closing.end():]}".strip()
    return reasoning, answer, False


class ModelProvider(ABC):
    """Interface implemented by each supported model access mode."""

    provider_id: str

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def _temperature(self, options: GenerationOptions) -> float:
        return self.config.temperature if options.temperature is None else options.temperature

    def _max_tokens(self, options: GenerationOptions) -> int:
        return self.config.max_tokens if options.max_tokens is None else options.max_tokens

    @abstractmethod
    def list_models(self) -> list[ModelInfo]:
        """Return models that can be selected from this provider."""

    @abstractmethod
    def invoke(
        self,
        messages: list[BaseMessage],
        model: str,
        options: GenerationOptions,
    ) -> AIMessage:
        """Invoke exactly the requested model without hidden fallback."""


class _LangChainProvider(ModelProvider):
    """Shared lazy client cache for LangChain-backed providers."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._clients: dict[str, BaseChatModel] = {}
        self._client_lock = RLock()

    @abstractmethod
    def create_client(self, model: str) -> BaseChatModel:
        pass

    def client(self, model: str) -> BaseChatModel:
        with self._client_lock:
            if model not in self._clients:
                self._clients[model] = self.create_client(model)
            return self._clients[model]

    def invoke(
        self,
        messages: list[BaseMessage],
        model: str,
        options: GenerationOptions,
    ) -> AIMessage:
        try:
            response = self.client(model).invoke(
                messages,
                temperature=self._temperature(options),
                max_tokens=self._max_tokens(options),
                **options.extra,
            )
            return _normalize_message(response)
        except LLMError:
            raise
        except Exception as exc:
            raise ModelInvocationError(f"{self.provider_id}/{model}: {exc}") from exc


class OpenAIProvider(_LangChainProvider):
    """OpenAI-compatible HTTP APIs, including user-supplied compatible APIs."""

    provider_id = "api"

    def configured_models(self) -> list[str]:
        configured = self.config.extra.get("models", [])
        if isinstance(configured, str):
            configured = [configured]
        models = [str(item) for item in configured if str(item).strip()]
        if self.config.model and self.config.model not in models:
            models.insert(0, self.config.model)
        return models

    def list_models(self) -> list[ModelInfo]:
        thinking_models = self.config.extra.get("thinking_models", [])
        return [
            ModelInfo(
                self.provider_id,
                name,
                name,
                supports_thinking=(thinking_models == "*" or name in thinking_models),
            )
            for name in self.configured_models()
        ]

    def create_client(self, model: str) -> BaseChatModel:
        if not self.config.base_url:
            raise ModelInvocationError("API Provider 缺少 base_url")
        if not model:
            raise ModelNotFound("API Provider 未指定模型")
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise ModelInvocationError("缺少 langchain-openai") from exc
        kwargs: dict[str, Any] = {
            "model": model,
            "base_url": self.config.base_url,
            "api_key": self.config.api_key or "not-needed",
            "timeout": self.config.timeout,
        }
        request_body = self.config.extra.get("request_body", {})
        if request_body:
            kwargs["extra_body"] = request_body
        return ChatOpenAI(**kwargs)


class OllamaProvider(_LangChainProvider):
    """Models installed in the configured local Ollama service."""

    provider_id = "ollama"

    @property
    def base_url(self) -> str:
        return self.config.base_url or "http://127.0.0.1:11434"

    def list_models(self) -> list[ModelInfo]:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=min(self.config.timeout, 10))
            response.raise_for_status()
            thinking_models = self.config.extra.get("thinking_models", [])
            return [
                ModelInfo(
                    self.provider_id,
                    item["name"],
                    item["name"],
                    supports_thinking=self._supports_thinking(item, thinking_models),
                )
                for item in response.json().get("models", [])
                if item.get("name")
            ]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ModelInvocationError(f"无法读取 Ollama 模型列表: {exc}") from exc

    def _supports_thinking(self, item: dict[str, Any], thinking_models: Any) -> bool:
        name = str(item.get("name", ""))
        if thinking_models == "*" or name in thinking_models:
            return True
        details = item.get("details", {}) or {}
        family = str(details.get("family", "")).lower()
        families = {str(value).lower() for value in details.get("families", []) or []}
        model_key = name.lower()
        return (
            family.startswith(("qwen3", "qwen35", "deepseek-r1"))
            or any(value.startswith(("qwen3", "qwen35", "deepseek-r1")) for value in families)
            or model_key.startswith(("qwen3", "qwen3.", "qwen3-", "deepseek-r1"))
        )

    def create_client(self, model: str) -> BaseChatModel:
        if not model:
            raise ModelNotFound("Ollama Provider 未指定模型")
        try:
            from langchain_ollama import ChatOllama
        except ImportError as exc:
            raise ModelInvocationError("缺少 langchain-ollama") from exc
        return ChatOllama(
            model=model,
            base_url=self.base_url,
            client_kwargs={"timeout": self.config.timeout},
        )

    def invoke(
        self,
        messages: list[BaseMessage],
        model: str,
        options: GenerationOptions,
    ) -> AIMessage:
        try:
            request_options = {
                "temperature": self._temperature(options),
                "num_predict": self._max_tokens(options),
            }
            request_kwargs = dict(options.extra)
            request_options.update(request_kwargs.pop("options", {}) or {})
            response = self.client(model).invoke(
                messages,
                options=request_options,
                reasoning=options.thinking_enabled,
                **request_kwargs,
            )
            return _normalize_message(response)
        except LLMError:
            raise
        except Exception as exc:
            raise ModelInvocationError(f"{self.provider_id}/{model}: {exc}") from exc


class BuiltinProvider(ModelProvider):
    """The single MiniCPM5 GGUF model bundled with the application."""

    provider_id = "builtin"
    model_id = "builtin"

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._client: BaseChatModel | None = None
        self._lock = RLock()

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(self.provider_id, self.model_id, "内置模型", supports_thinking=True)]

    def _get_client(self) -> BaseChatModel:
        if self._client is not None:
            return self._client
        model_path = Path(self.config.model)
        if not model_path.is_file():
            raise ModelNotFound(f"内置模型文件不存在: {model_path}")
        try:
            from langchain_community.chat_models import ChatLlamaCpp
        except ImportError as exc:
            raise ModelInvocationError("缺少 langchain-community 或 llama-cpp-python") from exc
        kwargs: dict[str, Any] = {
            "model_path": str(model_path),
            "n_ctx": int(self.config.extra.get("context_length", 8192)),
            "n_gpu_layers": int(self.config.extra.get("gpu_layers", 0)),
            "max_tokens": self.config.max_tokens,
            "verbose": bool(self.config.extra.get("verbose", False)),
        }
        threads = int(self.config.extra.get("threads", 0))
        if threads > 0:
            kwargs["n_threads"] = threads
        self._client = ChatLlamaCpp(**kwargs)
        return self._client

    def invoke(
        self,
        messages: list[BaseMessage],
        model: str,
        options: GenerationOptions,
    ) -> AIMessage:
        if model != self.model_id:
            raise ModelNotFound(f"内置 Provider 仅支持模型: {self.model_id}")
        with self._lock:
            try:
                client = self._get_client()
                llama = getattr(client, "client", None)
                base_handler = getattr(llama, "chat_handler", None)
                if base_handler is None and llama is not None:
                    from llama_cpp import llama_chat_format

                    base_handler = (
                        llama._chat_handlers.get(llama.chat_format)
                        or llama_chat_format.get_chat_completion_handler(llama.chat_format)
                    )

                if base_handler is not None:
                    def mode_handler(*args: Any, **kwargs: Any) -> Any:
                        kwargs["enable_thinking"] = options.thinking_enabled
                        return base_handler(*args, **kwargs)

                    llama.chat_handler = mode_handler
                try:
                    response = client.invoke(
                        messages,
                        temperature=(
                            self._temperature(options)
                            if options.temperature is not None
                            else (0.9 if options.thinking_enabled else self.config.temperature)
                        ),
                        max_tokens=self._max_tokens(options),
                        top_p=0.95,
                        **options.extra,
                    )
                    return _normalize_message(response)
                finally:
                    if base_handler is not None:
                        llama.chat_handler = base_handler
            except LLMError:
                raise
            except Exception as exc:
                raise ModelInvocationError(f"builtin/{model}: {exc}") from exc


PROVIDER_TYPES: dict[str, type[ModelProvider]] = {
    "api": OpenAIProvider,
    "ollama": OllamaProvider,
    "builtin": BuiltinProvider,
}


class ModelManager:
    """Public model interface used by agents, API endpoints, and future tools."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.providers: dict[str, ModelProvider] = {
            name: PROVIDER_TYPES[name](provider_config)
            for name, provider_config in config.providers.items()
            if provider_config.enabled
        }
        self.default_selection = ModelSelection(
            config.default_provider,
            config.default_model,
        )

    def list_models(self, provider: str | None = None) -> list[ModelInfo]:
        if provider is not None:
            return self._provider(provider).list_models()
        models: list[ModelInfo] = []
        for item in self.providers.values():
            try:
                models.extend(item.list_models())
            except LLMError:
                continue
        return models

    def chat(
        self,
        messages: list[BaseMessage],
        selection: ModelSelection | None = None,
        options: GenerationOptions | None = None,
    ) -> ModelResponse:
        selected = selection or self.default_selection
        generation = options or GenerationOptions()
        provider = self._provider(selected.provider)
        raw_message = provider.invoke(_sanitize_messages_for_model(messages), selected.model, generation)
        raw_content = _message_text(raw_message)
        metadata_reasoning = _message_reasoning(raw_message)
        tagged_reasoning, content, incomplete = _split_reasoning(
            raw_content,
            thinking_started=(
                generation.thinking_enabled and selected.provider == BuiltinProvider.provider_id
            ),
        )
        reasoning = metadata_reasoning or tagged_reasoning
        if incomplete:
            raise ModelInvocationError("模型的思考过程未完成，请重试或增加 max_tokens")
        if not content and reasoning:
            raise ModelInvocationError(
                "模型只返回了思考过程，没有返回最终回答；请关闭深度思考或增加 max_tokens"
            )
        if not content:
            raise ModelInvocationError("模型没有返回最终回答")
        clean_message = AIMessage(content=content)
        return ModelResponse(
            content=content,
            message=clean_message,
            provider=selected.provider,
            model=selected.model,
            reasoning=reasoning if generation.thinking_enabled else None,
            raw_content=raw_content,
        )

    def _provider(self, provider: str) -> ModelProvider:
        item = self.providers.get(provider)
        if item is None:
            available = ", ".join(self.providers) or "无"
            raise ProviderNotFound(f"Provider '{provider}' 未启用；当前可用: {available}")
        return item


def _normalize_message(response: Any) -> AIMessage:
    if isinstance(response, AIMessage):
        message = response
    else:
        message = AIMessage(content=str(getattr(response, "content", response)))
    if not _message_text(message).strip() and not _message_reasoning(message):
        raise ModelInvocationError("模型返回了空内容")
    return message


_MODEL_SAFE_ADDITIONAL_KEYS = {
    "function_call",
    "refusal",
}


def _sanitize_messages_for_model(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Strip Agent Dogs metadata before replaying history to model providers."""
    sanitized: list[BaseMessage] = []
    for message in messages:
        content = message.content
        if message.type == "human":
            sanitized.append(HumanMessage(content=content))
        elif message.type == "system":
            sanitized.append(SystemMessage(content=content))
        elif message.type == "ai":
            kwargs = {
                key: value
                for key, value in (getattr(message, "additional_kwargs", {}) or {}).items()
                if key in _MODEL_SAFE_ADDITIONAL_KEYS and value
            }
            sanitized.append(AIMessage(content=content, additional_kwargs=kwargs))
        elif hasattr(message, "model_copy"):
            sanitized.append(
                message.model_copy(
                    update={
                        "additional_kwargs": {},
                        "response_metadata": {},
                    }
                )
            )
        else:
            sanitized.append(message)
    return sanitized


# Short, stable name for callers that prefer a service-oriented vocabulary.
LLMService = ModelManager
