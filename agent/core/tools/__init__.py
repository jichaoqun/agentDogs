"""Internal tool registry exports."""

from __future__ import annotations

from pathlib import Path

from .base import RegisteredTool, RiskLevel, ToolRegistry, ToolResult, ToolSpec
from .file_tools import DEFAULT_WORKSPACE_ROOT, WorkspaceFileTools, create_file_tool_registry


def create_default_tool_registry(root: Path = DEFAULT_WORKSPACE_ROOT) -> ToolRegistry:
    return create_file_tool_registry(root)


__all__ = [
    "DEFAULT_WORKSPACE_ROOT",
    "RegisteredTool",
    "RiskLevel",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "WorkspaceFileTools",
    "create_default_tool_registry",
    "create_file_tool_registry",
]
