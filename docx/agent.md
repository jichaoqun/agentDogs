# Agent 技术设计

## 当前阶段目标

当前项目已经完成基础 LLM 会话和 workspace 文件管理。Agent 第一阶段的目标是先建立“主 Agent + SimpleChatAgent”的最小可运行骨架：

- 主 Agent 负责任务解析、复杂度判断、工具任务识别、路由、信息补全、计划生成和计划确认。
- SimpleChatAgent 负责普通问答、解释、闲聊和简单文本生成。
- SimpleTaskAgent 负责明确、低风险、可直接调用工具完成的简单任务。
- 第一阶段不自动执行高风险复杂任务，不调用工具静默修改文件，不接入长期任务调度。
- 复杂任务先通过 LangGraph interrupt 暂停，让用户确认计划后再进入 TaskAgent 第一阶段执行。
- 计划确认后进入 TaskAgent 第一阶段执行，只做 workspace 文件只读分析和步骤状态记录。

## LLM 支持

项目的模型层由 `agent/core/utils/llm_models.py` 统一封装，目前支持三类模型来源：

- API 模型：OpenAI-compatible API，来自 `config/llm.yaml` 中 `providers.api` 配置。
- Ollama 模型：通过 Ollama 本地服务调用，并可扫描本地模型列表。
- 内置模型：项目内置本地模型，前端显示为“内置模型”。

模型选择、温度、最大输出长度和深度思考参数由前端传入后端，最终通过 `GenerationOptions` 传给模型层。

## MainAgent 与 SimpleChatAgent 职责

### MainAgent

位置：`agent/core/main_agent.py`

MainAgent 是当前阶段的统一入口，`chat()` 对外接口保持兼容，并新增 `resume()` 用于恢复 LangGraph 中断。它内部通过 LangGraph 表达任务流转：

- 接收用户输入和模型参数。
- 分析任务复杂度。
- 决定进入简单聊天、补充信息或复杂任务计划。
- 信息不足或计划待确认时触发 interrupt。
- 用户提交补充信息、确认计划、修改意见或取消任务后，通过 `resume()` 继续同一个 graph。
- 汇总最终响应并维护会话历史。

MainAgent 不直接执行工具。它会把普通聊天交给 SimpleChatAgent，把明确低风险工具任务交给 SimpleTaskAgent，把计划确认后的复杂任务交给 TaskAgent。

## Tools 与 Sub Agents 分层

当前采用三层结构：

- Tools: 基础能力层，负责文件、搜索、代码、文档解析等原子能力。
- Sub Agents: 专业任务层，组合工具完成某一类任务。
- MainAgent: 负责解析、规划、确认和调度。

工具层和子 Agent 层都有注册表：

- `ToolRegistry`: 注册和列出内部工具。
- `SubAgentRegistry`: 注册和列出专业子 Agent。

后端提供只读调试接口：

- `GET /api/v1/tools`
- `GET /api/v1/agents`

前端不直接调用工具，避免绕过 Agent 的权限和人工确认流程。

### SimpleChatAgent

位置：`agent/core/sub_agents/simple_chat_agent.py`

SimpleChatAgent 是一个低风险子 Agent：

- 拼接系统提示、历史消息和本轮用户输入。
- 调用当前选择的模型生成回复。
- 保存用户消息和助手回复到会话历史。
- 不调用工具，不读写文件，不执行 shell 或 Python。

SimpleChatAgent 适合处理普通问答、概念解释、简单文本生成、短文本改写、翻译和闲聊。

### SimpleTaskAgent

位置：`agent/core/sub_agents/simple_task_agent.py`

SimpleTaskAgent 是简单工具任务执行器：

- 接收 MainAgent 已判断为 `simple_task` 的请求。
- 通过 `ToolRegistry` 调用低风险工具。
- 第一版支持 `list_workspace_tree`、`read_file`、`workspace_search`、`web_search`、`file_info`。
- 即使工具注册表中存在高风险工具，也不会静默调用写入、删除、重命名、上传下载或命令执行类工具。

