"""Main agent orchestration for conversation and first-stage task routing."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable
from uuid import uuid4

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
except ImportError:  # pragma: no cover - requirements.txt installs langgraph.
    END = "__end__"
    START = "__start__"
    Command = None
    InMemorySaver = None
    StateGraph = None
    interrupt = None

from .sub_agents import SimpleChatAgent, SimpleTaskAgent, SubAgentRegistry, TaskAgent, create_default_sub_agent_registry
from .state import AgentState, ClarificationQuestion, Route, TaskAnalysis, TaskBrief, TaskPlan
from .tools import ToolRegistry, create_default_tool_registry
from .utils.llm_config import AppConfig
from .utils.llm_models import (
    GenerationOptions,
    ModelManager,
    ModelResponse,
    ModelSelection,
)
from .utils.time_utils import current_time_context, isoformat, now_local


COMPLEX_MARKERS = (
    "整个项目",
    "项目分析",
    "代码库",
    "修改代码",
    "改代码",
    "仓库",
    "实现",
    "修复",
    "优化",
    "重构",
    "新增",
    "删除",
    "重命名",
    "上传",
    "下载",
    "测试",
    "前端",
    "后端",
    "接口",
    "数据库",
    "文件管理",
    "计划",
    "方案",
    "报告",
    "分阶段",
    "自动执行",
)
MISSING_TARGET_REFS = (
    "这个文件",
    "那个文件",
    "这个文档",
    "那个文档",
    "这个目录",
    "那个目录",
    "这个表格",
    "那个表格",
)
MISSING_TARGET_ACTIONS = (
    "处理",
    "修改",
    "分析",
    "整理",
    "删除",
    "重命名",
    "上传",
    "下载",
    "转换",
    "总结",
    "修复",
)
PATH_HINT = re.compile(
    r"(`[^`]+`|[\w\u4e00-\u9fff .\\/-]+\.(?:md|txt|py|js|jsx|ts|tsx|json|ya?ml|html|css|csv|docx|pdf))",
    re.IGNORECASE,
)
SIMPLE_FILE_LIST_MARKERS = (
    "有哪些文件",
    "文件列表",
    "列出文件",
    "列一下文件",
    "查看目录",
    "目录列表",
    "文件树",
    "当前工作目录",
    "当前目录",
)
SIMPLE_FILE_SCOPE_MARKERS = ("当前项目", "当前工作目录", "当前目录", "workspace")
SIMPLE_FILE_READ_MARKERS = ("读取", "查看", "打开", "预览", "内容", "看一下")
SIMPLE_FILE_SEARCH_MARKERS = ("搜索", "查找", "检索", "找一下", "包含")
SIMPLE_FILE_INFO_MARKERS = ("信息", "属性", "大小", "类型", "元信息")
WEB_SEARCH_MARKERS = ("联网", "网上", "网络", "互联网", "web", "Web")
RESEARCH_MARKERS = ("调研", "研究")
RESEARCH_COMPLEX_MARKERS = ("整理", "对比", "报告", "分析", "方案", "总结")
HIGH_RISK_TOOL_MARKERS = ("写入", "保存", "修改", "改写", "删除", "重命名", "创建", "新增", "覆盖", "移动", "上传", "下载")
REALTIME_MARKERS = ("今天", "现在", "实时", "最新", "今年", "新闻", "天气", "比赛", "赛程", "价格", "预报")
WEATHER_MARKERS = ("天气", "气温", "降雨", "下雨", "预报", "空气质量")
COMMON_LOCATION_MARKERS = ("北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "重庆", "天津", "武汉", "西安")
SEARCH_TYPO_MAP = {
    "搜素": "搜索",
    "查讯": "查询",
}


AGENT_METADATA_KEY = "agent_dogs"
APP_METADATA_KEYS = {
    "created_at",
    "status",
    "interrupt",
    "plan_status",
    "task",
    "steps",
    "tool_calls",
    "debug_trace",
    "agent_flow",
    "task_brief",
    "route",
    "complexity",
    "clarification",
    "plan_steps",
    "plan",
}
MAX_DEBUG_TEXT = 900
MAX_DEBUG_EVENTS = 80


def _agent_response_metadata(message: AIMessage | HumanMessage) -> dict[str, Any]:
    response_metadata = getattr(message, "response_metadata", {}) or {}
    metadata = response_metadata.get(AGENT_METADATA_KEY)
    if isinstance(metadata, dict):
        return metadata
    legacy = getattr(message, "additional_kwargs", {}) or {}
    return legacy if isinstance(legacy, dict) else {}


def _safe_additional_kwargs(message: AIMessage) -> dict[str, Any]:
    return {
        key: value
        for key, value in (getattr(message, "additional_kwargs", {}) or {}).items()
        if key not in APP_METADATA_KEYS and not (key == "tool_calls" and not value)
    }


def _with_agent_metadata(message: AIMessage | HumanMessage, metadata: dict[str, Any]) -> AIMessage | HumanMessage:
    response_metadata = dict(getattr(message, "response_metadata", {}) or {})
    response_metadata[AGENT_METADATA_KEY] = metadata
    if isinstance(message, AIMessage):
        return AIMessage(
            content=message.content,
            additional_kwargs=_safe_additional_kwargs(message),
            response_metadata=response_metadata,
        )
    return HumanMessage(content=message.content, response_metadata=response_metadata)


class AgentInterruptError(RuntimeError):
    """Raised when a pending LangGraph interrupt cannot be resumed safely."""


class AgentRunCancelled(RuntimeError):
    """Raised when the current agent run was cancelled by the user."""


@dataclass(slots=True)
class MainAgent:
    config: AppConfig
    models: ModelManager | None = None
    tool_registry: ToolRegistry | None = None
    history: InMemoryChatMessageHistory = field(default_factory=InMemoryChatMessageHistory)
    simple_chat_agent: SimpleChatAgent = field(init=False, repr=False)
    simple_task_agent: SimpleTaskAgent = field(init=False, repr=False)
    search_agent: Any = field(init=False, repr=False)
    file_agent: Any = field(init=False, repr=False)
    sub_agent_registry: SubAgentRegistry = field(init=False, repr=False)
    task_agent: TaskAgent = field(init=False, repr=False)
    last_state: AgentState | None = field(default=None, init=False, repr=False)
    pending_interrupt: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _default_thread_id: str = field(default_factory=lambda: f"agent-{uuid4()}", init=False, repr=False)
    _graph: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        models = self.models or ModelManager(self.config)
        self.models = models
        self.tool_registry = self.tool_registry or create_default_tool_registry(search_config=self.config.search)
        self.simple_chat_agent = SimpleChatAgent(self.config, models, self.history)
        self.sub_agent_registry = create_default_sub_agent_registry(self.tool_registry, self.simple_chat_agent)
        self.simple_task_agent = self.sub_agent_registry.get("simple_task").agent
        self.search_agent = self.sub_agent_registry.get("search_agent").agent
        self.file_agent = self.sub_agent_registry.get("file_agent").agent
        self.task_agent = self.sub_agent_registry.get("task_agent").agent
        self._graph = self._build_graph()

    def chat(
        self,
        user_input: str,
        *,
        selection: ModelSelection | None = None,
        options: GenerationOptions | None = None,
        thinking_enabled: bool | None = None,
        thread_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ModelResponse:
        self._raise_if_cancelled(is_cancelled)
        if self.pending_interrupt is not None:
            raise AgentInterruptError("当前任务正在等待补充或确认，请先处理当前弹窗。")
        text = user_input.strip()
        if not text:
            raise ValueError("消息不能为空")
        if thinking_enabled is not None and options is None:
            options = GenerationOptions(thinking_enabled=thinking_enabled)
        now = now_local()

        initial_state: AgentState = {
            "messages": list(self.history.messages),
            "user_input": text,
            "current_time": now.isoformat(),
            "current_time_context": current_time_context(now),
            "selection": selection,
            "options": options,
            "errors": [],
            "final_response": "",
            "model_response": None,  # type: ignore[typeddict-item]
            "interrupt": None,  # type: ignore[typeddict-item]
            "interrupt_id": "",
            "plan_summary": "",
            "plan_steps": [],
            "plan_risks": [],
            "plan_status": None,  # type: ignore[typeddict-item]
            "task_status": "",
            "task_steps": [],
            "tool_calls": [],
            "debug_trace": [],
            "agent_flow": {},
            "task_brief": None,  # type: ignore[typeddict-item]
        }
        result_state = self._graph.invoke(initial_state, self._thread_config(thread_id))
        return self._handle_graph_result(
            result_state,
            user_record=text,
            is_cancelled=is_cancelled,
        )

    def resume(
        self,
        interrupt_id: str,
        payload: dict[str, Any],
        *,
        thread_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ModelResponse:
        self._raise_if_cancelled(is_cancelled)
        if Command is None:
            raise AgentInterruptError("当前环境不支持 LangGraph resume。")
        if self.pending_interrupt is None:
            raise AgentInterruptError("当前会话没有等待恢复的任务。")
        if self.pending_interrupt.get("id") != interrupt_id:
            raise AgentInterruptError("补充信息已过期，请重新打开当前任务弹窗。")
        expected_type = self.pending_interrupt.get("type")
        if payload.get("type") != expected_type:
            raise AgentInterruptError("恢复类型与当前中断任务不匹配。")

        resume_payload = self._normalize_resume_payload(payload)
        now = now_local()
        resume_payload["current_time"] = now.isoformat()
        resume_payload["current_time_context"] = current_time_context(now)
        result_state = self._graph.invoke(
            Command(resume=resume_payload),
            self._thread_config(thread_id),
        )
        return self._handle_graph_result(
            result_state,
            user_record=self._resume_summary(expected_type, resume_payload),
            is_cancelled=is_cancelled,
        )

    def clear(self) -> None:
        self.history.clear()
        self.last_state = None
        self.pending_interrupt = None

    def has_pending_interrupt(self) -> bool:
        return self.pending_interrupt is not None

    def response_metadata(self) -> dict[str, Any]:
        return self._message_metadata(self.last_state or {})

    def _raise_if_cancelled(self, is_cancelled: Callable[[], bool] | None) -> None:
        if is_cancelled and is_cancelled():
            raise AgentRunCancelled("当前对话已被用户中断。")

    def _thread_config(self, thread_id: str | None) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": thread_id or self._default_thread_id}}

    def _normalize_resume_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        interrupt_type = str(payload.get("type") or "")
        if interrupt_type == "clarification":
            answers = payload.get("answers") or {}
            if not isinstance(answers, dict):
                raise AgentInterruptError("补充信息格式不正确。")
            return {
                "type": "clarification",
                "answers": {str(key): str(value).strip() for key, value in answers.items()},
            }
        if interrupt_type == "plan_confirmation":
            decision = str(payload.get("decision") or "")
            if decision not in {"approve", "revise", "cancel"}:
                raise AgentInterruptError("计划确认操作不正确。")
            return {
                "type": "plan_confirmation",
                "decision": decision,
                "feedback": str(payload.get("feedback") or "").strip(),
            }
        raise AgentInterruptError("未知的恢复类型。")

    def _handle_graph_result(
        self,
        result_state: AgentState,
        *,
        user_record: str | None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ModelResponse:
        interrupts = result_state.get("__interrupt__") or []
        if interrupts:
            return self._handle_interrupt(
                result_state,
                interrupts[0],
                user_record=user_record,
                is_cancelled=is_cancelled,
            )

        self._raise_if_cancelled(is_cancelled)
        self.pending_interrupt = None
        result_state["status"] = "completed"
        self.last_state = result_state
        result = result_state.get("model_response")
        if result is None:
            raise RuntimeError("Agent graph completed without a model response")

        result = self._with_message_metadata(result, result_state)
        if user_record:
            self._record_history(user_record, result.message)
        return result

    def _handle_interrupt(
        self,
        result_state: AgentState,
        interrupt_obj: Any,
        *,
        user_record: str | None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ModelResponse:
        self._raise_if_cancelled(is_cancelled)
        payload = interrupt_obj.value if isinstance(interrupt_obj.value, dict) else {}
        interrupt_id = str(getattr(interrupt_obj, "id", "") or uuid4().hex)
        interrupt_type = str(payload.get("type") or "clarification")
        content = str(payload.get("message") or "任务已暂停，等待你的确认。")

        state: AgentState = {
            key: value for key, value in result_state.items()
            if key != "__interrupt__"
        }
        state["status"] = "interrupted"
        state["interrupt_type"] = interrupt_type  # type: ignore[typeddict-item]
        state["interrupt_id"] = interrupt_id
        state["interrupt"] = self._interrupt_metadata(interrupt_id, payload)
        if interrupt_type == "plan_confirmation":
            state["plan_status"] = "pending"
        self.last_state = state
        self.pending_interrupt = {
            "id": interrupt_id,
            "type": interrupt_type,
            "payload": payload,
        }

        message = _with_agent_metadata(AIMessage(content=content), self._message_metadata(state))
        if user_record:
            self._record_history(user_record, message)
        selected = self._selected_model(state.get("selection"))
        return ModelResponse(
            content=content,
            message=message,
            provider=selected.provider,
            model=selected.model,
            raw_content=content,
        )

    def _build_graph(self) -> Any:
        if StateGraph is None:
            return _LinearAgentGraph(self)

        graph = StateGraph(AgentState)
        graph.add_node("analyze_task", self._analyze_task)
        graph.add_node("route_task", self._route_task)
        graph.add_node("simple_chat", self._simple_chat)
        graph.add_node("simple_task", self._simple_task)
        graph.add_node("clarify_interrupt", self._clarify_interrupt)
        graph.add_node("generate_plan", self._generate_plan)
        graph.add_node("plan_confirm_interrupt", self._plan_confirm_interrupt)
        graph.add_node("execute_task", self._execute_task)
        graph.add_node("finalize", self._finalize)

        graph.add_edge(START, "analyze_task")
        graph.add_edge("analyze_task", "route_task")
        graph.add_conditional_edges(
            "route_task",
            self._route_from_state,
            {
                "simple_chat": "simple_chat",
                "simple_task": "simple_task",
                "clarify": "clarify_interrupt",
                "future_task": "generate_plan",
            },
        )
        graph.add_edge("simple_chat", END)
        graph.add_edge("simple_task", "finalize")
        graph.add_edge("clarify_interrupt", "generate_plan")
        graph.add_edge("generate_plan", "plan_confirm_interrupt")
        graph.add_conditional_edges(
            "plan_confirm_interrupt",
            self._route_after_plan_confirmation,
            {
                "revise": "generate_plan",
                "execute_task": "execute_task",
                "finalize": "finalize",
            },
        )
        graph.add_edge("execute_task", "finalize")
        graph.add_edge("finalize", END)
        if InMemorySaver is None:
            return graph.compile()
        return graph.compile(checkpointer=InMemorySaver())

    def _analyze_task(self, state: AgentState) -> AgentState:
        text = state["user_input"]
        errors = list(state.get("errors", []))
        try:
            analysis = self._rule_analysis(text)
            if analysis is None:
                analysis = self._llm_analysis(text, state.get("selection"), state.get("current_time_context", ""))
        except Exception as exc:  # Keep routing available when judgment fails.
            errors.append(f"analyze_task: {exc}")
            analysis = TaskAnalysis(
                intent=text[:80],
                complexity="simple",
                confidence=0.3,
                reason="任务解析失败，回退为普通对话。",
            )
        brief = self._build_task_brief(text, analysis, state)
        output = {
            "intent": analysis.intent,
            "complexity": analysis.complexity,
            "route_hint": analysis.route_hint,
            "tool_intents": analysis.tool_intents,
            "reason": analysis.reason,
            "task_brief": brief.model_dump(),
        }
        return {
            "task_analysis": analysis,
            "task_brief": brief,
            "missing_info": analysis.missing_info,
            "clarification_questions": self._clarification_questions(analysis),
            "plan_steps": analysis.suggested_steps,
            "errors": errors,
            "debug_trace": self._debug_trace(
                state,
                stage="MainAgent.analyze_task",
                agent="MainAgent",
                input=state["user_input"],
                output=output,
                status="completed",
            ),
        }

    def _route_task(self, state: AgentState) -> AgentState:
        analysis = state["task_analysis"]
        route: Route
        if analysis.route_hint:
            route = analysis.route_hint
        elif analysis.complexity == "needs_info":
            route = "clarify"
        elif analysis.complexity == "complex":
            route = "future_task"
        else:
            route = "simple_chat"
        return {
            "route": route,
            "debug_trace": self._debug_trace(
                state,
                stage="MainAgent.route_task",
                agent="MainAgent",
                input=getattr(analysis, "complexity", ""),
                output={"route": route},
                status="completed",
                route=route,
            ),
        }

    def _route_from_state(self, state: AgentState) -> Route:
        return state.get("route", "simple_chat")

    def _route_after_plan_confirmation(self, state: AgentState) -> str:
        if state.get("plan_decision") == "revise":
            return "revise"
        if state.get("plan_decision") == "approve":
            return "execute_task"
        return "finalize"

    def _build_task_brief(self, text: str, analysis: TaskAnalysis, state: AgentState) -> TaskBrief:
        normalized = self._normalize_user_input(text)
        context = self._brief_context(normalized, state)
        delegate_to = self._delegate_for_analysis(analysis, normalized, context)
        source_policy = "requires_fresh_external_info" if self._requires_fresh_external_info(normalized) else "not_required"
        if context.get("source_scope") == "workspace":
            source_policy = "workspace_only"
        return TaskBrief(
            intent=self._brief_intent(normalized, analysis),
            user_goal=text.strip(),
            normalized_input=normalized,
            context=context,
            constraints=self._brief_constraints(normalized, source_policy),
            source_policy=source_policy,
            expected_output=self._brief_expected_output(normalized, analysis),
            delegate_to=delegate_to,
            confidence=analysis.confidence,
        )

    def _normalize_user_input(self, text: str) -> str:
        normalized = text.strip()
        for source, target in SEARCH_TYPO_MAP.items():
            normalized = normalized.replace(source, target)
        return re.sub(r"\s+", " ", normalized)

    def _brief_context(self, text: str, state: AgentState) -> dict[str, Any]:
        context: dict[str, Any] = {}
        current_date = str(state.get("current_time") or "").split("T", 1)[0]
        if "今天" in text and current_date:
            context["relative_time"] = "今天"
            context["date"] = current_date
        if "今年" in text and current_date:
            context["relative_time"] = "今年"
            context["year"] = current_date[:4]
        location = self._extract_location(text)
        if location:
            context["location"] = location
        path = self._extract_path_for_brief(text)
        if path:
            context["path"] = path
        if self._requires_fresh_external_info(text):
            context["source_scope"] = "web"
        elif any(marker in text for marker in SIMPLE_FILE_SCOPE_MARKERS) or "文件" in text or "项目" in text:
            context["source_scope"] = "workspace"
        if self._is_weather_request(text):
            context["domain"] = "weather"
            pieces = [location, context.get("date"), "天气 预报"]
            context["query"] = " ".join(str(item) for item in pieces if item)
        elif self._requires_fresh_external_info(text):
            context["query"] = text
        return {key: value for key, value in context.items() if value not in (None, "", [], {})}

    def _delegate_for_analysis(self, analysis: TaskAnalysis, text: str, context: dict[str, Any]) -> str:
        if analysis.route_hint == "simple_chat":
            return "simple_chat"
        if analysis.route_hint in {"clarify", "future_task"}:
            return analysis.route_hint
        if self._is_search_delegate(text, context):
            return "search_agent"
        if "list_workspace_tree" in analysis.tool_intents:
            return "simple_task"
        if context.get("path") or analysis.tool_intents and any(intent in analysis.tool_intents for intent in ("read_file", "file_info", "list_workspace_tree")):
            return "file_agent"
        if "workspace_search" in analysis.tool_intents:
            return "search_agent"
        return "simple_task"

    def _brief_intent(self, text: str, analysis: TaskAnalysis) -> str:
        if self._is_weather_request(text):
            return "weather_lookup"
        if self._is_search_delegate(text, {}):
            return "search"
        return analysis.task_kind or "chat"

    def _brief_constraints(self, text: str, source_policy: str) -> list[str]:
        constraints: list[str] = []
        if source_policy == "requires_fresh_external_info":
            constraints.append("需要使用新鲜外部信息，不能只依赖模型历史知识。")
        if any(marker in text for marker in HIGH_RISK_TOOL_MARKERS):
            constraints.append("涉及高风险操作时必须先请求用户确认。")
        return constraints

    def _brief_expected_output(self, text: str, analysis: TaskAnalysis) -> str:
        if self._is_weather_request(text):
            return "给出简洁天气结论，并说明来源或无法确认的原因。"
        if self._is_search_delegate(text, {}):
            return "返回精炼搜索结论、关键发现和证据来源。"
        if analysis.route_hint == "simple_chat":
            return "直接回答用户问题。"
        return "返回任务执行摘要、关键结果和必要的下一步。"

    def _extract_location(self, text: str) -> str:
        for location in COMMON_LOCATION_MARKERS:
            if location in text:
                return location
        return ""

    def _extract_path_for_brief(self, text: str) -> str:
        match = PATH_HINT.search(text)
        if not match:
            return ""
        raw = match.group(1).strip("`") if match.group(1) else match.group(0).strip("`")
        parts = [item.strip(" `，。；;：:?？!！") for item in raw.split() if item.strip()]
        candidate = parts[-1] if parts else raw
        for prefix in ("请帮我查看", "请帮我读取", "帮我查看", "帮我读取", "请查看", "请读取", "查看", "读取", "打开", "预览", "帮我", "请"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):].strip(" `，。；;：:?？!！")
        for suffix in ("中的内容是什么", "里面的内容是什么", "的内容是什么", "中的内容", "里面的内容", "的内容", "内容是什么"):
            if candidate.endswith(suffix):
                candidate = candidate[:-len(suffix)].strip(" `，。；;：:?？!！")
        return candidate

    def _requires_fresh_external_info(self, text: str) -> bool:
        return any(marker in text for marker in REALTIME_MARKERS)

    def _is_weather_request(self, text: str) -> bool:
        return any(marker in text for marker in WEATHER_MARKERS)

    def _is_search_delegate(self, text: str, context: dict[str, Any]) -> bool:
        if context.get("source_scope") == "web":
            return True
        return self._requires_fresh_external_info(text) or any(marker in text for marker in SIMPLE_FILE_SEARCH_MARKERS + ("查询", "查一下", "搜一下", "搜索"))

    def _debug_trace(
        self,
        state: AgentState,
        *,
        stage: str,
        agent: str,
        input: Any | None = None,
        output: Any | None = None,
        status: str | None = None,
        route: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> list[dict[str, Any]]:
        trace = list(state.get("debug_trace") or [])
        event = {
            "stage": stage,
            "agent": agent,
            "input": self._debug_value(input),
            "output": self._debug_value(output),
            "status": status,
            "route": route,
            "tool_calls": self._debug_value(tool_calls or []),
            "error": error,
        }
        trace.append({key: value for key, value in event.items() if value not in (None, [], {})})
        return trace[-MAX_DEBUG_EVENTS:]

    def _tool_debug_events(self, state: AgentState, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        trace = list(state.get("debug_trace") or [])
        for call in tool_calls:
            trace.append({
                "stage": "ToolRegistry.call",
                "agent": "ToolRegistry",
                "input": self._debug_value(call.get("payload")),
                "output": {"tool": call.get("tool"), "ok": call.get("ok")},
                "status": "completed" if call.get("ok") else "failed",
                "tool_calls": [self._debug_value(call)],
            })
        return trace[-MAX_DEBUG_EVENTS:]

    def _debug_value(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            return value if len(value) <= MAX_DEBUG_TEXT else f"{value[:MAX_DEBUG_TEXT]}..."
        if isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, list):
            return [self._debug_value(item) for item in value[:20]]
        if isinstance(value, tuple):
            return [self._debug_value(item) for item in value[:20]]
        if isinstance(value, dict):
            return {
                str(key): self._debug_value(item)
                for key, item in list(value.items())[:30]
                if key not in {"content"}
            }
        if hasattr(value, "model_dump"):
            return self._debug_value(value.model_dump())
        return self._debug_value(str(value))

    def _sub_agent_output(self, result: Any) -> dict[str, Any]:
        return {
            "content": getattr(result, "content", ""),
            "summary": getattr(result, "summary", ""),
            "findings": getattr(result, "findings", []),
            "evidence": getattr(result, "evidence", []),
            "next_actions": getattr(result, "next_actions", []),
            "confidence": getattr(result, "confidence", None),
            "error": getattr(result, "error", None),
        }

    def _build_agent_flow(self, state: AgentState) -> dict[str, Any]:
        trace = [item for item in state.get("debug_trace") or [] if isinstance(item, dict)]
        analysis = state.get("task_analysis")
        brief = state.get("task_brief")
        route = state.get("route")
        final_output = self._debug_value(state.get("final_response", ""))
        main_events = [item for item in trace if item.get("agent") == "MainAgent"]
        main_agent: dict[str, Any] = {
            "name": "MainAgent",
            "input": self._debug_value(state.get("user_input", "")),
            "analysis": self._debug_value(analysis),
            "taskBrief": self._debug_value(brief),
            "complexity": getattr(analysis, "complexity", None),
            "route": route,
            "routeReason": getattr(analysis, "reason", None),
            "status": state.get("status", "completed"),
            "planStatus": state.get("plan_status"),
            "plan": self._debug_value({
                "summary": state.get("plan_summary"),
                "steps": state.get("plan_steps"),
                "risks": state.get("plan_risks"),
            }) if state.get("plan_steps") or state.get("plan_summary") else None,
            "finalOutput": final_output,
            "events": main_events,
        }
        main_agent = {key: value for key, value in main_agent.items() if value not in (None, [], {})}

        sub_agents = self._agent_flow_sub_agents(state, trace)
        tools = self._agent_flow_tools(state, trace)
        return {
            "mainAgent": main_agent,
            "subAgents": sub_agents,
            "tools": tools,
            "finalOutput": final_output,
            "errors": self._debug_value(state.get("errors") or []),
        }

    def _agent_flow_sub_agents(self, state: AgentState, trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sub_agents: list[dict[str, Any]] = []
        for event in trace:
            agent_name = str(event.get("agent") or "")
            if not agent_name or agent_name in {"MainAgent", "ToolRegistry"}:
                continue
            sub_agents.append(self._agent_flow_sub_agent_event(agent_name, event))

        for step in state.get("task_steps") or []:
            if not isinstance(step, dict):
                continue
            agent_name = str(step.get("assigned_agent") or "")
            if not agent_name:
                continue
            payload = self._agent_flow_sub_agent_event(
                agent_name,
                {
                    "input": {"step": step.get("title"), "index": step.get("index")},
                    "output": step.get("result"),
                    "status": step.get("status"),
                    "error": step.get("error"),
                    "tool_calls": step.get("tool_calls") or [],
                },
            )
            payload["stepIndex"] = step.get("index")
            sub_agents.append(payload)
        return sub_agents[:40]

    def _agent_flow_sub_agent_event(self, agent_name: str, event: dict[str, Any]) -> dict[str, Any]:
        spec = self._sub_agent_spec(agent_name)
        payload: dict[str, Any] = {
            "name": agent_name,
            "type": spec.get("name") if spec else agent_name,
            "description": spec.get("description") if spec else "",
            "capabilitySpec": spec,
            "capabilities": spec.get("capabilities") if spec else [],
            "input": self._debug_value(event.get("input")),
            "output": self._debug_value(event.get("output")),
            "status": event.get("status"),
            "error": event.get("error"),
            "relatedToolCalls": self._debug_value(event.get("tool_calls") or []),
        }
        return {key: value for key, value in payload.items() if value not in (None, [], {})}

    def _agent_flow_tools(self, state: AgentState, trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for event in trace:
            if event.get("agent") != "ToolRegistry":
                continue
            output = event.get("output") if isinstance(event.get("output"), dict) else {}
            call = (event.get("tool_calls") or [{}])[0]
            if not isinstance(call, dict):
                call = {}
            tools.append({
                "name": output.get("tool") or call.get("tool"),
                "input": self._debug_value(event.get("input")),
                "output": self._debug_value(output),
                "ok": output.get("ok") if "ok" in output else call.get("ok"),
                "status": event.get("status"),
                "error": event.get("error") or call.get("error"),
            })
        if tools:
            return [{key: value for key, value in item.items() if value not in (None, [], {})} for item in tools[:40]]
        return [
            {
                "name": call.get("tool"),
                "input": self._debug_value(call.get("payload")),
                "ok": call.get("ok"),
                "status": "completed" if call.get("ok") else "failed",
                "error": call.get("error"),
            }
            for call in (state.get("tool_calls") or [])[:40]
            if isinstance(call, dict)
        ]

    def _sub_agent_spec(self, agent_name: str) -> dict[str, Any] | None:
        normalized = agent_name.replace("Agent", "").replace("_", "").lower()
        for spec in self.sub_agent_registry.list_specs():
            names = {
                spec.name.replace("_", "").lower(),
                spec.name.lower(),
                "".join(part.title() for part in spec.name.split("_")).lower(),
            }
            if normalized in names or agent_name.lower() in names:
                return {
                    "name": spec.name,
                    "description": spec.description,
                    "handles": spec.handles,
                    "doesNotHandle": spec.does_not_handle,
                    "capabilities": spec.capabilities,
                    "tools": spec.tools,
                    "inputContract": spec.input_contract,
                    "outputContract": spec.output_contract,
                    "riskLevel": spec.risk_level,
                    "examples": spec.examples,
                }
        return None

    def _synthesize_task_result(self, state: AgentState, result: Any, agent_name: str) -> str:
        brief = state.get("task_brief")
        if not getattr(result, "ok", False):
            return str(getattr(result, "error", None) or getattr(result, "content", "") or "任务执行失败。")

        if agent_name == "SearchAgent":
            return self._synthesize_search_result(brief if isinstance(brief, TaskBrief) else None, result)
        return str(getattr(result, "content", "") or getattr(result, "summary", "") or "任务已完成。")

    def _synthesize_search_result(self, brief: TaskBrief | None, result: Any) -> str:
        content = str(getattr(result, "content", "") or "")
        summary = str(getattr(result, "summary", "") or "")
        data = getattr(result, "data", None) or {}
        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            return summary or content or "没有找到可汇总的搜索结果。"

        intent = brief.intent if brief else ""
        if intent == "weather_lookup":
            return self._synthesize_weather_result(brief, results, summary or content)

        findings = getattr(result, "findings", None) or self._findings_from_raw_results(results)
        lines = [f"我找到 {len(results)} 条相关结果，先给你一个简要汇总："]
        for index, item in enumerate(findings[:3], start=1):
            title = str(item.get("title") or "结果").strip()
            item_summary = str(item.get("summary") or "").strip()
            source = str(item.get("source") or item.get("url") or item.get("path") or "").strip()
            detail = f"{index}. {title}"
            if item_summary:
                detail = f"{detail}：{item_summary[:160]}"
            if source:
                detail = f"{detail}（来源：{source}）"
            lines.append(detail)
        if len(results) > 3:
            lines.append("更多原始搜索结果可以在调试信息里查看。")
        return "\n".join(lines)

    def _synthesize_weather_result(self, brief: TaskBrief | None, results: list[dict[str, Any]], fallback: str) -> str:
        context = brief.context if brief else {}
        location = str(context.get("location") or "").strip() or "目标地区"
        date = str(context.get("date") or context.get("relative_time") or "").strip()
        best = self._best_weather_result(results, date)
        combined = self._combined_result_text([best] + [item for item in results if item is not best])
        condition = self._extract_weather_condition(combined)
        temperature = self._extract_temperature_range(combined)
        air_quality = self._extract_air_quality(combined)
        source = str(best.get("source") or "").strip()
        url = str(best.get("url") or best.get("path") or "").strip()

        pieces = []
        if condition:
            pieces.append(condition)
        if temperature:
            pieces.append(f"气温约 {temperature}")
        if air_quality:
            pieces.append(f"空气质量{air_quality}")

        when = f"{date} " if date else ""
        if pieces:
            answer = f"{location}{when}天气：{'，'.join(pieces)}。"
        else:
            short = self._compact_search_excerpt(best) or fallback
            answer = f"我找到了{location}{when}天气相关结果，但没有稳定提取出完整天气字段。{short[:220]}"

        if source or url:
            answer = f"{answer}\n来源：{source or '搜索结果'}{f'，{url}' if url else ''}"
        answer = f"{answer}\n原始搜索结果已保留在调试信息中。"
        return answer

    def _best_weather_result(self, results: list[dict[str, Any]], date: str = "") -> dict[str, Any]:
        if not results:
            return {}
        compact_date = date.replace("-", "")
        zh_date = ""
        if re.match(r"\d{4}-\d{2}-\d{2}$", date):
            year, month, day = date.split("-")
            zh_date = f"{year}年{int(month):02d}月{int(day):02d}日"
        preferred_sources = ("weather.com.cn", "cma.gov.cn", "tianqi.com")

        def score(item: dict[str, Any]) -> int:
            haystack = self._combined_result_text([item])
            identity = f"{item.get('source') or ''} {item.get('url') or ''} {item.get('title') or ''}".lower()
            value = 0
            if date and date in haystack:
                value += 5
            if compact_date and compact_date in identity:
                value += 5
            if zh_date and zh_date in haystack:
                value += 5
            if self._extract_temperature_range(haystack):
                value += 3
            if self._extract_weather_condition(haystack):
                value += 2
            for index, source in enumerate(preferred_sources):
                if source in identity:
                    value += len(preferred_sources) - index
            return value

        return max(results, key=score)

    def _combined_result_text(self, results: list[dict[str, Any]]) -> str:
        chunks: list[str] = []
        for item in results[:5]:
            for key in ("title", "summary", "content_excerpt"):
                value = str(item.get(key) or "").strip()
                if value:
                    chunks.append(value)
        return "\n".join(chunks)

    def _compact_search_excerpt(self, item: dict[str, Any]) -> str:
        for key in ("summary", "content_excerpt", "title"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    def _extract_temperature_range(self, text: str) -> str:
        match = re.search(r"(-?\d{1,2})\s*[~～\-—至到]\s*(-?\d{1,2})\s*[℃°]?", text)
        if match:
            return f"{match.group(1)}~{match.group(2)}℃"
        high = re.search(r"最高(?:气温|温度)?\D{0,8}(-?\d{1,2})\s*[℃°]?", text)
        low = re.search(r"最低(?:气温|温度)?\D{0,8}(-?\d{1,2})\s*[℃°]?", text)
        if high and low:
            return f"{low.group(1)}~{high.group(1)}℃"
        return ""

    def _extract_weather_condition(self, text: str) -> str:
        conditions = (
            "雷阵雨",
            "阵雨",
            "小雨",
            "中雨",
            "大雨",
            "暴雨",
            "多云",
            "晴",
            "阴",
            "雨夹雪",
            "小雪",
            "中雪",
            "大雪",
            "雾",
            "霾",
        )
        for condition in conditions:
            if condition in text:
                return condition
        return ""

    def _extract_air_quality(self, text: str) -> str:
        levels = "优|良|轻度污染|中度污染|重度污染|严重污染"
        match = re.search(rf"(?:空气质量|空气|AQI)[^\n，。:：]{{0,20}}({levels})", text)
        if match:
            return match.group(1)
        match = re.search(rf"\b\d{{1,3}}\s*({levels})\b", text)
        if match:
            return match.group(1)
        return ""

    def _findings_from_raw_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "title": item.get("title") or item.get("path") or item.get("url") or "结果",
                "summary": str(item.get("summary") or item.get("content_excerpt") or "")[:240],
                "source": item.get("source") or item.get("url") or item.get("path") or "",
            }
            for item in results[:5]
        ]

    def _simple_chat(self, state: AgentState) -> AgentState:
        result = self.simple_chat_agent.chat(
            state["user_input"],
            selection=state.get("selection"),
            options=state.get("options"),
            current_time=state.get("current_time_context", ""),
        )
        return {
            "model_response": result,
            "final_response": result.content,
            "debug_trace": self._debug_trace(
                state,
                stage="MainAgent.simple_chat",
                agent="SimpleChatAgent",
                input=state["user_input"],
                output=result.content,
                status="completed",
                route=state.get("route"),
            ),
        }

    def _simple_task(self, state: AgentState) -> AgentState:
        brief = state.get("task_brief")
        if isinstance(brief, TaskBrief) and brief.delegate_to == "search_agent":
            result = self.search_agent.handle_brief(brief)
            agent_name = "SearchAgent"
        elif isinstance(brief, TaskBrief) and brief.delegate_to == "file_agent":
            result = self.file_agent.handle_brief(brief)
            agent_name = "FileAgent"
        else:
            result = self.simple_task_agent.handle(state["user_input"])
            agent_name = "SimpleTaskAgent"
        status = result.status if result.ok else "failed"
        trace = self._debug_trace(
            state,
            stage="MainAgent.simple_task",
            agent=agent_name,
            input=brief.model_dump() if isinstance(brief, TaskBrief) else state["user_input"],
            output=self._sub_agent_output(result),
            status=status,
            route=state.get("route"),
            tool_calls=result.tool_calls,
            error=result.error,
        )
        trace = self._tool_debug_events({"debug_trace": trace}, result.tool_calls)
        final_response = self._synthesize_task_result(state, result, agent_name)
        trace = self._debug_trace(
            {**state, "debug_trace": trace},
            stage="MainAgent.synthesize_result",
            agent="MainAgent",
            input={
                "agent": agent_name,
                "task_brief": brief.model_dump() if isinstance(brief, TaskBrief) else None,
                "sub_agent_result": self._sub_agent_output(result),
            },
            output=final_response,
            status=status,
            route=state.get("route"),
        )
        return {
            "task_status": status,
            "tool_calls": result.tool_calls,
            "final_response": final_response,
            "debug_trace": trace,
        }

    def _clarify_interrupt(self, state: AgentState) -> AgentState:
        if interrupt is None:
            return self._clarify_fallback(state)
        questions = state.get("clarification_questions") or []
        count = len(questions) or len(state.get("missing_info") or [])
        suffix = f"我需要先确认 {count} 个关键信息。" if count else "我需要先确认几个关键信息。"
        payload = {
            "type": "clarification",
            "message": f"这个任务还需要补充信息，才能继续执行。{suffix}",
            "clarification": {
                "original_message": state["user_input"],
                "questions": [
                    question.model_dump()
                    for question in questions
                ],
            },
        }
        answer = interrupt(payload)
        answers = answer.get("answers", {}) if isinstance(answer, dict) else {}
        return {
            "clarification_answers": {
                str(key): str(value).strip()
                for key, value in answers.items()
            },
            "route": "future_task",
        }

    def _clarify_fallback(self, state: AgentState) -> AgentState:
        questions = state.get("clarification_questions") or []
        count = len(questions) or len(state.get("missing_info") or [])
        suffix = f"我需要先确认 {count} 个关键信息。" if count else "我需要先确认几个关键信息。"
        return {"final_response": f"这个任务还需要补充信息，才能继续执行。{suffix}"}

    def _generate_plan(self, state: AgentState) -> AgentState:
        errors = list(state.get("errors", []))
        try:
            plan = self._llm_plan(state)
        except Exception as exc:
            errors.append(f"generate_plan: {exc}")
            base_steps = state.get("plan_steps") or self._default_plan_steps(state["user_input"])
            plan = TaskPlan(
                summary=f"围绕“{state['user_input'][:60]}”完成一个需要确认后再执行的复杂任务。",
                steps=base_steps,
                risks=["第一阶段只确认计划，不自动修改文件或执行工具。"],
                requires_confirmation=True,
            )
        return {
            "route": "future_task",
            "task_plan": plan,
            "plan_summary": plan.summary,
            "plan_steps": plan.steps,
            "plan_risks": plan.risks,
            "plan_status": "pending",
            "errors": errors,
            "debug_trace": self._debug_trace(
                state,
                stage="MainAgent.generate_plan",
                agent="MainAgent",
                input=state["user_input"],
                output=plan.model_dump(),
                status="completed",
                route="future_task",
            ),
        }

    def _plan_confirm_interrupt(self, state: AgentState) -> AgentState:
        if interrupt is None:
            return self._plan_fallback(state)
        plan = self._plan_from_state(state)
        payload = {
            "type": "plan_confirmation",
            "message": "我已经整理出执行计划，请确认后再继续。",
            "plan": plan.model_dump(),
        }
        answer = interrupt(payload)
        decision = answer.get("decision", "approve") if isinstance(answer, dict) else "approve"
        feedback = answer.get("feedback", "") if isinstance(answer, dict) else ""
        if decision == "revise":
            return {
                "plan_decision": "revise",
                "plan_feedback": str(feedback).strip(),
                "plan_status": "revised",
            }
        if decision == "cancel":
            return {
                "plan_decision": "cancel",
                "plan_feedback": str(feedback).strip(),
                "plan_status": "cancelled",
                "final_response": "已取消当前任务。后续如果需要，可以重新发起任务或调整目标后再试。",
            }
        return {
            "plan_decision": "approve",
            "plan_feedback": str(feedback).strip(),
            "plan_status": "approved",
        }

    def _execute_task(self, state: AgentState) -> AgentState:
        steps = state.get("plan_steps") or self._default_plan_steps(state["user_input"])
        result = self.task_agent.execute(user_input=state["user_input"], plan_steps=steps)
        lines = [
            "计划已确认，TaskAgent 已完成第一阶段执行。",
            "",
            result.summary,
            "",
            "步骤结果：",
        ]
        for step in result.steps:
            label = {
                "completed": "完成",
                "failed": "失败",
                "waiting_confirmation": "等待确认",
                "running": "运行中",
                "pending": "待处理",
            }.get(step.status, step.status)
            detail = step.result or step.error or "无结果"
            lines.append(f"{step.index}. [{label}] {step.title}\n   {detail}")
        trace = self._debug_trace(
            state,
            stage="MainAgent.execute_task",
            agent="TaskAgent",
            input={"user_input": state["user_input"], "plan_steps": steps},
            output=result.as_dict(),
            status=result.status,
            route=state.get("route"),
            tool_calls=result.tool_calls,
        )
        trace = self._tool_debug_events({"debug_trace": trace}, result.tool_calls)
        return {
            "task_status": result.status,
            "task_steps": [item.as_dict() for item in result.steps],
            "tool_calls": result.tool_calls,
            "final_response": "\n".join(lines),
            "debug_trace": trace,
        }

    def _plan_fallback(self, state: AgentState) -> AgentState:
        plan = self._plan_from_state(state)
        lines = [
            "我判断这是一个复杂任务。第一阶段先给出执行计划，不自动改文件或调用工具。",
            "",
            plan.summary,
            "",
            "建议步骤：",
        ]
        lines.extend(f"{index}. {step}" for index, step in enumerate(plan.steps, start=1))
        return {"final_response": "\n".join(lines), "plan_status": "pending"}

    def _finalize(self, state: AgentState) -> AgentState:
        existing = state.get("model_response")
        if existing is not None and state.get("route") == "simple_chat":
            return {
                "debug_trace": self._debug_trace(
                    state,
                    stage="MainAgent.finalize",
                    agent="MainAgent",
                    output={"preserved_model_response": True},
                    status=state.get("status", "completed"),
                    route=state.get("route"),
                )
            }

        content = state.get("final_response", "").strip()
        if not content:
            content = "任务已完成分析，但没有生成可展示结果。"
        trace = self._debug_trace(
            state,
            stage="MainAgent.finalize",
            agent="MainAgent",
            output=content,
            status=state.get("status", "completed"),
            route=state.get("route"),
        )
        metadata = self._message_metadata({**state, "debug_trace": trace})
        message = _with_agent_metadata(AIMessage(content=content), metadata)
        selected = self._selected_model(state.get("selection"))
        return {
            "model_response": ModelResponse(
                content=content,
                message=message,
                provider=selected.provider,
                model=selected.model,
                raw_content=content,
            ),
            "debug_trace": trace,
        }

    def _rule_analysis(self, text: str) -> TaskAnalysis | None:
        text = self._normalize_user_input(text)
        if self._is_weather_request(text) and not self._extract_location(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="needs_info",
                task_kind="tool",
                route_hint="clarify",
                risk_level="low",
                requires_confirmation=False,
                confidence=0.82,
                reason="天气查询需要明确地点，才能委派搜索 Agent 获取实时信息。",
                missing_info=["天气查询地点。"],
                clarification_questions=[
                    ClarificationQuestion(
                        id="location",
                        question="你想查询哪个城市或地区的天气？",
                        options=["北京", "上海", "广州", "深圳"],
                    )
                ],
            )
        if self._has_missing_target(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="needs_info",
                task_kind="unknown",
                route_hint="clarify",
                risk_level="medium",
                requires_confirmation=True,
                confidence=0.82,
                reason="用户提到了待处理对象，但没有给出明确路径或范围。",
                missing_info=[
                    "要处理的具体文件、目录或对象路径。",
                    "希望执行的具体动作。",
                    "期望的输出形式或完成标准。",
                ],
                clarification_questions=[
                    ClarificationQuestion(
                        id="target",
                        question="要处理的具体文件、目录或对象是什么？",
                        options=["当前选中文件", "workspace 中的某个文件", "整个 workspace"],
                    ),
                    ClarificationQuestion(
                        id="action",
                        question="希望执行什么具体动作？",
                        options=["分析并总结", "修改内容", "转换格式", "检查问题"],
                    ),
                    ClarificationQuestion(
                        id="output",
                        question="期望输出是什么形式？",
                        options=["直接回复结论", "生成 Markdown 文档", "修改原文件", "创建新文件"],
                    ),
                ],
            )
        if self._looks_high_risk_tool_task(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="complex",
                task_kind="tool",
                route_hint="future_task",
                tool_intents=[],
                estimated_steps=3,
                risk_level="high",
                requires_confirmation=True,
                confidence=0.84,
                reason="任务涉及写入、删除、重命名、上传下载等高风险工具操作，需要计划确认。",
                suggested_steps=[
                    "确认操作目标、范围和预期结果。",
                    "读取相关文件或目录现状，生成拟执行方案。",
                    "在真正写入、删除或移动前请求用户确认。",
                ],
            )
        if self._is_complex_research_task(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="complex",
                task_kind="task",
                route_hint="future_task",
                tool_intents=["workspace_search", "web_search"],
                estimated_steps=4,
                risk_level="medium",
                requires_confirmation=True,
                confidence=0.8,
                reason="任务涉及调研、整理或对比，通常需要多步骤搜索和汇总，应先确认计划。",
                suggested_steps=[
                    "确认调研目标、范围和输出格式。",
                    "选择 workspace 搜索或联网搜索来源。",
                    "汇总搜索结果并按主题整理。",
                    "输出对比结论、来源和未覆盖风险。",
                ],
            )
        tool_intents = self._simple_tool_intents(text)
        if tool_intents:
            return TaskAnalysis(
                intent=text[:80],
                complexity="simple",
                task_kind="tool",
                route_hint="simple_task",
                tool_intents=tool_intents,
                estimated_steps=len(tool_intents),
                risk_level="low",
                requires_confirmation=False,
                confidence=0.86,
                reason="这是目标明确、低风险、可由工具直接完成的简单任务。",
            )
        if self._is_explicit_project_report(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="complex",
                task_kind="task",
                route_hint="future_task",
                estimated_steps=5,
                risk_level="medium",
                requires_confirmation=True,
                confidence=0.78,
                reason="任务涉及项目、代码、文件或多步骤执行，需要任务型 Agent 承接。",
                suggested_steps=self._default_plan_steps(text),
            )
        if self._looks_complex(text):
            return None
        if self._looks_simple(text):
            return TaskAnalysis(
                intent=text[:80],
                complexity="simple",
                task_kind="chat",
                route_hint="simple_chat",
                confidence=0.75,
                reason="普通问答、解释、闲聊或简单文本生成。",
            )
        return None

    def _llm_analysis(
        self,
        text: str,
        selection: ModelSelection | None,
        current_time: str = "",
    ) -> TaskAnalysis:
        if self.models is None:
            raise RuntimeError("Model manager is not initialized")
        prompt = (
            "请判断用户任务复杂度，只返回 JSON，不要解释。\n"
            "字段：intent, complexity, task_kind, route_hint, tool_intents, estimated_steps, risk_level, requires_confirmation, confidence, reason, missing_info, suggested_steps, clarification_questions。\n"
            "complexity 只能是 simple、needs_info、complex。\n"
            "route_hint 只能是 simple_chat、simple_task、clarify、future_task 或空。\n"
            "task_kind 只能是 chat、tool、task、unknown；risk_level 只能是 low、medium、high。\n"
            "simple: 普通问答、解释、闲聊、简单文本生成。\n"
            "simple_task: 目标明确、低风险、可直接使用工具完成，例如列文件、读明确路径文件、搜索文件、查看文件信息。\n"
            "联网查一下、搜索 workspace 中的明确关键词属于 simple_task；调研并整理对比属于 complex。\n"
            "needs_info: 缺少目标文件、范围、输出格式或确认条件。\n"
            "complex: 多步骤、项目/代码/文件操作、需要执行或长期跟踪。\n"
            "写入、删除、重命名、上传下载、命令执行必须视为 high risk 且 requires_confirmation=true。\n"
            "如果 complexity=needs_info，clarification_questions 必须是数组；每项包含 id、question、options、allow_custom、required。\n"
            "每个 options 给 2-4 个简短候选项；不能确定候选项时 options 为空且 allow_custom=true。\n"
            f"用户输入：{text}"
        )
        if current_time:
            prompt = f"{prompt}\n\n{current_time}"
        result = self.models.chat(
            [
                SystemMessage(content="你是主 Agent 的任务解析器。"),
                HumanMessage(content=prompt),
            ],
            selection=selection,
            options=GenerationOptions(temperature=0, max_tokens=512),
        )
        data = _extract_json(result.content)
        return TaskAnalysis(**data)

    def _llm_plan(self, state: AgentState) -> TaskPlan:
        if self.models is None:
            raise RuntimeError("Model manager is not initialized")
        answers = state.get("clarification_answers") or {}
        feedback = state.get("plan_feedback") or ""
        answer_lines = "\n".join(
            f"- {key}: {value}" for key, value in answers.items() if value
        ) or "无"
        feedback_text = feedback or "无"
        prompt = (
            "请为用户任务生成一个需要人工确认的执行计划，只返回 JSON，不要解释。\n"
            "字段：summary, steps, risks, requires_confirmation。\n"
            "summary 是一句话目标摘要；steps 是 3-6 个可执行步骤；risks 是 1-4 个风险或确认点；requires_confirmation 固定为 true。\n"
            "第一阶段只生成计划，不要声称已经执行、修改文件或调用工具。\n\n"
            f"原始任务：{state['user_input']}\n\n"
            f"补充信息：\n{answer_lines}\n\n"
            f"用户对上一版计划的修改意见：{feedback_text}"
        )
        current_time = state.get("current_time_context", "")
        if current_time:
            prompt = f"{prompt}\n\n{current_time}"
        result = self.models.chat(
            [
                SystemMessage(content="你是主 Agent 的任务规划器。"),
                HumanMessage(content=prompt),
            ],
            selection=state.get("selection"),
            options=GenerationOptions(temperature=0.2, max_tokens=900),
        )
        data = _extract_json(result.content)
        plan = TaskPlan(**data)
        if not plan.steps:
            plan.steps = self._default_plan_steps(state["user_input"])
        if not plan.summary:
            plan.summary = f"完成用户提出的任务：{state['user_input'][:80]}"
        return plan

    def _has_missing_target(self, text: str) -> bool:
        if PATH_HINT.search(text):
            return False
        return (
            any(ref in text for ref in MISSING_TARGET_REFS)
            and any(action in text for action in MISSING_TARGET_ACTIONS)
        )

    def _simple_tool_intents(self, text: str) -> list[str]:
        text = self._normalize_user_input(text)
        intents: list[str] = []
        has_path = PATH_HINT.search(text) is not None
        if self._is_file_list_request(text):
            intents.append("list_workspace_tree")
        if has_path and any(marker in text for marker in SIMPLE_FILE_INFO_MARKERS):
            intents.append("file_info")
        elif has_path and any(marker in text for marker in SIMPLE_FILE_READ_MARKERS):
            intents.append("read_file")
        if self._is_web_search_request(text):
            intents.append("web_search")
        if self._is_file_search_request(text):
            intents.append("workspace_search")
        return intents

    def _is_file_list_request(self, text: str) -> bool:
        if any(marker in text for marker in SIMPLE_FILE_LIST_MARKERS):
            return True
        has_scope = any(marker in text for marker in SIMPLE_FILE_SCOPE_MARKERS)
        return has_scope and "文件" in text and any(marker in text for marker in ("哪些", "有什么", "列表", "列出"))

    def _is_file_search_request(self, text: str) -> bool:
        if self._is_web_search_request(text):
            return False
        if not any(marker in text for marker in SIMPLE_FILE_SEARCH_MARKERS):
            return False
        cleaned = text
        for marker in SIMPLE_FILE_SEARCH_MARKERS + ("文件", "内容", "workspace", "中", "的", "一下", "包含"):
            cleaned = cleaned.replace(marker, " ")
        return bool(cleaned.strip(" ，,。；;：:"))

    def _is_web_search_request(self, text: str) -> bool:
        text = self._normalize_user_input(text)
        if self._requires_fresh_external_info(text) and any(marker in text for marker in SIMPLE_FILE_SEARCH_MARKERS + ("查询", "查一下", "搜一下", "搜索", "怎么样")):
            return True
        if not any(marker in text for marker in WEB_SEARCH_MARKERS):
            return False
        return any(marker in text for marker in SIMPLE_FILE_SEARCH_MARKERS + ("查询", "查一下", "搜一下", "搜索"))

    def _is_complex_research_task(self, text: str) -> bool:
        return (
            any(marker in text for marker in RESEARCH_MARKERS)
            and any(marker in text for marker in RESEARCH_COMPLEX_MARKERS)
        )

    def _looks_high_risk_tool_task(self, text: str) -> bool:
        if not any(marker in text for marker in HIGH_RISK_TOOL_MARKERS):
            return False
        return bool(
            PATH_HINT.search(text)
            or any(marker in text for marker in SIMPLE_FILE_SCOPE_MARKERS)
            or any(marker in text for marker in ("文件", "文档", "目录", "workspace"))
        )

    def _looks_complex(self, text: str) -> bool:
        lower = text.lower()
        return any(marker in lower or marker in text for marker in COMPLEX_MARKERS)

    def _is_explicit_project_report(self, text: str) -> bool:
        return (
            any(marker in text for marker in ("整个项目", "当前项目", "本项目", "项目分析"))
            and any(marker in text for marker in ("报告", "分析", "整理"))
        )

    def _looks_simple(self, text: str) -> bool:
        normalized = text.strip().lower()
        if normalized in {"hi", "hello", "你好", "在吗", "早上好", "晚上好"}:
            return True
        simple_prefixes = (
            "解释",
            "说明",
            "什么是",
            "请问",
            "帮我写",
            "改写",
            "翻译",
            "总结这段",
        )
        if text.startswith(simple_prefixes):
            return True
        return len(text) <= 160

    def _default_plan_steps(self, text: str) -> list[str]:
        return [
            "复述并确认任务目标、输入范围和交付结果。",
            "拆解任务步骤，标记需要文件、工具或外部信息的位置。",
            "执行前请求用户确认关键操作，尤其是写文件、删除和长时间运行命令。",
            "分步骤执行并记录中间结果、错误和下一步。",
            "汇总最终结果，说明修改内容、验证方式和未完成风险。",
        ]

    def _clarification_questions(self, analysis: TaskAnalysis) -> list[ClarificationQuestion]:
        if analysis.complexity != "needs_info":
            return []
        if analysis.clarification_questions:
            return [
                self._normalize_question(index, item)
                for index, item in enumerate(analysis.clarification_questions, start=1)
            ]
        missing = analysis.missing_info or ["请补充目标对象、处理范围和期望输出。"]
        return [
            ClarificationQuestion(
                id=f"q{index}",
                question=item,
                options=[],
                allow_custom=True,
                required=True,
            )
            for index, item in enumerate(missing, start=1)
        ]

    def _normalize_question(
        self,
        index: int,
        question: ClarificationQuestion,
    ) -> ClarificationQuestion:
        text = question.question.strip() or f"请补充第 {index} 项信息。"
        options = [item.strip() for item in question.options if item.strip()][:4]
        return ClarificationQuestion(
            id=question.id.strip() or f"q{index}",
            question=text,
            options=options,
            allow_custom=question.allow_custom,
            required=question.required,
        )

    def _plan_from_state(self, state: AgentState) -> TaskPlan:
        existing = state.get("task_plan")
        if isinstance(existing, TaskPlan):
            return existing
        return TaskPlan(
            summary=state.get("plan_summary", "") or f"完成用户提出的任务：{state['user_input'][:80]}",
            steps=state.get("plan_steps") or self._default_plan_steps(state["user_input"]),
            risks=state.get("plan_risks") or ["第一阶段只确认计划，不自动执行任务。"],
            requires_confirmation=True,
        )

    def _interrupt_metadata(self, interrupt_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        interrupt_type = str(payload.get("type") or "clarification")
        metadata: dict[str, Any] = {
            "id": interrupt_id,
            "type": interrupt_type,
            "message": str(payload.get("message") or "任务已暂停，等待你的确认。"),
            "clarification": None,
            "plan": None,
        }
        if interrupt_type == "clarification":
            metadata["clarification"] = payload.get("clarification")
        elif interrupt_type == "plan_confirmation":
            metadata["plan"] = payload.get("plan")
        return metadata

    def _resume_summary(self, interrupt_type: str | None, payload: dict[str, Any]) -> str:
        if interrupt_type == "clarification":
            answers = payload.get("answers") or {}
            lines = ["补充信息："]
            for index, (key, value) in enumerate(answers.items(), start=1):
                lines.append(f"{index}. {key}: {value}")
            return "\n".join(lines)
        if interrupt_type == "plan_confirmation":
            decision = payload.get("decision")
            feedback = payload.get("feedback") or ""
            if decision == "approve":
                return "确认计划，继续后续流程。"
            if decision == "cancel":
                return "取消当前任务。"
            return f"请根据以下意见修改计划：\n{feedback}"
        return "继续当前任务。"

    def _message_metadata(self, state: AgentState) -> dict[str, Any]:
        analysis = state.get("task_analysis")
        route = state.get("route")
        metadata: dict[str, Any] = {
            "created_at": isoformat(),
            "status": state.get("status", "completed"),
            "interrupt": state.get("interrupt"),
            "plan_status": state.get("plan_status"),
            "task": {"status": state.get("task_status")} if state.get("task_status") else None,
            "steps": state.get("task_steps"),
            "tool_calls": state.get("tool_calls"),
            "debug_trace": state.get("debug_trace"),
            "task_brief": state.get("task_brief").model_dump() if hasattr(state.get("task_brief"), "model_dump") else state.get("task_brief"),
        }
        metadata["agent_flow"] = self._build_agent_flow(state)
        if route:
            metadata["route"] = route
        if analysis:
            metadata["complexity"] = analysis.complexity
        if route == "clarify":
            metadata["clarification"] = {
                "original_message": state["user_input"],
                "questions": [
                    question.model_dump()
                    for question in state.get("clarification_questions", [])
                ],
            }
        if route == "future_task":
            metadata["plan_steps"] = state.get("plan_steps", [])
            plan = self._plan_from_state(state) if state.get("plan_steps") else None
            if plan:
                metadata["plan"] = plan.model_dump()
        return metadata

    def _with_message_metadata(self, result: ModelResponse, state: AgentState) -> ModelResponse:
        metadata = self._message_metadata(state)
        message = _with_agent_metadata(result.message, metadata)
        return ModelResponse(
            content=result.content,
            message=message,
            provider=result.provider,
            model=result.model,
            reasoning=result.reasoning,
            raw_content=result.raw_content,
        )

    def _record_history(self, user_record: str, assistant_message: AIMessage) -> None:
        user_message = _with_agent_metadata(HumanMessage(content=user_record), {"created_at": isoformat()})
        self.history.add_message(user_message)
        self.history.add_message(assistant_message)
        self._trim_history()

    def _selected_model(self, selection: ModelSelection | None) -> ModelSelection:
        if self.models is None:
            raise RuntimeError("Model manager is not initialized")
        return selection or self.models.default_selection

    def _trim_history(self) -> None:
        if len(self.history.messages) > self.config.max_history_messages:
            self.history.messages = self.history.messages[-self.config.max_history_messages:]


class _LinearAgentGraph:
    """Small boot fallback used only before dependencies are installed."""

    def __init__(self, agent: MainAgent) -> None:
        self.agent = agent

    def invoke(self, initial_state: AgentState, config: dict[str, Any] | None = None) -> AgentState:
        state: AgentState = dict(initial_state)
        state.update(self.agent._analyze_task(state))
        state.update(self.agent._route_task(state))
        route = self.agent._route_from_state(state)
        if route == "clarify":
            state.update(self.agent._clarify_fallback(state))
        elif route == "simple_task":
            state.update(self.agent._simple_task(state))
        elif route == "future_task":
            state.update(self.agent._generate_plan(state))
            state.update(self.agent._plan_fallback(state))
        else:
            state.update(self.agent._simple_chat(state))
        state.update(self.agent._finalize(state))
        return state


def _extract_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("structured analysis must be a JSON object")
    return parsed
