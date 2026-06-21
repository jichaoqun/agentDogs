from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MessageOut(BaseModel):
    role: str
    content: str


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
