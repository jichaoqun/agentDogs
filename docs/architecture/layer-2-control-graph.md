# 第二层：Main Control Graph 详细设计

## 1. 目标

Main Control Graph 是一次 Run 的确定性控制平面，负责：

- 显式表示任务状态；
- 限制合法状态转换；
- 驱动 Coordinator 和领域 Agent；
- 统一处理工具、审批、澄清、取消、压缩和错误；
- 保存可恢复 checkpoint；
- 保证 LLM 不能绕过系统安全规则。

Graph 解决“当前运行到哪里、下一状态是否合法”，不解决“具体搜索什么、代码怎么写”。

## 2. 控制原则

### 2.1 语义由LLM产生，流转由代码执行

Coordinator 或 Agent 可以产生结构化命令，但不能直接跳转 Graph。

```text
LLM → Typed Command → Schema Validation → Rule Router → Graph Node
```

### 2.2 安全状态不由LLM决定

以下内容只使用确定性代码：

- 工具是否允许；
-是否需要审批；
-路径是否合法；
-预算是否耗尽；
-是否取消；
-是否允许 Resume；
-当前状态是否可转换；
-结果是否属于当前 Run。

### 2.3 Graph节点保持小而明确

节点只做一类状态转换。不得在单个节点中同时完成 Coordinator 判断、工具执行、结果汇总和最终回答。

## 3. 状态模型

Graph 使用可序列化 `AgentState`。状态分为六组：

```text
identity      session_id / run_id
conversation  user_input / messages
coordination  command / plan / completed_tasks
execution     task_executions / ready_task_ids / running_task_ids
tooling       每个 TaskExecution 内的 pending_tool_call / results / approval
runtime       status / budgets / errors / events
```

禁止存入：

- 不可序列化的 SDK Client；
-线程锁；
-打开的文件句柄；
-运行中的 subprocess 对象；
-完整 Tool Handler；
-闭包和 lambda。

这些对象通过 Runtime Context 注入。

## 4. 标准命令

### 4.1 CoordinatorCommand

```python
class CoordinatorCommand(BaseModel):
    type: Literal["delegate", "plan", "clarify", "final", "fail"]
    target_agent: str | None
    goal: str
    context: str
    success_criteria: list[str]
    reason: str
```

### 4.2 AgentAction

```python
class ToolAction(BaseModel):
    type: Literal["tool"]
    tool_call: ToolCall

class CompleteAction(BaseModel):
    type: Literal["completed"]
    result: TaskResult

class TransferAction(BaseModel):
    type: Literal["transfer"]
    target_agent: str
    reason: str

class ClarifyAction(BaseModel):
    type: Literal["clarify"]
    request: ClarificationRequest

class FailAction(BaseModel):
    type: Literal["fail"]
    error: AgentError
```

领域 Agent不能直接启动另一个 Agent；`TransferAction` 必须返回 Coordinator 重新判断。

## 5. 节点设计

### 5.1 initialize

输入：

- Session Runtime 创建的初始状态；
-用户消息；
-RunOptions。

职责：

-校验 session_id/run_id；
-写入 RunStarted 事件；
-设置全局预算；
-加载最近会话摘要；
-将状态设为 `coordinating`。

输出边：`coordinate`。

### 5.2 coordinate

职责：

-调用 Coordinator；
-校验结构化输出；
-写入 `coordinator_command`；
-递增 Coordinator 调用预算；
-记录 CoordinatorDecided。

本节点不执行工具。

### 5.3 start_agent

职责：

-根据 Command 创建 `AgentTask`；
-验证目标 Agent 存在；
-初始化该任务的 `agent_messages`；
-在指定 `TaskExecution` 中设置 `agent_name`；
-设置 Agent 局部预算；
-构建首次 Context。

输出边：`agent_step`。

### 5.4 agent_step

职责：

-从 AgentRegistry 获取 active agent；
-使用 Context Manager 构建当前视图；
-执行一次 LLM 决策；
-解析为 `AgentAction`；
-检查 Agent 迭代预算；
-记录 AgentStepCompleted。

本节点只执行一次推理，不包含 while 循环。循环由 Graph 边表达。

### 5.5 policy_check

职责：

-校验 ToolCall schema；
-验证 `requested_by == task_execution.agent_name`；
-验证工具属于 Agent allowed tools；
-调用 Policy Engine；
-生成 allow/approval/deny 决策。

### 5.6 execute_tool

职责：

-从 ToolRegistry 获取 Handler；
-调用 Tool Runtime；
-传入 CancellationToken；
-获取结构化 ToolResult；
-记录 ToolCompleted。

本节点不重新调用 Agent。

### 5.7 apply_tool_result

职责：

