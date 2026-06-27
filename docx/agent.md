# Agent 技术设计

本文档记录当前 Agent Dogs 的后端 Agent 架构、任务路由、子 Agent 能力边界、调试数据结构和当前限制。

## 当前目标

项目已经从“MainAgent 简单路由 + 子 Agent 猜测执行”调整为“MainAgent 任务理解与委派 + 子 Agent 领域自治执行 + MainAgent 最终汇总输出”。

当前职责划分：

- `MainAgent`: 统一入口，理解用户输入、补全时间上下文、判断复杂度、生成 `TaskBrief`、选择子 Agent、汇总最终回答、维护调试信息。
- `SimpleChatAgent`: 纯对话和简单文本生成，不调用工具。
- `SearchAgent`: 处理 workspace、关键词和联网搜索任务，自己决定搜索 query、搜索源、筛选结果并返回结构化证据。
- `FileAgent`: 处理 workspace 文件读取、文件搜索、只读分析和摘要。
- `SimpleTaskAgent`: 兼容层，保留一步低风险工具任务能力，后续逐步弱化。
- `TaskAgent`: 复杂任务的第一阶段执行器，按计划步骤执行低风险只读任务，并记录每步状态。

## LLM 支持

模型层由 `agent/core/utils/llm_models.py` 统一封装，配置来自 `config/llm.yaml`。

支持的 Provider：

- `api`: OpenAI-compatible API。
- `ollama`: 本地 Ollama 服务。
- `builtin`: 项目内置本地模型。

模型选择、温度、最大输出长度和 thinking 参数由前端传给后端，最终以 `GenerationOptions` 传入模型层。模型调用不会在 Provider 之间静默 fallback；显式选择的 Provider 调用失败时直接返回错误。

## 主流程

LangGraph 节点流程：

```text
START
  -> analyze_task
  -> route_task
  -> simple_chat
  -> END

route_task
  -> simple_task
  -> finalize
  -> END

route_task
  -> clarify_interrupt
  -> generate_plan
  -> plan_confirm_interrupt
  -> execute_task / revise / cancel
  -> finalize
  -> END

route_task
  -> generate_plan
  -> plan_confirm_interrupt
  -> execute_task / revise / cancel
  -> finalize
  -> END
```

关键节点职责：

- `analyze_task`: 生成 `TaskAnalysis` 和 `TaskBrief`。
- `route_task`: 根据复杂度、风险、缺失信息和 route hint 选择路线。
- `simple_chat`: 调用 `SimpleChatAgent`。
- `simple_task`: 根据 `TaskBrief.delegate_to` 委派给 `SearchAgent`、`FileAgent` 或兼容层 `SimpleTaskAgent`。
- `MainAgent.synthesize_result`: 把子 Agent 的结构化结果汇总成最终用户回答。
- `clarify_interrupt`: 信息不足时暂停，返回结构化补充问题。
- `generate_plan`: 复杂任务生成计划。
- `plan_confirm_interrupt`: 等待用户确认、修改或取消计划。
- `execute_task`: 计划确认后进入 `TaskAgent` 第一阶段执行。
- `finalize`: 生成统一 `ModelResponse` 并附带 metadata。

## TaskAnalysis

`TaskAnalysis` 是主 Agent 对任务复杂度和风险的判断。

主要字段：

- `intent`: 用户意图摘要。
- `complexity`: `simple`、`needs_info`、`complex`。
- `task_kind`: `chat`、`tool`、`task`、`unknown`。
- `route_hint`: `simple_chat`、`simple_task`、`clarify`、`future_task`。
- `tool_intents`: 可能使用的工具意图。
- `risk_level`: `low`、`medium`、`high`。
- `requires_confirmation`: 是否需要人工确认。
- `missing_info`: 缺失信息。
- `clarification_questions`: 前端可展示的问题。

任务判断优先使用规则；规则无法确定时再调用 LLM 生成结构化 JSON。

## TaskBrief

`TaskBrief` 是 MainAgent 给子 Agent 的任务委派对象。它不是工具参数，而是任务说明。

字段：

- `intent`: 标准化意图，例如 `weather_lookup`、`search`。
- `user_goal`: 原始用户目标。
- `normalized_input`: 纠错和清洗后的输入，例如把“搜素”修正为“搜索”。
- `context`: 时间、地点、路径、搜索范围等上下文。
- `constraints`: 约束，例如需要新鲜外部信息、遇到高风险操作必须确认。
- `source_policy`: `not_required`、`workspace_only`、`requires_fresh_external_info`。
- `expected_output`: 期望输出形式。
- `delegate_to`: 目标子 Agent。
- `confidence`: 主 Agent 对判断的置信度。

示例：用户输入“今天北京的天气怎么样”时，MainAgent 会把“今天”转成当前日期，并委派给 `SearchAgent`：

