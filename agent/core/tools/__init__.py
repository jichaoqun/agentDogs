"""Internal tool registry exports."""

from __future__ import annotations

from pathlib import Path

from .base import RegisteredTool, RiskLevel, ToolRegistry, ToolResult, ToolSpec
from .file_tools import DEFAULT_WORKSPACE_ROOT, WorkspaceFileTools, create_file_tool_registry
from .search_tools import SearchTools, register_search_tools
from ..utils.llm_config import SearchConfig


def create_default_tool_registry(
    root: Path = DEFAULT_WORKSPACE_ROOT,
    search_config: SearchConfig | None = None,
) -> ToolRegistry:
    registry = create_file_tool_registry(root)
    return register_search_tools(registry, search_config)


__all__ = [
    "DEFAULT_WORKSPACE_ROOT",
    "RegisteredTool",
    "RiskLevel",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "SearchTools",
    "WorkspaceFileTools",
    "create_default_tool_registry",
    "create_file_tool_registry",
    "register_search_tools",
]
