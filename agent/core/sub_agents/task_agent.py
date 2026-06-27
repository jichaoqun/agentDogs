"""First-stage task execution coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .file_agent import FileAgent
from .registry import SubAgentSpec
from .search_agent import SearchAgent


SEARCH_STEP_MARKERS = ("搜索", "查找", "检索", "查询", "调研", "联网", "网上", "网络")


StepStatus = Literal["pending", "running", "completed", "failed", "waiting_confirmation"]


@dataclass(slots=True)
class TaskStepRecord:
    index: int
    title: str
    status: StepStatus = "pending"
    result: str = ""
    error: str | None = None
    assigned_agent: str = "FileAgent"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "assigned_agent": self.assigned_agent,
            "tool_calls": self.tool_calls,
        }


@dataclass(slots=True)
class TaskExecutionResult:
    status: str
    summary: str
    steps: list[TaskStepRecord]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "steps": [item.as_dict() for item in self.steps],
            "tool_calls": self.tool_calls,
        }


@dataclass(slots=True)
class TaskAgent:
    """Minimal coordinator that delegates first-phase work to specialist agents."""

    CAPABILITY = SubAgentSpec(
        name="task_agent",
        description="复杂任务计划步骤调度器，负责把多步骤任务委派给领域子 Agent 并汇总步骤状态。",
        handles=["多步骤任务执行", "计划确认后的任务调度", "跨文件和搜索的阶段性执行"],
        does_not_handle=["纯聊天", "未经确认的高风险写操作", "直接底层工具调用"],
        capabilities=["task.execute", "task.summarize", "step.track", "search.dispatch", "file.dispatch"],
        tools=["file_agent", "search_agent"],
        input_contract={"type": "approved_plan", "fields": ["user_input", "plan_steps"]},
        output_contract={"type": "TaskExecutionResult", "fields": ["summary", "steps", "tool_calls"]},
        risk_level="medium",
        examples=["计划确认后分析整个项目", "按步骤搜索并总结资料"],
    )

    file_agent: FileAgent
    search_agent: SearchAgent | None = None

    @classmethod
    def capability_spec(cls) -> SubAgentSpec:
        return cls.CAPABILITY

    def execute(self, *, user_input: str, plan_steps: list[str]) -> TaskExecutionResult:
        records: list[TaskStepRecord] = []
        all_tool_calls: list[dict[str, Any]] = []
        overall_status = "completed"

        for index, title in enumerate(plan_steps, start=1):
            record = TaskStepRecord(index=index, title=title, status="running")
            if self.search_agent is not None and self._looks_like_search_step(title, user_input):
                record.assigned_agent = "SearchAgent"
                result = self.search_agent.handle_step(title, user_input=user_input)
            else:
                record.assigned_agent = "FileAgent"
                result = self.file_agent.handle_step(title, user_input=user_input)
            record.tool_calls = result.tool_calls
            all_tool_calls.extend(result.tool_calls)
            if result.ok:
                record.status = result.status if result.status in {"waiting_confirmation", "completed"} else "completed"
                record.result = result.content
                if record.status == "waiting_confirmation":
                    overall_status = "waiting_confirmation"
            else:
                record.status = "failed"
                record.error = result.error or result.content
                record.result = result.content
                if overall_status != "waiting_confirmation":
                    overall_status = "failed"
            records.append(record)

        summary = self._summary(overall_status, records)
        return TaskExecutionResult(
            status=overall_status,
            summary=summary,
            steps=records,
            tool_calls=all_tool_calls,
        )

    def _summary(self, status: str, records: list[TaskStepRecord]) -> str:
        completed = sum(1 for item in records if item.status == "completed")
        waiting = sum(1 for item in records if item.status == "waiting_confirmation")
        failed = sum(1 for item in records if item.status == "failed")
        if status == "waiting_confirmation":
            return f"TaskAgent 已完成 {completed} 个步骤，{waiting} 个步骤等待人工确认。"
        if status == "failed":
            return f"TaskAgent 已完成 {completed} 个步骤，{failed} 个步骤失败。"
        return f"TaskAgent 已完成 {completed} 个步骤。"

    def _looks_like_search_step(self, title: str, user_input: str) -> bool:
        text = f"{title}\n{user_input}"
        return any(marker in text for marker in SEARCH_STEP_MARKERS)