SimpleTaskAgent 适合处理目标明确的一步工具任务，例如“当前项目中有哪些文件”“读取 你好.md”“搜索 protein”“联网查一下 LangGraph”“查看 notes.md 信息”。

## LangChain / LangGraph 技术选择

第一阶段使用 LangGraph 的 `StateGraph` 表达主 Agent 流程，并使用 `InMemorySaver` 作为第一版 checkpointer。LangGraph 更适合长期运行、有状态、多节点路由的 Agent 编排，后续可以自然接入持久化 checkpoint、人工确认和任务恢复。

参考文档：

- LangGraph overview: https://docs.langchain.com/oss/python/langgraph/overview
- Thinking in LangGraph: https://docs.langchain.com/oss/python/langgraph/thinking-in-langgraph
- LangChain structured output: https://docs.langchain.com/oss/python/langchain/structured-output
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph checkpointers: https://docs.langchain.com/oss/python/langgraph/checkpointers
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts

依赖位置：`requirements.txt`

```txt
langgraph>=1,<2
```

## Agent 状态字段

位置：`agent/core/state.py`

`AgentState` 是 LangGraph 节点之间传递的运行状态。当前字段如下：

- `messages`: 当前会话历史消息快照。
- `user_input`: 本轮用户输入。
- `task_analysis`: 主 Agent 生成的结构化任务判断。
- `route`: 路由结果，可为 `simple_chat`、`simple_task`、`clarify`、`future_task`。
- `status`: 当前 graph 结果，可为 `completed` 或 `interrupted`。
- `missing_info`: 信息不足时需要用户补充的问题。
- `clarification_questions`: 前端弹窗使用的结构化补充问题。
- `clarification_answers`: 用户提交的补充信息。
- `task_plan`: 复杂任务计划，包含摘要、步骤、风险和确认需求。
- `plan_summary`: 计划摘要。
- `plan_steps`: 复杂任务的初步步骤列表。
- `plan_risks`: 计划风险与确认点。
- `plan_decision`: 用户对计划的操作，可为 `approve`、`revise`、`cancel`。
- `plan_feedback`: 用户对计划的修改意见。
- `plan_status`: 计划状态，可为 `pending`、`approved`、`revised`、`cancelled`。
- `final_response`: 最终展示给用户的文本。
- `interrupt_type`: 当前中断类型，可为 `clarification` 或 `plan_confirmation`。
- `interrupt_id`: LangGraph interrupt id，用于恢复时校验。
- `interrupt`: 返回给前端的结构化中断数据。
- `errors`: 节点运行错误信息，供调试和后续恢复使用。
- `selection`: 本轮模型选择。
- `options`: 本轮生成参数。
- `model_response`: 最终返回给 API 层的模型响应对象。

状态原则：

- state 保存原始结构化数据，不保存拼好的长 prompt。
- prompt 在节点内部按需格式化，便于调试、测试和后续替换节点实现。
- 第一阶段 checkpoint 只存在内存中，后端重启后 pending 中断不会恢复。

## 任务解析结构

`TaskAnalysis` 是主 Agent 对本轮任务的结构化判断：

- `intent`: 用户意图摘要。
- `complexity`: `simple`、`needs_info`、`complex`。
- `task_kind`: `chat`、`tool`、`task` 或 `unknown`。
- `route_hint`: 主 Agent 的显式路由建议。
- `tool_intents`: 简单工具任务可能使用的工具名称。
- `estimated_steps`: 预计步骤数量。
- `risk_level`: `low`、`medium`、`high`。
- `requires_confirmation`: 是否需要人工确认。
- `confidence`: 判断置信度，范围 `0-1`。
- `reason`: 判断理由。
- `missing_info`: 需要用户补充的信息。
- `suggested_steps`: 复杂任务建议步骤。
- `clarification_questions`: 信息不足时给前端展示的问题，每项包含 `id`、`question`、`options`、`allow_custom`、`required`。

