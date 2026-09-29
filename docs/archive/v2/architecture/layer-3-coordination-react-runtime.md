# 第三层：Coordinator 与 ReAct Runtime 详细设计

## 1. 目标与边界

第三层把用户目标转换为受限、可验证、可恢复的执行步骤，包括：

- Coordinator：选择下一项语义动作；
- Planner：按需生成任务依赖图；
- ReAct Runtime：驱动一个领域 Agent 完成一个 Task；
- Context Manager：为每次模型调用构建有界上下文；
- Budget Manager：预留、结算和限制模型调用预算。

第三层不直接执行工具、不修改 Session 生命周期、不判断最终工具权限，也不持久化外部副作用。所有动作必须返回第二层定义的结构化 Command 或 AgentAction。

依赖方向：

```text
Control Graph -> Coordinator / Planner / ReAct Runtime
Coordinator / ReAct Runtime -> Agent Catalog / Context / Model Gateway
Coordinator / ReAct Runtime -X-> Tool Handler / Session Store
```

## 2. Coordinator

### 2.1 输入快照

```python
class CoordinationInput(BaseModel):
    run_id: str
    objective: str
    conversation_summary: str
    plan: TaskPlan | None
    task_results: list[TaskResultReference]
    active_interrupt: InterruptSummary | None
    available_agents: list[AgentDescriptor]
    remaining_budget: BudgetSnapshot
    constraints: list[str]
```

Coordinator 只接收已提交事实的摘要和引用，不接收完整网页、stdout、文件全文或领域 Agent 的隐藏推理。

### 2.2 输出协议

```python
class DelegateCommand(BaseModel):
    type: Literal["delegate"]
    command_id: str
    task: AgentTask

class PlanCommand(BaseModel):
    type: Literal["plan"]
    command_id: str
    reason: str

class ClarifyCommand(BaseModel):
    type: Literal["clarify"]
    command_id: str
    request: ClarificationRequest

class FinalCommand(BaseModel):
    type: Literal["final"]
    command_id: str
    result_references: list[str]
    completion_basis: list[str]

class FailCommand(BaseModel):
    type: Literal["fail"]
    command_id: str
    error: AgentError

CoordinatorCommand = DelegateCommand | PlanCommand | ClarifyCommand | FinalCommand | FailCommand
```

`command_id` 由 Graph 根据输入 revision 派生，不能由模型自由生成。相同输入 revision 重试必须产生相同 ID，便于去重。

### 2.3 确定性校验

模型输出后，代码必须验证：

- `target_agent` 存在且启用；
- AgentTask 的 goal、success criteria 和 constraints 非空且大小受限；
- Delegate 不得指向已经成功完成的同一语义任务；
- Final 必须引用至少一个已提交结果，或明确说明无需执行；
- Clarify 只能询问完成目标所必需且本地上下文无法推断的信息；
- Plan 不得在已有有效计划且没有新事实时重复生成；
- 命令不会突破剩余预算或权限上限。

校验失败时允许一次结构化修复；再次失败进入稳定错误 `COORDINATOR_OUTPUT_INVALID`。

### 2.4 防循环规则

Graph 维护以下确定性计数和指纹：

```python
class CoordinationGuard(BaseModel):
    coordinator_calls: int
    agent_switches: int
    repeated_command_count: int
    last_command_fingerprint: str | None
    completed_task_fingerprints: set[str]
```

相同目标、Agent、输入引用和成功标准构成任务指纹。没有新增事实时连续产生相同命令超过上限，必须停止并返回 partial/blocked，不允许依赖提示词无限循环。

## 3. Planner

### 3.1 使用条件

只有满足至少一个条件时才调用 Planner：

- 任务包含两个以上有依赖的可验证阶段；
- 涉及多领域 Agent；
- 预计成本超过直接委派阈值；
- 用户明确要求先规划；
- 高风险动作前需要展示执行范围。

### 3.2 TaskPlan

```python
class PlanTask(BaseModel):
    task_id: str
    goal: str
    preferred_agent: str
    dependencies: list[str]
    success_criteria: list[str]
    constraints: list[str]
    input_references: list[str]
    risk: Literal["read_only", "network", "execute", "write", "destructive"]
    status: Literal["pending", "ready", "running", "completed", "failed", "blocked", "skipped"]

class TaskPlan(BaseModel):
    plan_id: str
    revision: int
    tasks: list[PlanTask]
    join_groups: list[JoinGroup]
    max_parallel_tasks: int
    completion_criteria: list[str]
```

