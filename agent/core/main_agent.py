"""Main agent orchestration for conversation and first-stage task routing."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any
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
from .state import AgentState, ClarificationQuestion, Route, TaskAnalysis, TaskPlan
from .tools import ToolRegistry, create_default_tool_registry
from .utils.llm_config import AppConfig
from .utils.llm_models import (
    GenerationOptions,
    ModelManager,
    ModelResponse,
    ModelSelection,
)


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
HIGH_RISK_TOOL_MARKERS = ("写入", "保存", "修改", "改写", "删除", "重命名", "创建", "新增", "覆盖", "移动", "上传", "下载")


class AgentInterruptError(RuntimeError):
    """Raised when a pending LangGraph interrupt cannot be resumed safely."""


@dataclass(slots=True)
class MainAgent:
    config: AppConfig
    models: ModelManager | None = None
    tool_registry: ToolRegistry | None = None
    history: InMemoryChatMessageHistory = field(default_factory=InMemoryChatMessageHistory)
    simple_chat_agent: SimpleChatAgent = field(init=False, repr=False)
    simple_task_agent: SimpleTaskAgent = field(init=False, repr=False)
    sub_agent_registry: SubAgentRegistry = field(init=False, repr=False)
    task_agent: TaskAgent = field(init=False, repr=False)
    last_state: AgentState | None = field(default=None, init=False, repr=False)
    pending_interrupt: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _default_thread_id: str = field(default_factory=lambda: f"agent-{uuid4()}", init=False, repr=False)
    _graph: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        models = self.models or ModelManager(self.config)
        self.models = models
        self.tool_registry = self.tool_registry or create_default_tool_registry()
        self.simple_chat_agent = SimpleChatAgent(self.config, models, self.history)
        self.sub_agent_registry = create_default_sub_agent_registry(self.tool_registry, self.simple_chat_agent)
        self.simple_task_agent = self.sub_agent_registry.get("simple_task").agent
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
    ) -> ModelResponse:
        if self.pending_interrupt is not None:
            raise AgentInterruptError("当前任务正在等待补充或确认，请先处理当前弹窗。")
        text = user_input.strip()
        if not text:
            raise ValueError("消息不能为空")
        if thinking_enabled is not None and options is None:
            options = GenerationOptions(thinking_enabled=thinking_enabled)

        initial_state: AgentState = {
            "messages": list(self.history.messages),
            "user_input": text,
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
        }
        result_state = self._graph.invoke(initial_state, self._thread_config(thread_id))
        return self._handle_graph_result(
            result_state,
            user_record=text,
        )

    def resume(
        self,
        interrupt_id: str,
        payload: dict[str, Any],
        *,
        thread_id: str | None = None,
    ) -> ModelResponse:
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
        result_state = self._graph.invoke(
            Command(resume=resume_payload),
            self._thread_config(thread_id),
        )
        return self._handle_graph_result(
            result_state,
            user_record=self._resume_summary(expected_type, resume_payload),
        )

    def clear(self) -> None:
        self.history.clear()
        self.last_state = None
        self.pending_interrupt = None

    def has_pending_interrupt(self) -> bool:
        return self.pending_interrupt is not None

    def response_metadata(self) -> dict[str, Any]:
        return self._message_metadata(self.last_state or {})

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
    ) -> ModelResponse:
        interrupts = result_state.get("__interrupt__") or []
        if interrupts:
            return self._handle_interrupt(result_state, interrupts[0], user_record=user_record)

        self.pending_interrupt = None
        result_state["status"] = "completed"
        self.last_state = result_state
        result = result_state.get("model_response")
        if result is None:
            raise RuntimeError("Agent graph completed without a model response")

        if user_record and result_state.get("route") != "simple_chat":
            self.history.add_user_message(user_record)
            self.history.add_message(result.message)
            self._trim_history()
        return result

    def _handle_interrupt(
        self,
        result_state: AgentState,
        interrupt_obj: Any,
        *,
        user_record: str | None,
    ) -> ModelResponse:
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

        message = AIMessage(content=content, additional_kwargs=self._message_metadata(state))
        if user_record:
            self.history.add_user_message(user_record)
        self.history.add_message(message)
        self._trim_history()
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
                analysis = self._llm_analysis(text, state.get("selection"))
        except Exception as exc:  # Keep routing available when judgment fails.
            errors.append(f"analyze_task: {exc}")
            analysis = TaskAnalysis(
                intent=text[:80],
                complexity="simple",
                confidence=0.3,
                reason="任务解析失败，回退为普通对话。",
            )
        return {
            "task_analysis": analysis,
            "missing_info": analysis.missing_info,
            "clarification_questions": self._clarification_questions(analysis),
            "plan_steps": analysis.suggested_steps,
            "errors": errors,
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
        return {"route": route}

    def _route_from_state(self, state: AgentState) -> Route:
        return state.get("route", "simple_chat")

    def _route_after_plan_confirmation(self, state: AgentState) -> str:
        if state.get("plan_decision") == "revise":
            return "revise"
        if state.get("plan_decision") == "approve":
            return "execute_task"
        return "finalize"

    def _simple_chat(self, state: AgentState) -> AgentState:
        result = self.simple_chat_agent.chat(
            state["user_input"],
            selection=state.get("selection"),
            options=state.get("options"),
        )
        return {
            "model_response": result,
            "final_response": result.content,
        }

    def _simple_task(self, state: AgentState) -> AgentState:
        result = self.simple_task_agent.handle(state["user_input"])
        status = result.status if result.ok else "failed"
        return {
            "task_status": status,
            "tool_calls": result.tool_calls,
            "final_response": result.content,
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
        return {
            "task_status": result.status,
            "task_steps": [item.as_dict() for item in result.steps],
            "tool_calls": result.tool_calls,
            "final_response": "\n".join(lines),
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
            return {}

        content = state.get("final_response", "").strip()
        if not content:
            content = "任务已完成分析，但没有生成可展示结果。"
        message = AIMessage(content=content, additional_kwargs=self._message_metadata(state))
        selected = self._selected_model(state.get("selection"))
        return {
            "model_response": ModelResponse(
                content=content,
                message=message,
                provider=selected.provider,
                model=selected.model,
                raw_content=content,
            )
        }

    def _rule_analysis(self, text: str) -> TaskAnalysis | None:
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

    def _llm_analysis(self, text: str, selection: ModelSelection | None) -> TaskAnalysis:
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
            "needs_info: 缺少目标文件、范围、输出格式或确认条件。\n"
            "complex: 多步骤、项目/代码/文件操作、需要执行或长期跟踪。\n"
            "写入、删除、重命名、上传下载、命令执行必须视为 high risk 且 requires_confirmation=true。\n"
            "如果 complexity=needs_info，clarification_questions 必须是数组；每项包含 id、question、options、allow_custom、required。\n"
            "每个 options 给 2-4 个简短候选项；不能确定候选项时 options 为空且 allow_custom=true。\n"
            f"用户输入：{text}"
        )
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
        intents: list[str] = []
        has_path = PATH_HINT.search(text) is not None
        if self._is_file_list_request(text):
            intents.append("list_workspace_tree")
        if has_path and any(marker in text for marker in SIMPLE_FILE_INFO_MARKERS):
            intents.append("file_info")
        elif has_path and any(marker in text for marker in SIMPLE_FILE_READ_MARKERS):
            intents.append("read_file")
        if self._is_file_search_request(text):
            intents.append("search_files")
        return intents

    def _is_file_list_request(self, text: str) -> bool:
        if any(marker in text for marker in SIMPLE_FILE_LIST_MARKERS):
            return True
        has_scope = any(marker in text for marker in SIMPLE_FILE_SCOPE_MARKERS)
        return has_scope and "文件" in text and any(marker in text for marker in ("哪些", "有什么", "列表", "列出"))

    def _is_file_search_request(self, text: str) -> bool:
        if not any(marker in text for marker in SIMPLE_FILE_SEARCH_MARKERS):
            return False
        cleaned = text
        for marker in SIMPLE_FILE_SEARCH_MARKERS + ("文件", "内容", "workspace", "中", "的", "一下", "包含"):
            cleaned = cleaned.replace(marker, " ")
        return bool(cleaned.strip(" ，,。；;：:"))

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
            "status": state.get("status", "completed"),
            "interrupt": state.get("interrupt"),
            "plan_status": state.get("plan_status"),
            "task": {"status": state.get("task_status")} if state.get("task_status") else None,
            "steps": state.get("task_steps"),
            "tool_calls": state.get("tool_calls"),
        }
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
