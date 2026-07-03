# Agent 技术设计

本文档记录 Agent Dogs 当前后端 Agent 架构、模块拆分、任务路由、子 Agent 能力边界、调试数据结构和当前限制。

## 当前目标

Agent Dogs 当前采用“MainAgent 任务理解与编排 + 子 Agent 领域自治执行 + MainAgent 最终汇总输出”的结构。

职责划分：

- `MainAgent`：统一入口，维护 LangGraph 流程、会话中断、恢复、历史记录和最终 `ModelResponse`。
- `AgentRoutingMixin`：任务理解、规则路由、LLM 任务解析、TaskBrief 构造。
- `AgentDebugMixin`：生成 `debug_trace` 和前端分层 `agent_flow`。
- `AgentResponseSynthesizerMixin`：把子 Agent 的结构化结果汇总成最终用户回答。
- `AgentMetadataMixin`：把应用 metadata 写入 `response_metadata["agent_dogs"]`，避免污染模型协议字段。
- `SimpleChatAgent`：纯聊天和简单文本生成，不调用工具。
- `SearchAgent`：处理 workspace、关键词和联网搜索任务。
- `FileAgent`：处理 workspace 文件读取、文件搜索、只读分析和摘要。
- `CodeAgent`：通过配置的 `code_sandbox` 后端用 Python 完成数据分析、图表生成、代码/项目分析、代码生成和受控脚本执行。
- `SimpleTaskAgent`：兼容层，保留一步低风险工具任务能力。
- `TaskAgent`：复杂任务计划确认后的第一阶段执行协调器，按步骤分类、委派 File/Search/Code 子 Agent、复用上下文，并把 workspace 写入动作转为二次确认。

## 后端模块拆分

`agent/core/main_agent.py` 只保留主编排逻辑：

- 初始化模型、工具和子 Agent。
- 构建 LangGraph。
- `chat()` / `resume()` 入口。
- LangGraph 节点：`analyze_task`、`route_task`、`simple_chat`、`simple_task`、`clarify_interrupt`、`generate_plan`、`plan_confirm_interrupt`、`execute_task`、`finalize`。

拆出的模块：

- `agent/core/agent_routing.py`
  - 路径识别、关键词 marker、规则判断。
  - `_rule_analysis()`、`_llm_analysis()`、`_llm_plan()`。
  - `TaskBrief` 的 context、intent、constraints、delegate_to、expected_output 构造。
- `agent/core/agent_debug.py`
  - `debug_trace` 事件追加。
  - tool 调用事件展开。
  - `agent_flow` 的 `mainAgent/subAgents/tools/finalOutput/errors` 分层结构。
- `agent/core/agent_responses.py`
  - SearchAgent 结果摘要。
  - Weather 搜索结果提取。
  - CodeAgent 沙箱结果和 artifacts 汇总。
- `agent/core/agent_metadata.py`
  - `AGENT_METADATA_KEY = "agent_dogs"`。
  - `_with_agent_metadata()`。
  - `AIMessage.additional_kwargs` 安全过滤。
  - 用户/助手历史记录 metadata 写入。
- `agent/core/utils/prompt.py`
  - `DEFAULT_SYSTEM_PROMPT`。
  - `TASK_ANALYSIS_SYSTEM_PROMPT`。
  - `TASK_PLAN_SYSTEM_PROMPT`。
  - `build_task_analysis_prompt()`。
  - `build_task_plan_prompt()`。
  - `build_simple_chat_system_prompt()`。

## LLM 支持

模型层由 `agent/core/utils/llm_models.py` 统一封装，配置来自 `config/llm.yaml`。

支持 Provider：

- `api`: OpenAI-compatible API。
- `ollama`: 本地 Ollama 服务。
- `builtin`: 项目内置本地模型。

模型选择、温度、最大输出长度和 thinking 参数由前端传给后端，最终以 `GenerationOptions` 传入模型层。模型调用不会在 Provider 之间静默 fallback；显式选择的 Provider 失败时直接返回错误。

模型请求前会清洗历史消息，移除应用级 metadata，避免再次出现 `tool_calls: []` 污染 OpenAI/DeepSeek 请求协议的问题。

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

关键节点：