```python
class JoinGroup(BaseModel):
    group_id: str
    task_ids: list[str]
    policy: Literal["all_success", "all_settled", "min_success", "best_effort"]
    min_success: int | None = None
    cancel_remaining_when_satisfied: bool = False
```

代码校验 task_id 唯一、依赖存在、图无环、至少存在一个根任务、JoinGroup 引用合法、阈值可满足、风险不低于实际能力需求。

### 3.3 DAG 并行调度

Scheduler 根据依赖图选择全部 ready task，并在以下约束内并行启动：

- `max_parallel_tasks`；
- Run 剩余模型、工具、token 和 wall-time 预算；
- 每个 Agent 的并发配额；
- Sandbox、网络和外部服务的资源配额；
- Task 之间声明的资源互斥键，例如同一 workspace 写区域。

每个并行任务拥有独立的 `TaskExecution`、Agent 消息、预算 reservation、CancellationToken、工具 operation 和 checkpoint。任务不得直接读取其他运行中任务的可变状态，只能读取 fork 前已提交输入；跨任务数据通过已提交 TaskResultReference 传递。

父 Scheduler 负责：

1. 原子地将 ready task 转为 running 并创建子执行租约；
2. 驱动各子 Graph，允许不同 Worker 接管；
3. 接收子任务终态事件并更新依赖 readiness；
4. 按 join policy 收敛结果；
5. 将父 Run 取消传播到全部子任务；
6. 对失败、blocked 和 execution_unknown 执行 fail-fast、all-settled 或人工处理策略。

实现可分阶段进行：第一阶段令 `max_parallel_tasks = 1` 验证完整 TaskExecution/fork/join 协议；第二阶段提高并发度并启用资源互斥。阶段差异仅是调度配置，不改变数据模型和外部契约。

### 3.4 Replan

只有出现新事实、任务失败、依赖失效或用户修改目标时才允许 replan。Replan 产生新 revision，已完成任务不可删除或改写，只能保留引用；正在执行的任务必须先收敛或取消。

## 4. ReAct Runtime

### 4.1 单步协议

ReAct Runtime 每次只执行一次模型调用并返回一个动作：

```python
class AgentStepInput(BaseModel):
    run_id: str
    task: AgentTask
    agent: AgentDescriptor
    messages: list[ModelMessage]
    context: AgentContext
    remaining_budget: BudgetSnapshot
    previous_step_id: str | None

class AgentStepResult(BaseModel):
    step_id: str
    action: AgentAction
    usage: ModelUsage
    context_manifest_id: str
```

`step_id` 由 `run_id + task_id + task_attempt + step_number` 派生。Runtime 内部不得包含跨多个工具调用的 while 循环；循环由 Graph 的 checkpoint 边表达。

### 4.2 动作约束

AgentAction 只能是：

- 一个 ToolAction；
- CompleteAction；
- TransferAction；
- ClarifyAction；
- FailAction。

一次输出包含多个动作时视为 schema 错误。Agent 不能直接执行工具、创建另一个 Agent、扩大工具集合、修改预算或提交最终会话消息。

CompleteAction 必须包含与 success criteria 对应的证据引用。Runtime 进行结构校验，Coordinator 判断该结果是否足以满足更高层目标。

### 4.3 工具结果反馈

ToolResult 转换为模型消息前必须：

- 保留 `operation_id`、tool name、状态和错误码；
- 对 content 做长度限制和脱敏；
- 大结果替换为 artifact/reference 加摘要；
- 明确区分权限拒绝、参数错误、执行失败和结果未知；
- 不把 Policy 内部规则或敏感审计数据暴露给模型。

`execution_unknown` 不转换成普通失败；Agent 只能建议对账或请求用户处理，不能再次调用相同写操作。

## 5. Context Manager

### 5.1 上下文清单

每次模型调用生成不可变 ContextManifest：

```python
class ContextManifest(BaseModel):
    manifest_id: str
    run_id: str
    task_id: str | None
    profile: str
    references: list[ContextReference]
    summaries: list[SummaryReference]
    token_estimate: int
    redaction_policy_version: str
```

Manifest 保存引用和摘要，不复制大型源内容。它用于复现“模型当时看到了什么”，但不记录隐藏推理。