实现策略：

- 先用规则快速判断明显简单、明显缺信息和明显复杂的任务。
- 对不确定任务保留 LLM 结构化判断入口，按 JSON schema 解析为 `TaskAnalysis`。
- 如果任务解析失败，回退到 `simple`，避免用户会话被任务判断阻塞。

## 节点流转图

当前 LangGraph 流程：

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
  -> approve / revise / cancel
  -> END

route_task
  -> generate_plan
  -> plan_confirm_interrupt
  -> approve / revise / cancel
  -> finalize
  -> END
```

节点职责：

- `analyze_task`: 生成 `TaskAnalysis`，判断任务复杂度和缺失信息。
- `route_task`: 根据 `route_hint`、`complexity` 和风险判断写入 `route`。
- `simple_chat`: 调用 SimpleChatAgent 完成普通对话。
- `simple_task`: 调用 SimpleTaskAgent 完成明确、低风险的工具任务。
- `clarify_interrupt`: 使用 LangGraph `interrupt()` 返回结构化补充问题。
- `generate_plan`: 根据原始任务、补充信息和修改意见生成计划。
- `plan_confirm_interrupt`: 使用 `interrupt()` 返回计划确认数据。
- `finalize`: 生成统一 `ModelResponse`。第一阶段只确认计划，不执行任务。

## Clarify 交互

当任务进入 `needs_info` 时，后端会暂停 graph，并在聊天响应中返回结构化字段：

- `route`: `clarify`。
- `complexity`: `needs_info`。
- `status`: `interrupted`。
- `interrupt.type`: `clarification`。
- `interrupt.id`: 本次 LangGraph interrupt id。
- `clarification.original_message`: 原始任务文本。
- `clarification.questions`: 需要补充的问题列表。

前端收到 `interrupt.type=clarification` 后自动打开补充信息弹窗。每个问题可以显示 2-4 个建议选项，也允许用户自定义输入。

用户提交后，前端调用：

```text
POST /api/v1/sessions/{id}/resume
```

请求体包含 `interrupt_id`、`type=clarification` 和 `answers`。后端用 `Command(resume=...)` 恢复同一个 graph，继续进入 `generate_plan`。

## 计划确认交互

复杂任务或补充信息完成后的任务会进入计划确认：

- `route`: `future_task`。
- `status`: `interrupted`。
- `interrupt.type`: `plan_confirmation`。
- `interrupt.plan.summary`: 计划摘要。
- `interrupt.plan.steps`: 计划步骤。
- `interrupt.plan.risks`: 风险与确认点。
- `plan_status`: `pending`。

前端展示计划确认弹窗，用户可以：

- 确认计划：提交 `decision=approve`，当前阶段返回“计划已确认”，不执行 TaskAgent。
- 修改计划：提交 `decision=revise` 和 `feedback`，后端重新生成计划并再次中断等待确认。
- 取消任务：提交 `decision=cancel`，当前任务结束。

## 复杂度判断规则

### simple

适合直接交给 SimpleChatAgent：

- 普通问答。
- 概念解释。
- 闲聊。
- 简单文本生成。
- 短文本改写、翻译和总结。

示例：

- “你好”
- “什么是 RAG”
- “帮我写一段会议开场白”

### simple_task

适合直接交给 SimpleTaskAgent：

- 目标明确。
- 低风险。
- 一步或少量步骤可由工具完成。
- 不涉及写入、删除、重命名、上传下载或命令执行。

示例：

- “当前项目中有哪些文件”
- “当前工作目录有哪些文件”
- “读取 你好.md”
- “搜索 protein”
- “搜索 workspace 中的 protein”
- “联网查一下 LangGraph”
- “查看 notes.md 信息”

### needs_info

需要先让用户补充信息：

- 用户提到“这个文件”“这个文档”等对象，但没有给出路径或范围。
- 用户没有说明要做什么动作。
- 用户没有说明输出格式、完成标准或确认条件。

示例：

- “帮我处理这个文件”
- “把这个文档整理一下”

### complex

第一阶段进入 `future_task`，只生成计划：

- 多步骤任务。
- 涉及项目、仓库、代码、文件、前端、后端、接口、测试等上下文。
- 需要执行命令、写文件、删除、上传、下载、重命名等操作。
- 用户明确要求计划、方案、报告、分阶段实现或整体分析。
- 写入、删除、重命名、上传下载和命令执行等高风险工具任务。

示例：

- “帮我分析整个项目并生成报告”
- “帮我调研 LangGraph 并整理对比”
- “实现一个新的文件管理模块”
- “修复后端接口并补充测试”

## 后续扩展路线

### TaskAgent

TaskAgent 承接复杂任务的执行：

- 接收 MainAgent 生成的 `plan_steps`。
- 将计划步骤转成执行记录，状态包括 `pending`、`running`、`completed`、`failed`、`waiting_confirmation`。
- 第一阶段调用 FileAgent 和 SearchAgent，不接 CodeAgent、KnowledgeAgent。
- 汇总每一步结果、错误和下一步建议。
- 遇到写文件、删除、命令执行等高风险步骤时，不自动执行，标记为 `waiting_confirmation`。

### FileAgent

负责 workspace 文件能力：

- 文件读取。
- 文件搜索。
- 文件预览。
- 文件只读分析。
- 第一阶段不自动写文件，只生成拟修改建议并等待人工确认。

### KnowledgeAgent

负责知识库能力：

- 知识库检索。
- RAG 检索。
- GraphRAG 检索。
- 文档问答。
- 当前未实现，后续接入。

### SearchAgent

负责外部信息能力：

- workspace 搜索。
- 关键词搜索。
- 联网搜索入口：启用 `search.enabled` 后使用 DuckDuckGo HTML/lite best-effort 搜索，并抓取前几个结果页正文片段。
- 搜索结果汇总。
- 第一版不直接写文件，不写入长期记忆。
- `web_search` 未启用、网络失败或页面抓取失败时返回明确提示，不编造联网结果。
- 联网结果必须保留来源 URL、标题、摘要、正文片段和抓取状态，供后续回答引用。

### CodeAgent

负责代码和命令能力：

- Python 执行。
- Shell 执行。
- 项目分析。
- 数据分析。
- 当前未实现，后续接入；命令执行必须经过沙箱和人工确认。

### MemoryAgent

负责长期记忆能力：

- 记忆写入。
- 记忆检索。
- 记忆压缩。
- 记忆整理。
- 当前未实现，后续接入。

### 计划模式与人工确认

当前阶段已经具备计划确认的第一版：

- MainAgent 先生成计划清单。
- 前端展示计划步骤。
- 用户确认、修改或取消计划。
- 第一阶段确认后进入 TaskAgent，只执行低风险只读文件分析；高风险步骤等待人工确认。

后续接入 TaskAgent 后，计划模式会扩展为：

- TaskAgent 执行已确认步骤。
- 对写文件、删除、长时间命令等高风险操作使用人工确认。

LangGraph 的 interrupt 和 checkpoint 已用于信息补充和计划确认。后续可继续用于文件修改审批、命令执行审批、长任务暂停恢复和失败后从中间状态继续。

## 当前限制

- 第一阶段不自动执行高风险复杂任务。
- 第一阶段已经把 `route`、`complexity`、`status`、`interrupt`、`clarification`、`plan_steps`、`plan_status` 暴露给前端。
- 第一阶段已经扩展 `task`、`steps`、`tool_calls` 字段，用于展示 TaskAgent 执行结果。
- 第一阶段使用 `InMemorySaver`，服务重启后 Agent 图状态不会恢复。
- 当前 checkpoint 中仍保存部分 Python/Pydantic 对象；后续切换 SQLite/Postgres 持久化前，建议把 graph state 收敛为纯 JSON 可序列化字段。
- LLM 结构化判断是兜底能力，主要路径仍是规则判断。