```json
{
  "intent": "weather_lookup",
  "user_goal": "今天北京的天气怎么样",
  "normalized_input": "今天北京的天气怎么样",
  "context": {
    "relative_time": "今天",
    "date": "2026-06-28",
    "location": "北京",
    "source_scope": "web",
    "domain": "weather",
    "query": "北京 2026-06-28 天气 预报"
  },
  "source_policy": "requires_fresh_external_info",
  "expected_output": "给出简洁天气结论，并说明来源或无法确认的原因。",
  "delegate_to": "search_agent"
}
```

## 子 Agent 能力说明

每个子 Agent 类都提供 `CAPABILITY` 和 `capability_spec()`，由 `SubAgentRegistry` 读取注册。

能力说明字段：

- `name`
- `description`
- `handles`
- `does_not_handle`
- `capabilities`
- `tools`
- `input_contract`
- `output_contract`
- `risk_level`
- `examples`

后端接口 `GET /api/v1/agents` 会返回这些能力说明，供前端或调试使用。

## SubAgentResult

子 Agent 返回 `SubAgentResult`，既保留面向调试的详细内容，也提供给 MainAgent 汇总用的结构化字段。

字段：

- `ok`
- `content`
- `data`
- `error`
- `status`
- `summary`
- `findings`
- `evidence`
- `next_actions`
- `confidence`
- `tool_calls`

MainAgent 不再默认把 `content` 原样返回给用户。对于 `SearchAgent`，MainAgent 会优先读取 `summary/findings/evidence/data` 生成最终回答，原始长结果只放入调试信息。

## 搜索与天气示例

用户输入：

```text
今天北京的天气怎么样
```

当前流程：

```text
MainAgent.analyze_task
  -> 生成 TaskAnalysis + TaskBrief
MainAgent.route_task
  -> simple_task
SearchAgent.handle_brief
  -> web_search
MainAgent.synthesize_result
  -> 提取天气现象、温度、空气质量、来源
finalize
  -> 返回简洁最终回答
```

最终用户回答会类似：

```text
北京 2026-06-28 天气：小雨，气温约 21~29℃，空气质量优。
来源：www.tianqi.com，https://...
原始搜索结果已保留在调试信息中。
```

原始搜索结果仍可在前端调试面板的 `原始 JSON` 或 `SubAgent` 输出中查看。

## 调试信息

后端现在为每次 assistant 回复附带调试 metadata，统一放在 LangChain message 的：

```text
response_metadata["agent_dogs"]
```

不再把应用字段写入 `AIMessage.additional_kwargs` 顶层，避免污染 OpenAI/DeepSeek message 协议，尤其避免出现空 `tool_calls: []` 导致模型 API 400。

API 返回字段：

- `route`
- `complexity`
- `status`
- `interrupt`
- `plan_status`
- `task`
- `steps`
- `tool_calls`
- `debug_trace`
- `agent_flow`
- `task_brief`

`debug_trace` 是事件列表，记录：

- `MainAgent.analyze_task`
- `MainAgent.route_task`
- `MainAgent.simple_chat`
- `MainAgent.simple_task`
- `MainAgent.synthesize_result`
- `MainAgent.generate_plan`
- `MainAgent.execute_task`
- `MainAgent.finalize`
- `ToolRegistry.call`

`agent_flow` 是分层信息流，包含：

- `mainAgent`
- `subAgents`
- `tools`
- `finalOutput`
- `errors`

长 prompt、长文件内容、长工具结果会截断，避免调试信息过大。

## API 消息清洗

模型调用前会清洗历史消息，只保留模型协议需要的字段，移除应用级 metadata：

- `route`
- `complexity`
- `tool_calls`
- `steps`
- `debug_trace`
- `agent_flow`
- `task_brief`
- `interrupt`

旧消息兼容策略：

- API 输出时优先读取 `response_metadata["agent_dogs"]`。
- 旧历史消息 fallback 读取 `additional_kwargs`。
- 发送给模型前仍会清洗，避免旧数据污染模型请求。

## Clarify 交互

信息不足时进入 `clarify`：

- 例如“今天天气怎么样”缺少地点，会询问城市或地区。
- 后端返回 `interrupt.type = clarification`。
- 前端弹窗收集补充信息。
- 用户提交后调用 `POST /api/v1/sessions/{id}/resume`。

## 计划确认与复杂任务

复杂任务进入 `future_task`：

- MainAgent 生成计划。
- 前端展示计划确认弹窗。
- 用户可以确认、修改或取消。
- 当前阶段计划确认后进入 `TaskAgent` 第一阶段执行，只做低风险只读分析和步骤状态记录。

后续待做：

- 对复杂任务每个步骤增加完成度评价。
- 增加步骤验证、失败重试和证据检查。
- 把 LangGraph checkpoint state 整理成纯 JSON 可序列化结构，减少 checkpoint warning。

## 当前限制

- 写文件、删除、重命名、命令执行等高风险能力不会静默执行。
- `web_search` 依赖 `config/llm.yaml` 的 `search.enabled` 或环境变量启用。
- DuckDuckGo HTML 搜索是 best-effort，可能受网络、页面结构和搜索源限制影响。
- 天气等实时信息由搜索结果提取，无法保证等同官方气象接口。
- 复杂任务的逐步评价和质量检查尚未完成。
