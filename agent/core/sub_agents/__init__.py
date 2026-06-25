"""Sub-agent implementations used by the main agent graph."""

from __future__ import annotations

from ..tools import ToolRegistry, create_default_tool_registry
from .file_agent import FileAgent
from .registry import SubAgentRegistry, SubAgentResult, SubAgentSpec
from .simple_chat_agent import SimpleChatAgent
from .simple_task_agent import SimpleTaskAgent
from .task_agent import TaskAgent, TaskExecutionResult, TaskStepRecord


def create_default_sub_agent_registry(
    tools: ToolRegistry | None = None,
    simple_chat_agent: SimpleChatAgent | None = None,
) -> SubAgentRegistry:
    tool_registry = tools or create_default_tool_registry()
    file_agent = FileAgent(tool_registry)
    simple_task_agent = SimpleTaskAgent(tool_registry)
    task_agent = TaskAgent(file_agent)
    registry = SubAgentRegistry()
    registry.register(
        SubAgentSpec(
            name="simple_chat",
            description="普通问答、解释、闲聊和简单文本生成。",
            capabilities=["chat", "explain", "rewrite", "translate"],
            tools=[],
            risk_level="low",
        ),
        simple_chat_agent,
    )
    registry.register(
        SubAgentSpec(
            name="simple_task",
            description="明确、低风险、一步可完成的工具任务执行器。",
            capabilities=["tool.route", "file.list", "file.read", "file.search", "file.info"],
            tools=["list_workspace_tree", "read_file", "search_files", "file_info"],
            risk_level="low",
        ),
        simple_task_agent,
    )
    registry.register(
        SubAgentSpec(
            name="file_agent",
            description="workspace 文件读取、搜索和只读分析。",
            capabilities=["file.read", "file.search", "file.analysis"],
            tools=["list_workspace_tree", "read_file", "search_files", "file_info"],
            risk_level="low",
        ),
        file_agent,
    )
    registry.register(
        SubAgentSpec(
            name="task_agent",
            description="复杂任务计划步骤调度，第一版只委派 FileAgent。",
            capabilities=["task.execute", "task.summarize", "step.track"],
            tools=["file_agent"],
            risk_level="medium",
        ),
        task_agent,
    )
    return registry


__all__ = [
    "FileAgent",
    "SimpleChatAgent",
    "SimpleTaskAgent",
    "SubAgentRegistry",
    "SubAgentResult",
    "SubAgentSpec",
    "TaskAgent",
    "TaskExecutionResult",
    "TaskStepRecord",
    "create_default_sub_agent_registry",
]
