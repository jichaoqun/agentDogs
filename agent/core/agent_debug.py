"""Debug trace and layered agent-flow formatting for MainAgent."""

from __future__ import annotations

from typing import Any

from .state import AgentState

MAX_DEBUG_TEXT = 900
MAX_DEBUG_EVENTS = 80


class AgentDebugMixin:
    """Build compact debug events and frontend agent_flow payloads."""

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
                    "step_type": step.get("step_type"),
                    "step_index": step.get("index"),
                },
            )
            payload["stepIndex"] = step.get("index")
            payload["stepType"] = step.get("step_type")
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
            "stage": event.get("stage"),
            "stepIndex": event.get("step_index"),
            "stepType": event.get("step_type"),
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

