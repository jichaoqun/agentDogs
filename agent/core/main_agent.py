"""Main agent orchestration for conversation and first-stage task routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import AIMessage

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

from .agent_debug import AgentDebugMixin
from .agent_metadata import AGENT_METADATA_KEY, AgentMetadataMixin, _with_agent_metadata
from .agent_responses import AgentResponseSynthesizerMixin
from .agent_routing import AgentRoutingMixin
from .sub_agents import SimpleChatAgent, SimpleTaskAgent, SubAgentRegistry, TaskAgent, create_default_sub_agent_registry
from .state import AgentState, Route, TaskAnalysis, TaskBrief, TaskPlan
from .tools import ToolRegistry, create_default_tool_registry
from .utils.llm_config import AppConfig
from .utils.llm_models import (
    GenerationOptions,
    ModelManager,
    ModelResponse,
    ModelSelection,
)
from .utils.time_utils import current_time_context, now_local


class AgentInterruptError(RuntimeError):
    """Raised when a pending LangGraph interrupt cannot be resumed safely."""


class AgentRunCancelled(RuntimeError):
    """Raised when the current agent run was cancelled by the user."""


@dataclass(slots=True)
class MainAgent(AgentRoutingMixin, AgentDebugMixin, AgentResponseSynthesizerMixin, AgentMetadataMixin):
    config: AppConfig
    models: ModelManager | None = None
    tool_registry: ToolRegistry | None = None
    history: InMemoryChatMessageHistory = field(default_factory=InMemoryChatMessageHistory)
    simple_chat_agent: SimpleChatAgent = field(init=False, repr=False)
    simple_task_agent: SimpleTaskAgent = field(init=False, repr=False)
    search_agent: Any = field(init=False, repr=False)
    file_agent: Any = field(init=False, repr=False)
    code_agent: Any = field(init=False, repr=False)
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
        self.sub_agent_registry = create_default_sub_agent_registry(
            self.tool_registry,
            self.simple_chat_agent,
            self.config.code_execution,
        )
        self.simple_task_agent = self.sub_agent_registry.get("simple_task").agent
        self.search_agent = self.sub_agent_registry.get("search_agent").agent
        self.file_agent = self.sub_agent_registry.get("file_agent").agent
        self.code_agent = self.sub_agent_registry.get("code_agent").agent
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
        if interrupt_type == "workspace_confirmation":
            decision = str(payload.get("decision") or "")
            if decision not in {"approve", "cancel"}:
                raise AgentInterruptError("workspace 写入确认操作不正确。")
            return {
                "type": "workspace_confirmation",
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
        graph.add_node("workspace_confirm_interrupt", self._workspace_confirm_interrupt)
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
        graph.add_conditional_edges(
            "execute_task",
            self._route_after_execute_task,
            {
                "workspace_confirm": "workspace_confirm_interrupt",
                "finalize": "finalize",
            },
        )
        graph.add_edge("workspace_confirm_interrupt", "finalize")
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
        if route == "simple_chat" and self._is_file_execution_request(state["user_input"]):
            route = "future_task" if self._is_code_workspace_write_task(state["user_input"]) else "simple_task"
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

    def _route_after_execute_task(self, state: AgentState) -> str:
        if state.get("pending_confirmations"):
            return "workspace_confirm"
        return "finalize"

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
        elif isinstance(brief, TaskBrief) and brief.delegate_to == "code_agent":
            result = self.code_agent.handle_brief(brief)
            agent_name = "CodeAgent"
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
        result = self.task_agent.execute(
            user_input=state["user_input"],
            plan_steps=steps,
            task_brief=state.get("task_brief") if isinstance(state.get("task_brief"), TaskBrief) else None,
        )
        lines = [
            "计划已确认，TaskAgent 已完成第一阶段执行。",
            "",
            result.summary,
        ]
        completed_steps = [step for step in result.steps if step.status == "completed"]
        failed_steps = [step for step in result.steps if step.status == "failed"]
        waiting_steps = [step for step in result.steps if step.status == "waiting_confirmation"]
        if completed_steps:
            lines.extend(["", "已完成："])
            for step in completed_steps:
                detail = step.result or "已完成"
                lines.append(f"{step.index}. {step.title}\n   {detail}")
        if result.artifacts:
            lines.extend(["", "生成的 artifacts："])
            for item in result.artifacts[:8]:
                if isinstance(item, dict):
                    lines.append(f"- {item.get('filename') or item.get('path')} ({item.get('url') or item.get('path')})")
        if waiting_steps:
            lines.extend(["", "等待二次确认："])
            for step in waiting_steps:
                detail = step.result or "该步骤需要二次确认。"
                lines.append(f"{step.index}. {step.title}\n   {detail}")
        if failed_steps:
            lines.extend(["", "失败或未完成："])
            for step in failed_steps:
                detail = step.error or step.result or "无错误详情"
                lines.append(f"{step.index}. {step.title}\n   {detail}")
        if result.pending_confirmations:
            lines.extend(["", "下一步：请确认是否执行上述 workspace 写入动作。"])
        for step in result.steps:
            label = {
                "completed": "完成",
                "failed": "失败",
                "waiting_confirmation": "等待确认",
                "running": "运行中",
                "pending": "待处理",
            }.get(step.status, step.status)
            detail = step.result or step.error or "无结果"
            if not (completed_steps or waiting_steps or failed_steps):
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
        trace.extend(result.debug_events)
        trace = trace[-80:]
        trace = self._tool_debug_events({"debug_trace": trace}, result.tool_calls)
        return {
            "task_status": result.status,
            "task_steps": [item.as_dict() for item in result.steps],
            "tool_calls": result.tool_calls,
            "pending_confirmations": result.pending_confirmations,
            "final_response": "\n".join(lines),
            "debug_trace": trace,
        }

    def _workspace_confirm_interrupt(self, state: AgentState) -> AgentState:
        confirmations = state.get("pending_confirmations") or []
        if not confirmations:
            return {}
        if interrupt is None:
            return {"final_response": f"{state.get('final_response', '')}\n\nworkspace 写入动作仍在等待人工确认。"}
        base_message = state.get("final_response", "").strip()
        prompt_message = "还有 workspace 写入动作需要二次确认。请确认后再执行创建目录或发布 artifacts。"
        payload = {
            "type": "workspace_confirmation",
            "message": f"{base_message}\n\n{prompt_message}" if base_message else prompt_message,
            "workspace_confirmation": {
                "original_message": state["user_input"],
                "confirmations": confirmations,
                "artifacts": self._confirmation_artifacts(confirmations),
            },
        }
        answer = interrupt(payload)
        decision = answer.get("decision", "cancel") if isinstance(answer, dict) else "cancel"
        if decision != "approve":
            return {
                "task_status": state.get("task_status", "waiting_confirmation"),
                "final_response": f"{state.get('final_response', '').strip()}\n\n已取消 workspace 写入动作，未创建目录或发布 artifacts。",
                "pending_confirmations": confirmations,
            }

        write_result = self._execute_workspace_confirmations(confirmations)
        tool_calls = [*(state.get("tool_calls") or []), *write_result["tool_calls"]]
        trace = self._tool_debug_events({"debug_trace": state.get("debug_trace") or []}, write_result["tool_calls"])
        status = "completed" if write_result["ok"] else "failed"
        lines = [
            state.get("final_response", "").strip(),
            "",
            "workspace 写入确认已执行：",
            *write_result["lines"],
        ]
        return {
            "task_status": status,
            "tool_calls": tool_calls,
            "pending_confirmations": [],
            "final_response": "\n".join(line for line in lines if line is not None),
            "debug_trace": trace,
        }

    def _confirmation_artifacts(self, confirmations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in confirmations:
            for artifact in item.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                key = str(artifact.get("path") or artifact.get("url") or artifact.get("filename"))
                if key and key not in seen:
                    seen.add(key)
                    artifacts.append(artifact)
        return artifacts

    def _execute_workspace_confirmations(self, confirmations: list[dict[str, Any]]) -> dict[str, Any]:
        lines: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        ok = True
        created_dirs: set[str] = set()
        for item in confirmations:
            action = str(item.get("action") or "")
            target_dir = str(item.get("target_directory") or "").strip()
            if action == "create_directory":
                if not target_dir:
                    ok = False
                    lines.append("- 创建目录失败：缺少目标目录。")
                    continue
                result = self.tool_registry.call("create_directory", {"path": target_dir}) if self.tool_registry else None
                call = {"tool": "create_directory", "payload": {"path": target_dir}, "ok": bool(result and result.ok), "error": getattr(result, "error", None)}
                tool_calls.append(call)
                if result and result.ok:
                    created_dirs.add(target_dir)
                    lines.append(f"- 已创建或确认目录存在：{target_dir}")
                else:
                    ok = False
                    lines.append(f"- 创建目录失败：{target_dir}，{getattr(result, 'error', '工具不可用')}")
            elif action == "publish_artifacts":
                artifacts = [artifact for artifact in item.get("artifacts") or [] if isinstance(artifact, dict)]
                if not target_dir:
                    ok = False
                    lines.append("- 发布 artifacts 失败：缺少目标目录。")
                    continue
                if target_dir not in created_dirs:
                    mkdir = self.tool_registry.call("create_directory", {"path": target_dir}) if self.tool_registry else None
                    tool_calls.append({"tool": "create_directory", "payload": {"path": target_dir}, "ok": bool(mkdir and mkdir.ok), "error": getattr(mkdir, "error", None)})
                    if mkdir and mkdir.ok:
                        created_dirs.add(target_dir)
                    else:
                        ok = False
                        lines.append(f"- 发布前创建目录失败：{target_dir}，{getattr(mkdir, 'error', '工具不可用')}")
                        continue
                if not artifacts:
                    ok = False
                    lines.append(f"- 没有可发布的 artifacts，因此未向 {target_dir} 写入图表文件。请先解决 CodeAgent/OpenSandbox 执行失败。")
                    continue
                for artifact in artifacts:
                    source = str(artifact.get("path") or "").strip()
                    filename = str(artifact.get("filename") or source.rsplit("/", 1)[-1]).strip()
                    target = f"{target_dir.rstrip('/')}/{filename}" if filename else target_dir
                    result = self.tool_registry.call("publish_artifact", {"source": source, "target": target}) if self.tool_registry else None
                    call = {"tool": "publish_artifact", "payload": {"source": source, "target": target}, "ok": bool(result and result.ok), "error": getattr(result, "error", None)}
                    tool_calls.append(call)
                    if result and result.ok:
                        lines.append(f"- 已发布 artifact：{target}")
                    else:
                        ok = False
                        lines.append(f"- 发布 artifact 失败：{filename or source}，{getattr(result, 'error', '工具不可用')}")
        if not lines:
            lines.append("- 没有可执行的 workspace 写入动作。")
        return {"ok": ok, "lines": lines, "tool_calls": tool_calls}

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

    def _interrupt_metadata(self, interrupt_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        interrupt_type = str(payload.get("type") or "clarification")
        metadata: dict[str, Any] = {
            "id": interrupt_id,
            "type": interrupt_type,
            "message": str(payload.get("message") or "任务已暂停，等待你的确认。"),
            "clarification": None,
            "plan": None,
            "workspace_confirmation": None,
        }
        if interrupt_type == "clarification":
            metadata["clarification"] = payload.get("clarification")
        elif interrupt_type == "plan_confirmation":
            metadata["plan"] = payload.get("plan")
        elif interrupt_type == "workspace_confirmation":
            metadata["workspace_confirmation"] = payload.get("workspace_confirmation")
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
        if interrupt_type == "workspace_confirmation":
            decision = payload.get("decision")
            if decision == "approve":
                return "确认执行 workspace 写入动作。"
            return "取消 workspace 写入动作。"
        return "继续当前任务。"

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
