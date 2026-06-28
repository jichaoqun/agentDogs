"""Sub-agent implementations used by the main agent graph."""

from __future__ import annotations

from ..sandbox import DockerSandboxRunner
from ..tools import ToolRegistry, create_default_tool_registry
from ..utils.llm_config import CodeExecutionConfig
from .code_agent import CodeAgent
from .file_agent import FileAgent
from .registry import SubAgentRegistry, SubAgentResult, SubAgentSpec
from .search_agent import SearchAgent
from .simple_chat_agent import SimpleChatAgent
from .simple_task_agent import SimpleTaskAgent
from .task_agent import TaskAgent, TaskExecutionResult, TaskStepRecord


def create_default_sub_agent_registry(
    tools: ToolRegistry | None = None,
    simple_chat_agent: SimpleChatAgent | None = None,
    code_execution: CodeExecutionConfig | None = None,
) -> SubAgentRegistry:
    tool_registry = tools or create_default_tool_registry()
    file_agent = FileAgent(tool_registry)
    search_agent = SearchAgent(tool_registry)
    sandbox_runner = DockerSandboxRunner(code_execution or CodeExecutionConfig())
    code_agent = CodeAgent(tool_registry, sandbox_runner)
    simple_task_agent = SimpleTaskAgent(tool_registry, search_agent)
    task_agent = TaskAgent(file_agent, search_agent, code_agent)
    registry = SubAgentRegistry()
    registry.register(
        simple_chat_agent.capability_spec() if simple_chat_agent is not None else SimpleChatAgent.capability_spec(),
        simple_chat_agent,
    )
    for agent in (simple_task_agent, search_agent, file_agent, code_agent, task_agent):
        registry.register(agent.capability_spec(), agent)
    return registry


__all__ = [
    "CodeAgent",
    "FileAgent",
    "SearchAgent",
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
