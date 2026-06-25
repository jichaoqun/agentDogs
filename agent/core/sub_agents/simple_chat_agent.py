"""Simple chat sub-agent used for low-risk conversational turns."""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import HumanMessage, SystemMessage

from ..utils.llm_config import AppConfig
from ..utils.llm_models import (
    GenerationOptions,
    ModelManager,
    ModelResponse,
    ModelSelection,
)


@dataclass(slots=True)
class SimpleChatAgent:
    """Plain conversation agent: no tools, no file writes, no task execution."""

    config: AppConfig
    models: ModelManager
    history: InMemoryChatMessageHistory

    def chat(
        self,
        user_input: str,
        *,
        selection: ModelSelection | None = None,
        options: GenerationOptions | None = None,
    ) -> ModelResponse:
        messages = [
            SystemMessage(content=self.config.system_prompt),
            *self.history.messages,
            HumanMessage(content=user_input),
        ]
        result = self.models.chat(messages, selection=selection, options=options)
        self.history.add_user_message(user_input)
        self.history.add_message(result.message)
        if len(self.history.messages) > self.config.max_history_messages:
            self.history.messages = self.history.messages[-self.config.max_history_messages:]
        return result