-验证 call_id；
-将 ToolResult 转为 ToolMessage；
-追加到当前 `agent_messages`；
-清理 pending tool；
-更新 artifact 与错误摘要。

输出边：`agent_step`。

### 5.8 approval_interrupt

职责：

-构造安全展示数据；
-保存 checkpoint；
-调用 LangGraph interrupt；
-Resume 后校验 decision；
-批准则进入 execute_tool；
-拒绝则构造 PermissionDenied ToolResult。

### 5.9 clarification_interrupt

职责：

-保存问题、字段约束和原始任务；
-暂停；
-Resume 后将用户答案作为新的任务上下文；
-返回 Coordinator，而不是直接返回原 Agent。

### 5.10 generate_plan

职责：

-调用 Planner；
-验证 TaskPlan；
-建立任务依赖；
-根据风险决定是否需要 plan confirmation；
-将计划返回 Coordinator。

### 5.11 compress_context

职责：

-调用 Context Manager；
-保护最近消息；
-保护未完成工具调用对；
-生成工作摘要；
-保存压缩前引用；
-回到 `agent_step`。

### 5.12 finalize

职责：

-确保没有 pending tool/approval；
-根据完成结果生成最终回复；
-提交用户消息和 Assistant 消息；
-保存 TaskResult、artifact 和来源；
-写入 RunCompleted；
-将状态设为 `completed`。

### 5.13 cancel

职责：

-停止接纳模型和工具结果；
-取消子进程；
-不提交 pending Assistant 消息；
-写入 RunCancelled；
-状态设为 `cancelled`。

### 5.14 handle_error

职责：

-错误分类；
-判断 retryable；
-在预算内进入重试节点；
-不可恢复错误进入 failed；
-输出对用户安全的错误说明。

## 6. 路由规则

```python
def route_agent_action(state: AgentState) -> str:
    action = state["agent_action"]
    if action.type == "tool":
        return "policy_check"
    if action.type in {"completed", "transfer"}:
        return "coordinate"
    if action.type == "clarify":
        return "clarification_interrupt"
    if action.type == "fail":
        return "handle_error"
    raise InvalidStateTransition(...)
```

路由函数必须：

- 纯函数；
-无网络访问；
-不调用 LLM；
-不修改外部状态；
-对未知值失败关闭。

## 7. 转换矩阵

| 层级 | 当前状态 | 输入 | 下一状态 |
|---|---|---|---|
| Parent Run | initializing | 初始化成功 | coordinating |
| Parent Run | coordinating | delegate/fork | running_tasks |
| Parent Run | coordinating | plan | planning |
| Parent Run | coordinating | clarify | waiting_user；对应 Task 进入 waiting_clarification |
| Parent Run | coordinating | final | composing_final |
| Parent Run | running_tasks | 子任务产生 tool | 保持 running_tasks；对应 Task 进入 tool_preparing |
| Parent Run | running_tasks | 子任务 complete/transfer | 更新 join 或 coordinating |
| Task | tool_preparing | allow | tool_executing |
| Task | tool_preparing | approval | waiting_approval；Parent Run 聚合为 waiting_user |
| Task | waiting_approval | approve | tool_executing；Parent Run 聚合为 running_tasks |
| Task | waiting_approval | reject | running；Parent Run 聚合为 running_tasks |
| Parent Run / Task | 任意非终态 | cancel | cancelled |
| Parent Run / Task | 任意非终态 | fatal error | failed |

代码中应将矩阵实现为显式验证器。

## 8. 预算

```python
class RunBudget(BaseModel):
    max_model_calls: int
    max_tool_calls: int
    max_agent_switches: int
    max_planner_calls: int
    max_wall_seconds: int
    max_context_tokens: int
```

预算检查点：

- Coordinator 前；
- Agent Step 前；
- Tool执行前；
-子 Agent切换前；
-Planner 前。

预算耗尽后不得伪装成成功，应返回：

```text
已完成内容
未完成内容
停止原因
可继续方式
```

## 9. 重试

重试由错误类型决定：

| 错误 | 策略 |
|---|---|
| 结构化输出解析失败 | 同模型修复一次 |
| 429/临时5xx | 退避或备用模型 |
| Tool参数验证失败 | 返回 Agent 修正参数 |
| 权限拒绝 | 返回 Agent 选择替代方案 |
| 路径越界 | 不重试，记录安全事件 |
| Sandbox超时 | 可由 Agent 简化任务后重试 |
| 取消 | 不重试 |

## 10. Checkpoint

必须在以下边界保存：

- Coordinator 决策后；
-Agent产生 ToolCall 后；
-进入 interrupt 前；
-ToolResult应用后；
-Agent Task完成后；
-Finalization 前。

Checkpoint 不保存运行中的对象，只保存重建所需数据。

