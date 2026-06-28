"""First-stage task execution coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Literal

from ..state import TaskBrief
from .code_agent import CodeAgent
from .file_agent import FileAgent
from .registry import SubAgentResult, SubAgentSpec
from .search_agent import SearchAgent


StepStatus = Literal["pending", "running", "completed", "failed", "waiting_confirmation"]
StepType = Literal[
    "file_info",
    "file_read",
    "search",
    "data_analysis",
    "chart_generation",
    "code_analysis",
    "project_analysis",
    "script_execution",
    "workspace_write",
    "manual_review",
]

PATH_HINT = re.compile(
    r"`([^`]+)`|([\w\u4e00-\u9fff .\\/-]+\.(?:md|txt|py|js|jsx|ts|tsx|json|csv|xlsx|xls|ya?ml|html|css|xml|log|docx))",
    re.IGNORECASE,
)
DIRECTORY_HINT = re.compile(r"([\w\u4e00-\u9fff ._-]+?)(?:文件夹|目录)")
DIRECTORY_NAME_HINT = re.compile(r"名为\s*([\w\u4e00-\u9fff ._-]+?)\s*的?(?:文件夹|目录)")
DIRECTORY_TARGET_HINT = re.compile(
    r"(?:保存到|存放到|移动到|复制到|拷贝到|发布到|输出到|放到|到|至)\s*([\w\u4e00-\u9fff ._-]+?)\s*(?:文件夹|目录)"
)
DIRECTORY_TOKEN_HINT = re.compile(r"[A-Za-z0-9_.-]+")

SEARCH_STEP_MARKERS = ("搜索", "查找", "检索", "查询", "调研", "联网", "网上", "网络")
READ_STEP_MARKERS = ("打开", "读取", "查看", "预览", "内容")
DATA_STEP_MARKERS = ("数据", "表格", "统计", "分析", "探索", "缺失值", "摘要", "xlsx", "xls", "csv", "Excel", "excel")
CHART_STEP_MARKERS = ("生成图", "图表", "结果图", "画图", "趋势图", "柱状图", "折线图", "散点图", "图片", "PDF", "pdf", "chart", "plot")
CODE_STEP_MARKERS = ("Python", "python", "代码", "脚本", "函数", "类", "项目结构", "分析代码")
SCRIPT_STEP_MARKERS = ("运行这段", "执行这段", "运行以下", "执行以下", "运行下面", "执行下面", "用脚本处理", "运行代码")
PROJECT_STEP_MARKERS = ("整个项目", "项目结构", "代码库", "整个 workspace", "整个workspace")
FILE_CHECK_MARKERS = ("检查", "确认", "是否存在", "可读", "可读性", "存在")
WORKSPACE_WRITE_MARKERS = (
    "新建",
    "创建",
    "保存",
    "保存到",
    "存放",
    "写入",
    "修改",
    "改写",
    "删除",
    "重命名",
    "覆盖",
    "移动",
    "复制",
    "拷贝",
    "输出到",
    "生成到",
    "文件夹",
    "目录",
)
MANUAL_REVIEW_MARKERS = ("选择", "根据需要", "适合", "方法", "类型", "方案")


@dataclass(slots=True)
class TaskExecutionContext:
    user_input: str
    task_brief: TaskBrief | None = None
    primary_path: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    completed_code_tasks: set[str] = field(default_factory=set)
    pending_confirmations: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary_path": self.primary_path,
            "artifacts": self.artifacts,
            "findings": self.findings,
            "pending_confirmations": self.pending_confirmations,
            "completed_code_tasks": sorted(self.completed_code_tasks),
        }


@dataclass(slots=True)
class TaskStepRecord:
    index: int
    title: str
    status: StepStatus = "pending"
    result: str = ""
    error: str | None = None
    assigned_agent: str = "TaskAgent"
    step_type: StepType = "manual_review"
    requires_confirmation: bool = False
    confirmation_payload: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "assigned_agent": self.assigned_agent,
            "step_type": self.step_type,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_payload": self.confirmation_payload,
            "artifacts": self.artifacts,
            "tool_calls": self.tool_calls,
        }


@dataclass(slots=True)
class TaskExecutionResult:
    status: str
    summary: str
    steps: list[TaskStepRecord]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmations: list[dict[str, Any]] = field(default_factory=list)
    final_findings: list[dict[str, Any]] = field(default_factory=list)
    debug_events: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "steps": [item.as_dict() for item in self.steps],
            "tool_calls": self.tool_calls,
            "artifacts": self.artifacts,
            "pending_confirmations": self.pending_confirmations,
            "final_findings": self.final_findings,
            "debug_events": self.debug_events,
        }


@dataclass(slots=True)
class TaskAgent:
    """Coordinator that executes approved plans with specialist agents."""

    CAPABILITY = SubAgentSpec(
        name="task_agent",
        description="复杂任务计划步骤调度器，负责分类步骤、委派领域子 Agent、保留执行上下文并汇总阶段性结果。",
        handles=["多步骤任务执行", "计划确认后的任务调度", "跨文件、搜索和代码分析的阶段性执行"],
        does_not_handle=["纯聊天", "未经二次确认的高风险 workspace 写操作", "直接绕过子 Agent 调用底层工具"],
        capabilities=["task.execute", "task.summarize", "step.classify", "search.dispatch", "file.dispatch", "code.dispatch"],
        tools=["file_agent", "search_agent", "code_agent"],
        input_contract={"type": "approved_plan", "fields": ["user_input", "plan_steps", "task_brief"]},
        output_contract={"type": "TaskExecutionResult", "fields": ["summary", "steps", "tool_calls", "artifacts", "pending_confirmations", "final_findings"]},
        risk_level="medium",
        examples=["计划确认后分析表格并生成图表", "按步骤搜索并总结资料", "执行只读项目分析并记录步骤状态"],
    )

    file_agent: FileAgent
    search_agent: SearchAgent | None = None
    code_agent: CodeAgent | None = None

    @classmethod
    def capability_spec(cls) -> SubAgentSpec:
        return cls.CAPABILITY

    def execute(
        self,
        *,
        user_input: str,
        plan_steps: list[str],
        task_brief: TaskBrief | None = None,
    ) -> TaskExecutionResult:
        context = TaskExecutionContext(
            user_input=user_input,
            task_brief=task_brief,
            primary_path=self._extract_primary_path(user_input, task_brief),
        )
        records: list[TaskStepRecord] = []
        all_tool_calls: list[dict[str, Any]] = []
        debug_events: list[dict[str, Any]] = []

        for index, title in enumerate(plan_steps, start=1):
            step_type = self._classify_step(title, context)
            record = TaskStepRecord(index=index, title=title, status="running", step_type=step_type)
            if step_type == "workspace_write":
                self._mark_waiting_confirmation(record, context)
            elif step_type in {"data_analysis", "chart_generation", "code_analysis", "project_analysis", "script_execution"}:
                self._run_code_step(record, context)
            elif step_type == "search":
                self._run_search_step(record, context)
            elif step_type == "file_info":
                self._run_file_info_step(record, context)
            elif step_type == "file_read":
                self._run_file_step(record, context)
            else:
                self._run_manual_review_step(record, context)

            all_tool_calls.extend(record.tool_calls)
            records.append(record)
            debug_events.append(self._step_debug_event(record, context))

        status = self._overall_status(records)
        summary = self._summary(status, records)
        return TaskExecutionResult(
            status=status,
            summary=summary,
            steps=records,
            tool_calls=all_tool_calls,
            artifacts=context.artifacts,
            pending_confirmations=context.pending_confirmations,
            final_findings=context.findings,
            debug_events=debug_events,
        )

    def _classify_step(self, title: str, context: TaskExecutionContext) -> StepType:
        lowered = title.lower()
        title_path = self._extract_path(title)
        path = title_path or context.primary_path
        if self._looks_like_post_write_verification(title):
            return "manual_review"
        if self._looks_like_confirmation_policy_step(title):
            return "manual_review"
        if self._looks_like_file_check(title, title_path, has_context_path=bool(context.primary_path)):
            return "file_info"
        if any(marker in title for marker in SEARCH_STEP_MARKERS):
            return "search"
        if self._looks_like_manual_review(title):
            return "manual_review"
        if self._looks_like_workspace_write(title):
            return "workspace_write"
        if any(marker in title for marker in CHART_STEP_MARKERS):
            return "chart_generation"
        if any(marker in title for marker in SCRIPT_STEP_MARKERS):
            return "script_execution"
        if any(marker in title for marker in PROJECT_STEP_MARKERS):
            return "project_analysis"
        if path:
            if self._is_data_path(path) and (
                any(marker in title for marker in DATA_STEP_MARKERS)
                or any(marker in title for marker in CODE_STEP_MARKERS)
                or "pandas" in lowered
            ):
                return "data_analysis"
            if any(marker in title for marker in DATA_STEP_MARKERS):
                return "data_analysis"
            if any(marker in title for marker in CODE_STEP_MARKERS) or any(marker in lowered for marker in ("py", "js", "ts")):
                return "code_analysis"
            if any(marker in title for marker in READ_STEP_MARKERS):
                return "file_read"
        if any(marker in title for marker in MANUAL_REVIEW_MARKERS):
            return "manual_review"
        return "manual_review"

    def _run_code_step(self, record: TaskStepRecord, context: TaskExecutionContext) -> None:
        if self.code_agent is None:
            self._mark_failed(record, "CodeAgent 未注册，无法执行代码/数据步骤。")
            return
        path = self._extract_path(record.title) or context.primary_path
        if not path and record.step_type not in {"project_analysis", "script_execution"}:
            self._mark_failed(record, "缺少可分析的 workspace 文件路径。")
            return
        context.primary_path = context.primary_path or path
        intent = record.step_type
        if intent == "code_analysis":
            task_key = f"code_analysis:{path}"
        elif intent == "chart_generation":
            task_key = f"chart_generation:{path}"
        elif intent == "project_analysis":
            task_key = "project_analysis:workspace"
        elif intent == "script_execution":
            task_key = f"script_execution:{record.index}"
        else:
            task_key = f"data_analysis:{path}"

        if task_key in context.completed_code_tasks:
            record.assigned_agent = "TaskAgent"
            record.status = "completed"
            record.result = f"已复用前序 {path} 的 {record.step_type} 结果。"
            record.artifacts = list(context.artifacts)
            return

        brief = TaskBrief(
            intent=record.step_type,
            user_goal=context.user_input,
            normalized_input=f"{record.title}\n{context.user_input}",
            context={"path": path, "source_scope": "workspace", "artifact_expected": record.step_type == "chart_generation"},
            source_policy="workspace_only",
            expected_output="返回阶段性执行摘要、证据和 artifacts。",
            delegate_to="code_agent",
            confidence=0.72,
        )
        result = self.code_agent.handle_brief(brief)
        self._apply_sub_agent_result(record, result, "CodeAgent")
        record.artifacts = self._artifacts_from_result(result)
        if record.artifacts:
            context.artifacts.extend(self._new_artifacts(context.artifacts, record.artifacts))
        if result.findings:
            context.findings.extend(result.findings)
        if result.ok:
            context.completed_code_tasks.add(task_key)

    def _run_search_step(self, record: TaskStepRecord, context: TaskExecutionContext) -> None:
        if self.search_agent is None:
            self._mark_failed(record, "SearchAgent 未注册，无法执行搜索步骤。")
            return
        result = self.search_agent.handle_step(record.title, user_input=context.user_input)
        self._apply_sub_agent_result(record, result, "SearchAgent")
        if result.findings:
            context.findings.extend(result.findings)

    def _run_file_step(self, record: TaskStepRecord, context: TaskExecutionContext) -> None:
        path = self._extract_path(record.title) or context.primary_path
        step_text = record.title if not path else f"{record.title}\n目标文件：{path}"
        result = self.file_agent.handle_step(step_text, user_input=context.user_input)
        self._apply_sub_agent_result(record, result, "FileAgent")
        if result.findings:
            context.findings.extend(result.findings)

    def _run_file_info_step(self, record: TaskStepRecord, context: TaskExecutionContext) -> None:
        path = self._extract_path(record.title) or context.primary_path
        if not path:
            self._mark_failed(record, "缺少要检查的 workspace 文件路径。")
            return
        context.primary_path = context.primary_path or path
        payload = {"path": path}
        result = self.file_agent.tools.call("file_info", payload)
        record.assigned_agent = "FileAgent"
        record.tool_calls = [{"tool": "file_info", "payload": payload, "ok": result.ok, "error": result.error}]
        if result.ok:
            record.status = "completed"
            data = result.data or {}
            size = data.get("size")
            kind = data.get("type") or "file"
            suffix = f"，大小 {size} bytes" if size is not None else ""
            record.result = f"已确认 {path} 存在且可访问（{kind}{suffix}）。"
            context.findings.append({"title": "file_info", "summary": record.result, "path": path})
        else:
            record.status = "failed"
            record.error = result.error or "文件信息读取失败"
            record.result = record.error

    def _run_manual_review_step(self, record: TaskStepRecord, context: TaskExecutionContext) -> None:
        record.assigned_agent = "TaskAgent"
        record.status = "completed"
        if self._looks_like_post_write_verification(record.title):
            target = self._extract_directory(record.title) or self._extract_directory(context.user_input) or "目标目录"
            record.result = f"该步骤依赖二次确认后的 workspace 写入；当前尚未验证 {target} 中的最终文件列表。"
            return
        if record.title and any(marker in record.title for marker in MANUAL_REVIEW_MARKERS):
            basis = f"，基于 {context.primary_path}" if context.primary_path else ""
            record.result = f"该步骤属于方法/图表类型选择{basis}，已作为执行策略记录；具体分析由相邻数据分析或图表步骤完成。"
        else:
            record.result = "该步骤不需要调用外部工具，已作为任务执行上下文记录。"

    def _mark_waiting_confirmation(self, record: TaskStepRecord, context: TaskExecutionContext) -> None:
        payload = self._confirmation_payload(record.title, context)
        record.assigned_agent = "TaskAgent"
        record.status = "waiting_confirmation"
        record.requires_confirmation = True
        record.confirmation_payload = payload
        record.result = self._confirmation_message(payload)
        context.pending_confirmations.append(payload)

    def _apply_sub_agent_result(self, record: TaskStepRecord, result: SubAgentResult, assigned_agent: str) -> None:
        record.assigned_agent = assigned_agent
        record.tool_calls = result.tool_calls
        record.result = result.content
        if result.ok:
            record.status = result.status if result.status in {"waiting_confirmation", "completed"} else "completed"
            if record.status == "waiting_confirmation":
                record.requires_confirmation = True
        else:
            record.status = "failed"
            record.error = self._sub_agent_failure_detail(result)
            record.result = record.error

    def _mark_failed(self, record: TaskStepRecord, error: str) -> None:
        record.assigned_agent = "TaskAgent"
        record.status = "failed"
        record.error = error
        record.result = error

    def _confirmation_payload(self, title: str, context: TaskExecutionContext) -> dict[str, Any]:
        directory = self._extract_directory(title) or self._extract_directory(context.user_input)
        action = "workspace_write"
        if any(marker in title for marker in ("新建", "创建")):
            action = "create_directory"
        elif any(marker in title for marker in ("移动", "复制", "拷贝", "保存", "存放")):
            action = "publish_artifacts"
        return {
            "action": action,
            "step": title,
            "target_directory": directory,
            "artifacts": context.artifacts,
            "source_path": context.primary_path,
            "requires_confirmation": True,
        }

    def _confirmation_message(self, payload: dict[str, Any]) -> str:
        target = payload.get("target_directory") or "目标目录"
        if payload.get("action") == "create_directory":
            return f"该步骤需要二次确认后才能在 workspace 中创建目录：{target}。"
        if payload.get("action") == "publish_artifacts":
            artifacts = payload.get("artifacts") or []
            if artifacts:
                return f"该步骤需要二次确认后才能把 {len(artifacts)} 个 artifact 发布到 workspace 目录：{target}。"
            return f"该步骤需要二次确认后才能把生成结果保存到 workspace 目录：{target}。"
        return f"该步骤涉及 workspace 写入，需要二次确认后才能执行：{target}。"

    def _overall_status(self, records: list[TaskStepRecord]) -> str:
        if any(item.status == "waiting_confirmation" for item in records):
            return "waiting_confirmation"
        if any(item.status == "failed" for item in records):
            return "failed"
        return "completed"

    def _summary(self, status: str, records: list[TaskStepRecord]) -> str:
        completed = sum(1 for item in records if item.status == "completed")
        waiting = sum(1 for item in records if item.status == "waiting_confirmation")
        failed = sum(1 for item in records if item.status == "failed")
        parts = [f"TaskAgent 已完成 {completed} 个步骤"]
        if failed:
            parts.append(f"{failed} 个步骤失败")
        if waiting:
            parts.append(f"{waiting} 个步骤等待二次确认")
        suffix = "。" if status == "completed" else "，需要继续处理。"
        return "，".join(parts) + suffix

    def _looks_like_workspace_write(self, title: str) -> bool:
        if self._looks_like_file_check(title, self._extract_path(title)):
            return False
        if self._looks_like_post_write_verification(title):
            return False
        if any(
            marker in title
            for marker in (
                "新建",
                "创建",
                "移动",
                "复制",
                "拷贝",
                "写入",
                "修改",
                "改写",
                "删除",
                "重命名",
                "覆盖",
                "保存到",
                "保存至",
                "存放",
                "发布到",
                "输出到",
                "生成到",
            )
        ):
            return True
        return False

    def _looks_like_file_check(self, title: str, path: str, *, has_context_path: bool = False) -> bool:
        if not path and not has_context_path:
            return False
        write_markers = ("新建", "创建", "移动", "复制", "拷贝", "写入", "修改", "删除", "重命名", "覆盖", "保存到", "保存至", "发布到")
        if any(marker in title for marker in write_markers):
            return False
        explicit_file_check = any(marker in title for marker in ("是否存在", "可读", "可读性", "文件存在", "确认文件存在"))
        check_action = any(marker in title for marker in ("检查", "定位")) and "文件" in title
        return explicit_file_check or check_action

    def _looks_like_post_write_verification(self, title: str) -> bool:
        return "确认" in title and any(marker in title for marker in ("已保存", "成功保存", "文件列表", "列出"))

    def _looks_like_confirmation_policy_step(self, title: str) -> bool:
        return any(marker in title for marker in ("执行前请求用户确认", "请求用户确认关键操作", "标记需要", "确认关键操作"))

    def _looks_like_manual_review(self, title: str) -> bool:
        if not any(marker in title for marker in MANUAL_REVIEW_MARKERS):
            return False
        execution_markers = ("生成", "绘制", "画图", "保存", "输出", "写入", "读取", "查看", "pandas", "Python", "python")
        return not any(marker in title for marker in execution_markers)

    def _extract_primary_path(self, text: str, brief: TaskBrief | None = None) -> str:
        if brief is not None:
            path = str((brief.context or {}).get("path") or "").strip()
            if path:
                return path
        return self._extract_path(text)

    def _extract_path(self, text: str) -> str:
        match = PATH_HINT.search(text)
        if not match:
            return ""
        if match.group(1):
            return match.group(1).strip(" `，,。；;：:")
        raw = (match.group(2) or "").strip(" `，,。；;：:")
        parts = [item.strip(" `，,。；;：:") for item in raw.split() if item.strip()]
        candidate = parts[-1] if parts else raw
        return self._clean_path_candidate(candidate)

    def _clean_path_candidate(self, path: str) -> str:
        candidate = path.strip(" `，,。；;：:?？!！")
        prefixes = (
            "使用Python的pandas库读取",
            "使用 pandas 读取",
            "使用pandas读取",
            "读取并分析",
            "打开并读取",
            "检查当前目录下是否存在",
            "确认当前目录下是否存在",
            "检查",
            "确认",
            "是否存在",
            "存在",
            "读取",
            "查看",
            "分析",
        )
        suffixes = (
            "文件",
            "表格数据",
            "表格",
            "数据",
            "中的内容",
            "的内容",
            "可读性",
            "并确认其可读性",
        )
        changed = True
        while changed:
            changed = False
            for prefix in prefixes:
                if candidate.startswith(prefix):
                    candidate = candidate[len(prefix):].strip(" `，,。；;：:?？!！")
                    changed = True
            for suffix in suffixes:
                if candidate.endswith(suffix):
                    candidate = candidate[:-len(suffix)].strip(" `，,。；;：:?？!！")
                    changed = True
        ascii_match = re.search(
            r"([A-Za-z0-9_.\\/-]+\.(?:md|txt|py|js|jsx|ts|tsx|json|csv|xlsx|xls|ya?ml|html|css|xml|log|docx))$",
            candidate,
            re.IGNORECASE,
        )
        return ascii_match.group(1) if ascii_match else candidate

    def _extract_directory(self, text: str) -> str:
        explicit_dir = re.search(r"([A-Za-z0-9_.-]+)\s*(?:文件夹|目录)", text)
        if explicit_dir:
            return explicit_dir.group(1)
        explicit_tokens = [
            token for token in DIRECTORY_TOKEN_HINT.findall(text)
            if not re.search(r"\.(?:md|txt|py|js|jsx|ts|tsx|json|csv|xlsx|xls|ya?ml|html|css|xml|log|docx)$", token, re.IGNORECASE)
        ]
        if explicit_tokens:
            return explicit_tokens[-1]
        match = DIRECTORY_NAME_HINT.search(text) or DIRECTORY_TARGET_HINT.search(text)
        if not match and any(marker in text for marker in ("新建", "创建", "移动", "复制", "拷贝", "保存到", "保存至", "存放", "发布到", "输出到")):
            match = DIRECTORY_HINT.search(text)
        if not match:
            return ""
        raw = match.group(1).strip(" `，,。；;：:")
        token_matches = DIRECTORY_TOKEN_HINT.findall(raw)
        if token_matches:
            return token_matches[-1]
        for marker in ("当前目录下", "当前目录", "新建", "创建", "保存到", "存放到", "移动到", "复制到", "名为"):
            raw = raw.replace(marker, " ")
        parts = [item.strip(" `，,。；;：:") for item in raw.split() if item.strip()]
        return parts[-1] if parts else raw

    def _is_data_path(self, path: str) -> bool:
        return path.lower().endswith((".csv", ".json", ".xlsx", ".xls", ".txt", ".md"))

    def _is_code_data_path(self, path: str) -> bool:
        return path.lower().endswith((".csv", ".json", ".xlsx", ".xls", ".py", ".js", ".jsx", ".ts", ".tsx"))

    def _sub_agent_failure_detail(self, result: SubAgentResult) -> str:
        parts = [str(result.error or result.content or "子 Agent 执行失败。")]
        data = result.data if isinstance(result.data, dict) else {}
        sandbox = data.get("sandbox") if isinstance(data, dict) else None
        if isinstance(sandbox, dict):
            for key, label in (("error", "sandbox error"), ("stderr", "stderr"), ("stdout", "stdout")):
                value = str(sandbox.get(key) or "").strip()
                if value and value not in parts:
                    parts.append(f"{label}: {value[:700]}")
            if sandbox.get("exit_code") is not None:
                parts.append(f"exit_code: {sandbox.get('exit_code')}")
        if result.next_actions:
            parts.append("建议：" + "；".join(result.next_actions[:3]))
        return "\n".join(parts)

    def _step_debug_event(self, record: TaskStepRecord, context: TaskExecutionContext) -> dict[str, Any]:
        return {
            "stage": "TaskAgent.step",
            "agent": record.assigned_agent,
            "step_index": record.index,
            "step_type": record.step_type,
            "input": {
                "step": record.title,
                "context": context.as_dict(),
            },
            "output": {
                "result": record.result,
                "artifacts": record.artifacts,
                "confirmation_payload": record.confirmation_payload,
            },
            "status": record.status,
            "error": record.error,
            "tool_calls": record.tool_calls,
        }

    def _artifacts_from_result(self, result: SubAgentResult) -> list[dict[str, Any]]:
        data = result.data if isinstance(result.data, dict) else {}
        artifacts = data.get("artifacts", []) if isinstance(data, dict) else []
        return [item for item in artifacts if isinstance(item, dict)]

    def _new_artifacts(self, existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = {str(item.get("url") or item.get("path") or item.get("filename")) for item in existing}
        result = []
        for item in incoming:
            key = str(item.get("url") or item.get("path") or item.get("filename"))
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result
