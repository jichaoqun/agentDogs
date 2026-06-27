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
from .registry import SubAgentSpec


@dataclass(slots=True)
class SimpleChatAgent:
    """Plain conversation agent: no tools, no file writes, no task execution."""

    CAPABILITY = SubAgentSpec(
        name="simple_chat",
        description="普通问答、解释、闲聊和简单文本生成；不调用工具。",
        handles=["闲聊", "解释概念", "改写文本", "翻译", "不需要实时信息的普通问答"],
        does_not_handle=["实时信息查询", "联网搜索", "workspace 文件操作", "多步骤任务执行"],
        capabilities=["chat", "explain", "rewrite", "translate"],
        tools=[],
        input_contract={"type": "plain_text", "requires_task_brief": False},
        output_contract={"type": "model_response", "summary": "直接面向用户的自然语言回答"},
        risk_level="low",
        examples=["你好", "解释一下什么是 LangGraph", "帮我改写这段话"],
    )

    config: AppConfig
    models: ModelManager
    history: InMemoryChatMessageHistory

    @classmethod
    def capability_spec(cls) -> SubAgentSpec:
        return cls.CAPABILITY

    def chat(
        self,
        user_input: str,
        *,
        selection: ModelSelection | None = None,
        options: GenerationOptions | None = None,
        current_time: str = "",
    ) -> ModelResponse:
        system_prompt = self.config.system_prompt
        if current_time:
            system_prompt = f"{system_prompt}\n\n{current_time}"
        messages = [
            SystemMessage(content=system_prompt),
            *self.history.messages,
            HumanMessage(content=user_input),
        ]
        return self.models.chat(messages, selection=selection, options=options)
