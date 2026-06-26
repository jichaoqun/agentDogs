"""State and schema objects for the first-stage agent graph."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

from .utils.llm_models import GenerationOptions, ModelResponse, ModelSelection


Complexity = Literal["simple", "needs_info", "complex"]
Route = Literal["simple_chat", "simple_task", "clarify", "future_task"]
TaskKind = Literal["chat", "tool", "task", "unknown"]
TaskRiskLevel = Literal["low", "medium", "high"]
AgentStatus = Literal["completed", "interrupted"]
InterruptType = Literal["clarification", "plan_confirmation"]
PlanDecision = Literal["approve", "revise", "cancel"]
PlanStatus = Literal["pending", "approved", "revised", "cancelled"]


class TaskAnalysis(BaseModel):
    """Structured task judgment produced by the main agent."""

    intent: str = ""
    complexity: Complexity = "simple"
    task_kind: TaskKind = "chat"
    route_hint: Route | None = None
    tool_intents: list[str] = Field(default_factory=list)
    estimated_steps: int = Field(default=1, ge=1)
    risk_level: TaskRiskLevel = "low"
    requires_confirmation: bool = False
    confidence: float = Field(default=0.6, ge=0, le=1)
    reason: str = ""
    missing_info: list[str] = Field(default_factory=list)
    suggested_steps: list[str] = Field(default_factory=list)
    clarification_questions: list["ClarificationQuestion"] = Field(default_factory=list)


class ClarificationQuestion(BaseModel):
    """One structured question used by the frontend clarify dialog."""

    id: str = ""
    question: str
    options: list[str] = Field(default_factory=list)
    allow_custom: bool = True
    required: bool = True


class TaskPlan(BaseModel):
    """A user-confirmable plan for complex tasks."""

    summary: str = ""
    steps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    requires_confirmation: bool = True


class AgentState(TypedDict, total=False):
    """Runtime state passed between LangGraph nodes."""

    messages: list[BaseMessage]
    user_input: str
    current_time: str
    current_time_context: str
    task_analysis: TaskAnalysis
    route: Route
    status: AgentStatus
    missing_info: list[str]
    clarification_questions: list[ClarificationQuestion]
    clarification_answers: dict[str, str]
    task_plan: TaskPlan
    plan_summary: str
    plan_steps: list[str]
    plan_risks: list[str]
    plan_decision: PlanDecision
    plan_feedback: str
    plan_status: PlanStatus
    task_status: str
    task_steps: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    final_response: str
    interrupt_type: InterruptType
    interrupt_id: str
    interrupt: dict[str, Any]
    history_recorded: bool
    errors: list[str]
    selection: ModelSelection | None
    options: GenerationOptions | None
    model_response: ModelResponse