## 11. Runtime依赖注入

```python
class GraphRuntime:
    coordinator: Coordinator
    planner: Planner
    agents: AgentRegistry
    tools: ToolExecutor
    policy: PolicyEngine
    context: ContextManager
    sessions: SessionStore
    events: EventSink
    cancellation: CancellationRegistry
```

GraphState 保存数据，GraphRuntime 提供服务。

## 12. 测试

### 路由测试

- 每种 Command 到正确节点；
-未知 Command 失败；
-ToolCall 所属 Agent 不匹配；
-终态不能重新进入执行节点。

### Interrupt测试

-审批前保存 checkpoint；
-错误 interrupt_id 被拒绝；
-拒绝审批生成 ToolResult；
-Resume 后不重复执行已经完成的工具。

### 恢复测试

-在每个 checkpoint 边界模拟重启；
-恢复后状态一致；
-不会重复写消息或 artifact。

### 预算测试

-模型调用预算；
-工具调用预算；
-Agent切换预算；
-总运行时间预算。

## 13. 验收标准

- Graph 中所有边都由类型化结果和代码规则决定；
- LLM 不能直接选择任意节点；
-安全决策不依赖自然语言；
-每个节点职责单一；
-所有 interrupt 可恢复；
-取消可从所有非终态进入；
-状态可完整序列化；
-未知状态和未知动作默认拒绝。

## 14. 执行语义修订

本章补全 Graph 在崩溃恢复、工具副作用和最终提交方面的确定性语义。与前文章节冲突时，以本章为准。

### 14.1 Run 内受控并行

V2 支持同一 Run 内多个无依赖 Task 并行执行。单个 Agent step 仍只产生一个 ToolCall，但不同 TaskExecution 可以同时各自执行一个 ToolCall：

```python
class ToolAction(BaseModel):
    type: Literal["tool"]
    tool_call: ToolCall
```

模型在一个 Agent step 中返回多个工具调用时，解析器返回 `MULTIPLE_TOOL_CALLS_NOT_SUPPORTED`，并要求该 Agent 下一步只选择一个。这里限制的是单个任务步骤，不限制父 Run 中其他 TaskExecution 并行运行。

父 Graph 维护 `TaskExecution` 集合：

```python
class TaskExecution(BaseModel):
    task_id: str
    attempt: int
    status: Literal[
        "pending", "ready", "running", "waiting_approval",
        "waiting_clarification", "tool_preparing", "tool_executing",
        "tool_reconciling", "joining", "completed",
        "failed", "blocked", "cancelled", "execution_unknown"
    ]
    agent_name: str | None
    agent_messages_ref: str | None
    pending_operation_id: str | None
    checkpoint_id: str | None
    budget: TaskBudget
    lease: TaskLease | None
    result_reference: str | None
```

```python
class TaskLease(BaseModel):
    run_id: str
    task_id: str
    task_attempt: int
    lease_owner: str
    lease_token: str
    lease_until: datetime
    heartbeat_at: datetime
    recovery_attempts: int
```

Scheduler 从 DAG 中选择所有依赖已完成的 ready task，在 `max_parallel_tasks`、全局预算、Agent 配额和工具资源限制内执行 fork。每个子任务独立 checkpoint；父 Graph 只保存引用和聚合状态。

Join 规则必须由计划定义：

- `all_success`：全部成功后继续，一个失败即取消尚未开始的同组任务；
- `all_settled`：等待全部进入终态，再由 Coordinator 汇总；
- `min_success(n)`：达到成功阈值后可继续，并取消不再需要的任务；
- `best_effort`：允许 partial/failed 结果参与汇总。

实现阶段允许配置 `max_parallel_tasks = 1`，但存储模型、状态协议和测试必须使用 TaskExecution 集合，之后提高并发度不应改变上层契约。

原有 `start_agent`、`agent_step`、`policy_check`、`execute_tool` 和 `apply_tool_result` 节点都必须接收 `task_id`，只读写对应 TaskExecution，不能再依赖全局单值 `active_agent` 或 `active_task`。父 Run 的 `running_tasks` 状态是多个子状态的聚合视图，不覆盖子任务真实状态。

并发提交使用 `(run_id, task_id, task_revision)` CAS。不同任务可分别提交；会影响父 DAG readiness、共享预算或 join 状态的修改通过父 Run revision 原子合并。发生冲突时重新加载并重算聚合状态，不覆盖其他任务已提交结果。

### 14.2 状态机补充

父 Run 和子任务使用不同状态，不把多个子任务状态压成一个全局 `running_agent`：