- `analyze_task`: 生成 `TaskAnalysis` 和 `TaskBrief`。
- `route_task`: 根据复杂度、风险、缺失信息和 route hint 选择路线。
- `simple_chat`: 调用 `SimpleChatAgent`。
- `simple_task`: 根据 `TaskBrief.delegate_to` 委派给 `SearchAgent`、`FileAgent`、`CodeAgent` 或兼容层 `SimpleTaskAgent`。
- `MainAgent.synthesize_result`: 把子 Agent 结构化结果汇总成最终回答。
- `clarify_interrupt`: 信息不足时暂停，返回结构化补充问题。
- `generate_plan`: 复杂任务生成计划。
- `plan_confirm_interrupt`: 等待用户确认、修改或取消计划。
- `execute_task`: 计划确认后进入 `TaskAgent` 第一阶段执行。
- `finalize`: 生成统一 `ModelResponse` 并附带 metadata。

## TaskAnalysis

`TaskAnalysis` 是主 Agent 对任务复杂度和风险的判断。

主要字段：

- `intent`
- `complexity`: `simple`、`needs_info`、`complex`
- `task_kind`: `chat`、`tool`、`task`、`unknown`
- `route_hint`: `simple_chat`、`simple_task`、`clarify`、`future_task`
- `tool_intents`
- `risk_level`: `low`、`medium`、`high`
- `requires_confirmation`
- `missing_info`
- `suggested_steps`
- `clarification_questions`

任务判断优先使用规则；规则无法确定时再调用 LLM 生成结构化 JSON。

## TaskBrief

`TaskBrief` 是 MainAgent 给子 Agent 的任务委派对象。它不是底层工具参数，而是任务说明。

字段：

- `intent`: 标准化意图，例如 `weather_lookup`、`search`、`data_analysis`、`chart_generation`。
- `user_goal`: 原始用户目标。
- `normalized_input`: 纠错和清洗后的输入。
- `context`: 时间、地点、路径、搜索范围等上下文。
- `constraints`: 执行约束，例如需要新鲜外部信息、workspace 只读、高风险操作需确认。
- `source_policy`: `not_required`、`workspace_only`、`requires_fresh_external_info`。
- `expected_output`: 期望输出形式。
- `delegate_to`: 目标子 Agent。
- `confidence`: 主 Agent 对判断的置信度。

示例：用户输入“今天北京的天气怎么样”时，MainAgent 会把“今天”转成当前日期，并委派给 `SearchAgent`：