### 5.2 构建顺序

上下文按以下优先级装配：

1. 系统安全约束和 Agent 身份；
2. 当前目标、Task 和 success criteria；
3. 最近未完成的工具调用对；
4. 最近必要会话；
5. 已完成任务摘要和显式输入引用；
6. 按需检索的 Memory、Knowledge 和 Skill；
7. 低优先级历史摘要。

超过预算时先移除低优先级历史，再压缩已完成轨迹。安全约束、当前 Task、未完成工具调用和用户最新消息不可被压缩掉。

### 5.3 数据隔离

- Coordinator Context 不包含完整领域工作轨迹；
- Agent 只能读取 AgentTask 中授权的 reference；
- Memory/Knowledge 检索结果携带来源、租户、权限和时效元数据；
- Skill 只能提供指令和资源，不能扩大 Agent 权限；
- Context 缓存键必须包含 principal、policy version 和 source revision。

## 6. Model Gateway

第三层通过统一 ModelGateway 调用模型：

```python
class ModelGateway(Protocol):
    async def generate_structured(
        self,
        *,
        request_id: str,
        messages: list[ModelMessage],
        output_schema: type[BaseModel],
        timeout_seconds: int,
        cancellation: CancellationView,
    ) -> ModelResult: ...
```

职责包括超时、模型选择、结构化输出、usage 归一化、有限重试、取消检查和可观测事件。API key、SDK Client 和 provider 原始响应不得进入 GraphState。

模型请求可以重试，但只有通过 schema 校验的结果进入 checkpoint。Provider 是否实际完成未知时，重复模型调用允许产生不同文本，因为模型调用本身无外部副作用；预算按实际已确认 usage 结算，未知 usage 使用保守估计。

## 7. Budget Manager

预算采用“调用前预留、调用后结算”：

```python
class BudgetReservation(BaseModel):
    reservation_id: str
    run_id: str
    category: Literal["coordinator", "planner", "agent", "compression"]
    estimated_tokens: int
    expires_at: datetime
```

同一个 step 重试复用 reservation。预算耗尽时返回结构化 `BUDGET_EXHAUSTED`，包含已完成内容、未完成内容和继续方式，不得伪装为成功。

## 8. 错误与降级

| 错误 | 行为 |
|---|---|
| 结构化输出失败 | 同 request 修复一次，随后失败 |
| Coordinator 重复委派 | guard 拒绝并生成 partial/blocked |
| Planner 产生环 | 拒绝计划，允许一次修复 |
| Context 超限 | 按优先级压缩；仍超限则失败 |
| Model 429/临时 5xx | 有界退避或允许的备用模型 |
| Model 超时 | 记录未知 usage，按预算决定是否重试 |
| 取消 | 立即停止接纳结果，不重试 |

## 9. 可观测性

记录：

- coordination_started/decided/rejected；
- plan_created/revised/task_selected；
- agent_step_started/completed/rejected；
- context_manifest_created/compressed；
- model_call_started/completed/failed；
- budget_reserved/settled/exhausted。

事件只记录摘要、ID、耗时、usage 和错误码，不记录隐藏推理、密钥或完整敏感上下文。

## 10. 测试与验收

### 单元测试

- 所有 CoordinatorCommand schema 和 guard；
- 任务指纹稳定且能阻止重复委派；
- 计划 DAG 校验、ready 选择和 replan；
- 每个 Agent step 只接受一个动作；
- Context 优先级、压缩保护项和权限过滤；
- 预算预留与重复 step 结算。

### 集成测试

- Coordinator -> Agent -> ToolResult -> Coordinator 完整闭环；
- 无依赖任务可并行，有依赖任务只在前置结果提交后启动；
- 并发任务上下文、预算、ToolCall 和 checkpoint 相互隔离；
- join policy 在 partial、failed 和 cancellation 下能够确定性收敛；
- clarification 后使用新上下文重新协调；
- `execution_unknown` 不会触发重复写操作；
- 在每个模型结果 checkpoint 前后崩溃，恢复后状态一致。

### 验收标准

- 第三层不能绕过 Graph 或直接执行工具；
- 相同已提交输入不会无限产生相同委派；
- Planner 计划可验证、可修订并支持 DAG fork/join；
- Agent 一步只产生一个结构化动作；
- 上下文有来源、有权限边界、有大小上限；
- 所有模型调用受预算、取消和观测约束。
