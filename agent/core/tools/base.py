"""Common contracts for internal tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal


RiskLevel = Literal["low", "medium", "high"]
ToolCallable = Callable[[dict[str, Any]], "ToolResult"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk_level: RiskLevel = "low"
    capabilities: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolResult:
    ok: bool
    content: str = ""
    data: Any | None = None
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)

    @classmethod
    def success(
        cls,
        content: str = "",
        *,
        data: Any | None = None,
        artifacts: list[str] | None = None,
    ) -> "ToolResult":
        return cls(True, content=content, data=data, artifacts=artifacts or [])

    @classmethod
    def failure(cls, error: str, *, data: Any | None = None) -> "ToolResult":
        return cls(False, error=error, data=data)


@dataclass(slots=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolCallable


class ToolRegistry:
    """Name-based registry for internal Python tools."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolCallable) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具已注册：{spec.name}")
        self._tools[spec.name] = RegisteredTool(spec, handler)

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未知工具：{name}") from exc

    def list_specs(self) -> list[ToolSpec]:
        return [item.spec for item in sorted(self._tools.values(), key=lambda item: item.spec.name)]

    def call(self, name: str, payload: dict[str, Any] | None = None) -> ToolResult:
        tool = self.get(name)
        return tool.handler(payload or {})
