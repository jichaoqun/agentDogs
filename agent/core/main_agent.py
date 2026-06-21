"""Minimal LangChain conversational agent used by CLI and future API layers."""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import HumanMessage, SystemMessage

from .utils.llm_config import AppConfig
from .utils.llm_models import (
    GenerationOptions,
    ModelManager,
    ModelResponse,
    ModelSelection,
)


@dataclass(slots=True)
class MainAgent:
    config: AppConfig
    models: ModelManager | None = None
    history: InMemoryChatMessageHistory = field(default_factory=InMemoryChatMessageHistory)

    def __post_init__(self) -> None:
        if self.models is None:
            self.models = ModelManager(self.config)

    def chat(
        self,
        user_input: str,
        *,
        selection: ModelSelection | None = None,
        options: GenerationOptions | None = None,
        thinking_enabled: bool | None = None,
    ) -> ModelResponse:
        text = user_input.strip()
        if not text:
            raise ValueError("消息不能为空")
        messages = [
            SystemMessage(content=self.config.system_prompt),
            *self.history.messages,
            HumanMessage(content=text),
        ]
        if thinking_enabled is not None and options is None:
            options = GenerationOptions(thinking_enabled=thinking_enabled)
        result = self.models.chat(messages, selection=selection, options=options)
        self.history.add_user_message(text)
        self.history.add_message(result.message)
        if len(self.history.messages) > self.config.max_history_messages:
            self.history.messages = self.history.messages[-self.config.max_history_messages:]
        return result

    def clear(self) -> None:
        self.history.clear()