```python
RunLifecycleStatus = Literal[
    "initializing", "coordinating", "planning", "running_tasks",
    "waiting_user", "joining", "compressing", "composing_final",
    "committing_final", "cancelling", "execution_unknown",
    "completed", "cancelled", "failed",
]

TaskExecutionStatus = Literal[
    "pending", "ready", "running", "tool_preparing",
    "waiting_approval", "waiting_clarification", "tool_executing",
    "tool_reconciling", "joining", "completed", "failed",
    "blocked", "cancelled", "execution_unknown",
]
```

枚举的权威语义见 [Runtime 状态与结果契约](runtime-status-contract.md)。下表中的 `running_agent` 表示 Graph 节点名称；持久 TaskExecutionStatus 写入 `running`。

`waiting_tool/executing` 不再是复合状态。关键转换如下：

| 当前状态 | 条件 | 下一状态 |
|---|---|---|
| `running_agent` | 单个 ToolCall | `tool_preparing` |
| `tool_preparing` | deny | `running_agent` |
| `tool_preparing` | require approval | `waiting_approval` |
| `tool_preparing` | allow 且账本 prepared | `tool_executing` |
| `waiting_approval` | approve 且票据有效 | `tool_executing` |
| `tool_executing` | 结果已入账 | `running_agent` |
| `tool_executing` | 恢复时状态不明 | `tool_reconciling` |
| `tool_reconciling` | 无法确认 | `execution_unknown` |
| `composing_final` | 回复生成成功 | `committing_final` |
| `committing_final` | 原子提交成功 | `completed` |

### 14.3 工具节点拆分

工具链改为：

```text
prepare_tool_operation
    -> policy_decide
    -> approval_interrupt (optional)
    -> execute_tool_operation
    -> reconcile_tool_operation (recovery only)
    -> apply_tool_result
```

`prepare_tool_operation` 生成稳定 `operation_id`，规范化参数并计算 `arguments_hash`，然后在 checkpoint 中保存引用。Policy 决策和审批票据都绑定：

```text
operation_id + tool_name + arguments_hash + principal + policy_version
```

任何字段变化都会使旧审批失效。

`execute_tool_operation` 不直接决定是否重试。它只调用第四层 Tool Runtime，并读取 Operation Ledger 的确定状态。恢复时若账本为 `running/unknown`，必须先进入 `reconcile_tool_operation`。

### 14.4 无副作用节点与副作用节点

Graph 节点分两类：

- 可重放节点：模型调用、Coordinator、Planner、Context 压缩、最终回复生成；输出尚未提交时可以重算。
- 提交节点：工具执行、Interrupt 消费、artifact 发布、最终消息提交；必须通过幂等账本或原子事务执行。

Checkpoint 只意味着 Graph 状态已提交，不意味着外部副作用可自动重放。

### 14.5 最终回复拆分

原 `finalize` 拆为：

1. `compose_final_response`：只根据已提交 TaskResult 生成候选回复，无外部副作用。
2. `commit_run_completion`：通过 SessionStore 的 `finalize_run(completed, outcome, message)` 原子提交最终消息、Outcome、状态和 outbox event。Cancel 和不可恢复错误分别调用同一事务的 cancelled/failed 变体，消息可以为空。

最终消息使用 `(run_id, message_kind=final_assistant)` 唯一约束。事务结果未知时查询该键；存在则视为提交成功，不再生成第二条消息。

### 14.6 Checkpoint 瘦身

GraphState 不保存完整事件流、网页正文、stdout 或大型 ToolResult，只保存：

- 身份、状态、revision 和预算计数；
- 当前 Agent/Task/Operation 的稳定 ID；
- 最近必要消息和工作摘要；
- 独立消息、结果、artifact、event 的引用；
- 尚未完成的 Interrupt 或 Operation 引用。

事件、工具结果和大文本采用追加式存储。checkpoint 必须设置序列化大小上限，超限时先执行压缩或外置存储。

### 14.7 重试规则补充

- 模型调用可以按调用 ID 重试，只有成功解析的结果进入 checkpoint；
- 只读且声明幂等的工具可按同一 `operation_id` 自动恢复；
- 写入和破坏性工具不得因超时自动重放；
- `execution_unknown` 不得被 Coordinator 当作普通失败绕过；
- 预算在尝试前预留、提交后结算，恢复时不得重复扣减已入账调用；
- 权限拒绝不是基础设施错误，作为结构化 ToolResult 返回 Agent。

### 14.8 新增故障注入测试

在以下每个边界前后终止进程并恢复：

- Operation Ledger prepare 前后；
- 工具产生副作用后、结果入账前；
- ToolResult 入账后、应用到 Agent 消息前；
- Interrupt 保存后、HTTP 响应前；
- 最终消息提交前后；
- outbox 写入后、事件发布前。

验收条件是：不重复副作用、不重复最终消息、不丢失已提交结果，无法确定时稳定进入 `execution_unknown`。