```json
{
  "intent": "weather_lookup",
  "context": {
    "relative_time": "今天",
    "date": "2026-06-28",
    "location": "北京",
    "source_scope": "web",
    "domain": "weather",
    "query": "北京 2026-06-28 天气 预报"
  },
  "source_policy": "requires_fresh_external_info",
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

MainAgent 不再默认把 `content` 原样返回给用户。对于 SearchAgent 和 CodeAgent，MainAgent 会优先读取结构化字段生成最终回答，原始长结果保留在调试信息中。

## Search 与天气示例

用户输入：

```text
今天北京的天气怎么样
```

流程：

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

原始搜索结果仍可在前端调试面板的 `原始 JSON` 或 `SubAgent` 输出中查看。

## CodeAgent

`CodeAgent` 的定位不是“只会写代码”，而是“通过安全代码执行环境完成任务”的子 Agent。它可以分析数据、生成图表、分析代码结构、分析项目结构、生成代码文本，并在明确授权时执行用户提供的 Python 脚本。

当前支持：

- `data_analysis`: 读取 workspace 中的 CSV/Excel/JSON/TXT/MD，生成统计摘要、缺失值、类型推断、相关性和类别分布。
- `chart_generation`: 自动选择趋势图、柱状图、散点图或分布图，在沙箱中生成图表 artifact。
- `code_analysis`: 对 Python/JS/TS/JSON/YAML/HTML/CSS 等文件做结构分析。
- `project_analysis`: 对 workspace 项目做只读结构扫描、依赖/入口线索识别和文件类型统计。
- `code_generation`: 只返回代码文本，不落盘、不执行。
- `script_execution`: 用户明确要求运行 Python 代码时，通过 `code_sandbox` 后端执行。
- `notebook_like_analysis`: 执行分析脚本并返回 stdout、summary 和 artifacts。

安全边界：

- 支持 `opensandbox` 和 `local_process` 两种后端。
- OpenSandbox Server 不可用或 `code_execution.enabled=false` 时返回明确失败，不自动降级到本地执行。
- `local_process` 不是强安全沙箱，必须显式启用，默认需要人工 `execution_approval`。
- 不做隐式 fallback。
- workspace 通过环境变量 `AGENT_WORKSPACE_DIR` 提供给脚本，默认兼容 `/workspace`。
- 输出只写入 `runtime/artifacts/<run_id>/`。
- 默认无网络；OpenSandbox 后端可按任务控制 network，local_process 不承诺系统级禁网，需要网络时必须人工确认。
- 运行时依赖安装必须显式开启，并受 `allowed_packages` allowlist 限制。
- 用户脚本执行必须显式开启 `allow_user_script_execution`。
- 限制 CPU、内存、超时和 stdout/stderr 长度。

运行时依赖安装：

- 配置项：`code_execution.dependency_install.enabled`。
- allowlist：`code_execution.dependency_install.allowed_packages`。
- 默认推荐包：`pandas`、`numpy`、`openpyxl`、`matplotlib`、`seaborn`、`scipy`、`scikit-learn`。
- 非 allowlist 包会被拒绝，不能自动安装。

路由示例：

- `分析 02.xlsx 表格数据` -> `data_analysis`。
- `生成 02.xlsx 的分析结果图` -> `chart_generation`。
- `运行这段 Python 代码` -> `script_execution`。
- `帮我生成一个读取 csv 的脚本` -> `code_generation`，不执行沙箱。
- `分析整个项目代码结构` -> `project_analysis`。

Excel 行为：

- `分析 02.xlsx 表格数据` 会进入 `CodeAgent`，`TaskBrief.intent = data_analysis`。
- `生成 02.xlsx 的分析结果图` 会进入 `CodeAgent`，`TaskBrief.intent = chart_generation`。
- `帮我查看02.xlsx表格中的内容，并对他进行数据分析，将分析的结果图新建一个02_analys文件夹存放` 会进入 `future_task` 计划确认，因为它要求写入 workspace 指定目录。
- 当前图表默认写入 artifacts，不直接创建 workspace 下的 `02_analys`。
- 执行环境缺少 `openpyxl` 或 `matplotlib` 且依赖安装不可用时，必须返回明确失败，不能编造表格内容或图片。

## TaskAgent 第一阶段执行

`TaskAgent` 的职责是执行已经由用户确认的复杂任务计划，但“计划确认”不等于“允许写 workspace”。它会优先完成可安全自动执行的只读、搜索、沙箱分析和 artifact 生成步骤；遇到创建目录、写文件、移动/复制产物等动作时，返回 `waiting_confirmation`，等待后续二次确认。

输入：

- `user_input`: 原始用户目标。
- `plan_steps`: MainAgent 生成且用户已确认的计划步骤。
- `task_brief`: 可选的 MainAgent 委派对象，用于继承路径、意图、约束等上下文。

输出：

- `status`: `completed`、`failed` 或 `waiting_confirmation`。
- `summary`: 当前阶段汇总。
- `steps`: 每个步骤的结构化记录。
- `tool_calls`: 子 Agent 或工具调用记录。
- `artifacts`: CodeAgent 生成的产物。
- `pending_confirmations`: 等待二次确认的 workspace 写入动作。
- `final_findings`: 阶段性发现。

步骤类型：

- `file_read`: 委派给 `FileAgent`。
- `search`: 委派给 `SearchAgent`。
- `data_analysis`: 委派给 `CodeAgent`。
- `chart_generation`: 委派给 `CodeAgent`。
- `code_analysis`: 委派给 `CodeAgent`。
- `workspace_write`: 不直接执行，生成二次确认 payload。
- `manual_review`: 不调用工具，只记录为执行策略或上下文。

每个 `TaskStepRecord` 包含：

- `index`
- `title`
- `status`
- `result`
- `error`
- `assigned_agent`
- `step_type`
- `requires_confirmation`
- `confirmation_payload`
- `artifacts`
- `tool_calls`

对于 Excel 分析这类连续步骤，TaskAgent 会保留共享上下文，例如 `primary_path = 02.xlsx` 和已生成 artifacts。类似计划：

```text
打开并读取02.xlsx文件，查看数据结构和内容。
对数据进行初步探索性分析。
根据需要选择适合的分析方法和图表类型。
生成分析图表并保存为图片或PDF格式。
在当前目录下新建名为02_analys的文件夹。
将所有生成的图表文件移动到或直接保存到02_analys文件夹中。
```

当前执行策略：

- 前两个数据分析步骤委派或复用 `CodeAgent`。
- “选择图表类型”记录为 `manual_review`。
- “生成分析图表”委派给 `CodeAgent`，产物写入 `runtime/artifacts/<run_id>/`。
- “新建 02_analys 文件夹”和“移动/保存图表到 02_analys”标记为 `workspace_write + waiting_confirmation`。
- 未二次确认前，不调用创建目录、复制、移动或写入 workspace 的工具。

`pending_confirmations` 示例：

```json
{
  "action": "publish_artifacts",
  "step": "将所有生成的图表文件移动到或直接保存到02_analys文件夹中。",
  "target_directory": "02_analys",
  "artifacts": [
    {
      "filename": "chart.png",
      "url": "/api/v1/artifacts/run-1/chart.png"
    }
  ],
  "source_path": "02.xlsx",
  "requires_confirmation": true
}
```

当前阶段只返回待确认请求；后续可以接入前端二次确认弹窗和 `resume` 后的实际 workspace 写入。

## Prompt 管理

所有主 Agent 相关 prompt 统一放在 `agent/core/utils/prompt.py`。

包含：

- 默认系统提示：`DEFAULT_SYSTEM_PROMPT`
- 任务解析系统提示：`TASK_ANALYSIS_SYSTEM_PROMPT`
- 任务规划系统提示：`TASK_PLAN_SYSTEM_PROMPT`
- SimpleChat 防幻觉提示：`SIMPLE_CHAT_TOOL_GUARD_PROMPT`
- 任务解析 prompt builder：`build_task_analysis_prompt()`
- 任务规划 prompt builder：`build_task_plan_prompt()`
- SimpleChat 系统提示 builder：`build_simple_chat_system_prompt()`

约束：

- `SimpleChatAgent` 是纯聊天 Agent，不调用工具。
- 当用户要求读文件、分析表格、执行代码、生成图表、保存文件或创建目录时，SimpleChatAgent 不能声称已经完成，也不能编造文件内容、统计结果、图片或保存路径。

## 调试信息

后端为每次 assistant 回复附带调试 metadata，统一放在 LangChain message 的：

```text
response_metadata["agent_dogs"]
```

不再把应用字段写入 `AIMessage.additional_kwargs` 顶层，避免污染 OpenAI/DeepSeek message 协议。

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
- 当前阶段计划确认后进入 `TaskAgent` 第一阶段执行。
- TaskAgent 可把搜索步骤委派给 `SearchAgent`，文件步骤委派给 `FileAgent`，数据/图表/代码步骤委派给 `CodeAgent`。
- TaskAgent 会保留步骤上下文和 artifacts，最终返回结构化 `task_steps`、`tool_calls`、`pending_confirmations`。
- 创建目录、写文件、移动/复制 artifacts 到 workspace 等写入动作仍需要二次确认；计划确认不会直接触发这些高风险工具。

后续待做：

- 对复杂任务每个步骤增加完成度评价。
- 增加步骤验证、失败重试和证据检查。
- 把 LangGraph checkpoint state 整理成纯 JSON 可序列化结构，减少 checkpoint warning。

## 当前限制

- 写文件、删除、重命名、命令执行等高风险能力不会静默执行。
- `web_search` 依赖 `config/llm.yaml` 的 `search.enabled`。
- `CodeAgent` 依赖 `config/llm.yaml` 的 `code_execution.enabled`、`code_execution.backend` 和对应执行后端。
- DuckDuckGo HTML 搜索是 best-effort，可能受网络、页面结构和搜索源限制影响。
- 天气等实时信息由搜索结果提取，无法保证等同官方气象接口。
- 复杂任务的逐步评价和质量检查尚未完成。
