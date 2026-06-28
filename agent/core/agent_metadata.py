"""Message metadata isolation helpers for Agent Dogs messages."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from .state import AgentState
from .utils.llm_models import ModelResponse
from .utils.time_utils import isoformat


AGENT_METADATA_KEY = "agent_dogs"
APP_METADATA_KEYS = {
    "created_at",
    "status",
    "interrupt",
    "plan_status",
    "task",
    "steps",
    "tool_calls",
    "debug_trace",
    "agent_flow",
    "task_brief",
    "route",
    "complexity",
    "clarification",
    "plan_steps",
    "plan",
}


def _agent_response_metadata(message: AIMessage | HumanMessage) -> dict[str, Any]:
    response_metadata = getattr(message, "response_metadata", {}) or {}
    metadata = response_metadata.get(AGENT_METADATA_KEY)
    if isinstance(metadata, dict):
        return metadata
    legacy = getattr(message, "additional_kwargs", {}) or {}
    return legacy if isinstance(legacy, dict) else {}


def _safe_additional_kwargs(message: AIMessage) -> dict[str, Any]:
    return {
        key: value
        for key, value in (getattr(message, "additional_kwargs", {}) or {}).items()
        if key not in APP_METADATA_KEYS and not (key == "tool_calls" and not value)
    }


def _with_agent_metadata(message: AIMessage | HumanMessage, metadata: dict[str, Any]) -> AIMessage | HumanMessage:
    response_metadata = dict(getattr(message, "response_metadata", {}) or {})
    response_metadata[AGENT_METADATA_KEY] = metadata
    if isinstance(message, AIMessage):
        return AIMessage(
            content=message.content,
            additional_kwargs=_safe_additional_kwargs(message),
            response_metadata=response_metadata,
        )
    return HumanMessage(content=message.content, response_metadata=response_metadata)




class AgentMetadataMixin:
    """Attach app metadata without polluting model protocol fields."""

    def _message_metadata(self, state: AgentState) -> dict[str, Any]:
        analysis = state.get("task_analysis")
        route = state.get("route")
        metadata: dict[str, Any] = {
            "created_at": isoformat(),
            "status": state.get("status", "completed"),
            "interrupt": state.get("interrupt"),
            "plan_status": state.get("plan_status"),
            "task": {"status": state.get("task_status")} if state.get("task_status") else None,
            "steps": state.get("task_steps"),
            "tool_calls": state.get("tool_calls"),
            "debug_trace": state.get("debug_trace"),
            "task_brief": state.get("task_brief").model_dump() if hasattr(state.get("task_brief"), "model_dump") else state.get("task_brief"),
        }
        metadata["agent_flow"] = self._build_agent_flow(state)
        if route:
            metadata["route"] = route
        if analysis:
            metadata["complexity"] = analysis.complexity
        if route == "clarify":
            metadata["clarification"] = {
                "original_message": state["user_input"],
                "questions": [
                    question.model_dump()
                    for question in state.get("clarification_questions", [])
                ],
            }
        if route == "future_task":
            metadata["plan_steps"] = state.get("plan_steps", [])
            plan = self._plan_from_state(state) if state.get("plan_steps") else None
            if plan:
                metadata["plan"] = plan.model_dump()
        return metadata

    def _with_message_metadata(self, result: ModelResponse, state: AgentState) -> ModelResponse:
        metadata = self._message_metadata(state)
        message = _with_agent_metadata(result.message, metadata)
        return ModelResponse(
            content=result.content,
            message=message,
            provider=result.provider,
            model=result.model,
            reasoning=result.reasoning,
            raw_content=result.raw_content,
        )

    def _record_history(self, user_record: str, assistant_message: AIMessage) -> None:
        user_message = _with_agent_metadata(HumanMessage(content=user_record), {"created_at": isoformat()})
        self.history.add_message(user_message)
        self.history.add_message(assistant_message)
        self._trim_history()

