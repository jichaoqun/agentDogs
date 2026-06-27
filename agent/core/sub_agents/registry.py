"""Registry contracts for specialist sub-agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SubAgentSpec:
    name: str
    description: str
    handles: list[str] = field(default_factory=list)
    does_not_handle: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    input_contract: dict[str, Any] = field(default_factory=dict)
    output_contract: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    examples: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SubAgentResult:
    ok: bool
    content: str
    data: Any | None = None
    error: str | None = None
    status: str = "completed"
    summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    confidence: float | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def success(
        cls,
        content: str,
        *,
        data: Any | None = None,
        status: str = "completed",
        summary: str = "",
        findings: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_actions: list[str] | None = None,
        confidence: float | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> "SubAgentResult":
        return cls(
            True,
            content,
            data=data,
            status=status,
            summary=summary or content,
            findings=findings or [],
            evidence=evidence or [],
            next_actions=next_actions or [],
            confidence=confidence,
            tool_calls=tool_calls or [],
        )

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        data: Any | None = None,
        summary: str = "",
        findings: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_actions: list[str] | None = None,
        confidence: float | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> "SubAgentResult":
        return cls(
            False,
            error,
            data=data,
            error=error,
            status="failed",
            summary=summary or error,
            findings=findings or [],
            evidence=evidence or [],
            next_actions=next_actions or [],
            confidence=confidence,
            tool_calls=tool_calls or [],
        )


@dataclass(slots=True)
class RegisteredSubAgent:
    spec: SubAgentSpec
    agent: Any


class SubAgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, RegisteredSubAgent] = {}

    def register(self, spec: SubAgentSpec, agent: Any) -> None:
        if spec.name in self._agents:
            raise ValueError(f"子 Agent 已注册：{spec.name}")
        self._agents[spec.name] = RegisteredSubAgent(spec, agent)

    def get(self, name: str) -> RegisteredSubAgent:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise KeyError(f"未知子 Agent：{name}") from exc

    def list_specs(self) -> list[SubAgentSpec]:
        return [item.spec for item in sorted(self._agents.values(), key=lambda item: item.spec.name)]
