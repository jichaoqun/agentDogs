from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ClarificationQuestionOut(BaseModel):
    id: str
    question: str
    options: list[str] = Field(default_factory=list)
    allow_custom: bool = True
    required: bool = True


class ClarificationOut(BaseModel):
    original_message: str
    questions: list[ClarificationQuestionOut] = Field(default_factory=list)


class TaskPlanOut(BaseModel):
    summary: str = ""
    steps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    requires_confirmation: bool = True


class AgentInterruptOut(BaseModel):
    id: str
    type: Literal["clarification", "plan_confirmation"]
    message: str
    clarification: ClarificationOut | None = None
    plan: TaskPlanOut | None = None


class MessageOut(BaseModel):
    role: str
    content: str
    created_at: datetime | None = None
    route: str | None = None
    complexity: str | None = None
    clarification: ClarificationOut | None = None
    plan_steps: list[str] | None = None
    status: str | None = None
    interrupt: AgentInterruptOut | None = None
    plan_status: str | None = None
    task: dict | None = None
    steps: list[dict] | None = None
    tool_calls: list[dict] | None = None
    debug_trace: list[dict] | None = None
    agent_flow: dict | None = None
    task_brief: dict | None = None


class SessionOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut] = Field(default_factory=list)


class SessionCreate(BaseModel):
    title: str = Field(default="新会话", max_length=80)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    provider: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=131_072)
    thinking_enabled: bool = False


class ResumeRequest(BaseModel):
    interrupt_id: str = Field(min_length=1)
    type: Literal["clarification", "plan_confirmation"]
    answers: dict[str, str] = Field(default_factory=dict)
    decision: Literal["approve", "revise", "cancel"] | None = None
    feedback: str | None = None


class FailureOut(BaseModel):
    backend: str
    reason: str


class ChatResponse(BaseModel):
    session_id: str
    message: MessageOut
    backend: str
    provider: str
    model: str
    failures: list[FailureOut]
    reasoning: str | None = None
    thinking_enabled: bool = False
    cancelled: bool = False
    route: str | None = None
    complexity: str | None = None
    clarification: ClarificationOut | None = None
    plan_steps: list[str] | None = None
    status: str = "completed"
    interrupt: AgentInterruptOut | None = None
    plan_status: str | None = None
    task: dict | None = None
    steps: list[dict] | None = None
    tool_calls: list[dict] | None = None
    debug_trace: list[dict] | None = None
    agent_flow: dict | None = None
    task_brief: dict | None = None


class CancelRunResponse(BaseModel):
    session_id: str
    cancelled: bool
    status: str


class ToolInfoOut(BaseModel):
    name: str
    description: str
    input_schema: dict
    risk_level: str
    capabilities: list[str] = Field(default_factory=list)


class SubAgentInfoOut(BaseModel):
    name: str
    description: str
    handles: list[str] = Field(default_factory=list)
    does_not_handle: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    input_contract: dict = Field(default_factory=dict)
    output_contract: dict = Field(default_factory=dict)
    risk_level: str
    examples: list[str] = Field(default_factory=list)


class BackendStatus(BaseModel):
    name: str
    enabled: bool
    model: str


class StatusResponse(BaseModel):
    status: str
    default_provider: str
    default_model: str
    backends: list[BackendStatus]


class ModelInfoOut(BaseModel):
    provider: str
    model: str
    display_name: str
    supports_thinking: bool


class FileNodeOut(BaseModel):
    path: str
    name: str
    type: str
    size: int
    modified_at: datetime
    mime_type: str
    editable: bool
    children: list["FileNodeOut"] = Field(default_factory=list)


class FileContentOut(BaseModel):
    path: str
    name: str
    content: str
    editable: bool
    mime_type: str


class FileCreate(BaseModel):
    path: str = ""
    name: str = Field(min_length=1, max_length=255)
    type: str = Field(pattern="^(file|directory)$")


class FileUpdate(BaseModel):
    path: str
    name: str = Field(min_length=1, max_length=255)


class FileSave(BaseModel):
    path: str
    content: str
