# 第四层：Domain Agents、Tools 与信息能力详细设计

## 1. 目标与边界

第四层提供受限执行能力，包括领域 Agent 配置、Tool Runtime、Policy Engine、Operation Ledger、Artifact Runtime，以及 Memory、Knowledge、Skills 等信息能力。

核心原则：

- Agent 是配置和职责边界，不是权限边界；
- Policy Engine 是权限规则的权威来源；Tool Runtime 是产生实际副作用前不可绕过的执行检查点；
- 所有具有外部副作用的调用先进入 Operation Ledger；
- 审批绑定不可变操作内容，审批后不得替换参数；
- 信息能力返回有来源、有权限、有时效的引用；
- Skill 不能扩大 Agent 或用户权限。

第四层不创建 Session、不选择 Graph 节点、不委派其他 Agent，也不直接提交最终会话回复。

## 2. Agent Catalog

### 2.1 AgentDescriptor

```python
class AgentDescriptor(BaseModel):
    name: str
    version: str
    description: str
    system_prompt_ref: str
    allowed_tools: set[str]
    context_profile: str
    max_steps_per_task: int
    supported_task_kinds: set[str]
    result_schema: str
    enabled: bool
```

AgentRegistry 在进程启动时加载并验证配置。GraphState 只保存 `name + version`，不保存 Agent 实例。Run 开始后锁定版本；热更新只影响新 Run。

### 2.2 固定 Agent

#### GeneralAgent

面向普通问答、总结、本地只读检查和轻量综合。默认工具：`list_tree`、`read_file`、`file_info`、`search_files`、受限信息检索。

#### ResearchAgent

面向联网检索、来源交叉验证和研究摘要。结果必须区分事实与推断，并包含来源、发布时间或更新时间、不确定性。默认无工作区写权限。

#### CodeDataAgent

面向代码分析、测试、沙箱执行、数据处理和 artifact 生成。所有生成物先写入 Run artifact 区；发布到 workspace 是独立工具操作。

### 2.3 Agent 结果契约

```python
class TaskResult(BaseModel):
    task_id: str
    agent: str
    agent_version: str
    status: Literal["completed", "partial", "failed", "blocked"]
    summary: str
    criterion_results: list[CriterionResult]
    finding_references: list[str]
    artifacts: list[ArtifactReference]
    sources: list[SourceReference]
    errors: list[AgentError]
    recommended_next_action: str | None
```

`completed` 必须逐项满足 success criteria；缺少证据时只能返回 `partial`。结果不得包含隐藏推理或完整工具轨迹。

## 3. Tool Contract

### 3.1 ToolSpec

```python
class ToolSpec(BaseModel):
    name: str
    version: str
    description: str
    input_schema: str
    output_schema: str
    risk_level: Literal["read", "network", "execute", "write", "destructive"]
    side_effect: Literal["none", "idempotent", "compensatable", "non_idempotent"]
    approval_mode: Literal["never", "policy", "always"]
    allowed_agents: set[str]
    timeout_seconds: int
    max_output_bytes: int
    reconciliation_supported: bool
    compensation_tool: str | None
```

`risk_level` 不能由模型指定。工具注册时验证：非幂等副作用工具必须要求审批或提供清晰的业务约束；声明可补偿时必须注册 compensation tool。

### 3.2 ToolCall 与规范化

```python
class ToolCall(BaseModel):
    operation_id: str
    run_id: str
    task_id: str
    requested_by: str
    tool_name: str
    tool_version: str
    arguments: dict
    arguments_hash: str
```

Tool Runtime 使用 input schema 规范化参数后计算 hash。审批、账本和执行都使用规范化后的参数；原始模型参数仅用于诊断，不能直接执行。

路径参数必须解析为规范绝对路径，再验证位于授权根目录。符号链接、大小写、`..`、UNC 路径和平台差异必须在 Policy adapter 中处理。

### 3.3 ToolResult

```python
class ToolResult(BaseModel):
    operation_id: str
    tool_name: str
    status: Literal[
        "succeeded", "failed", "denied", "cancelled",
        "timed_out", "unknown"
    ]
    summary: str
    data_reference: str | None
    artifacts: list[ArtifactReference]
    error: ToolError | None
    started_at: datetime | None
    completed_at: datetime
```

大型输出不得直接进入 `summary`。原始 stdout、网页正文和二进制数据进入受限 blob/artifact store，结果中只保存引用、摘要、大小和内容 hash。

## 4. Policy Engine

### 4.1 一个权威规则源，两个检查职责

Policy 规则只在 Policy Engine 中定义，Graph 和 Tool Runtime 不得各自维护允许列表或审批规则的不同副本。

调用链中保留两个不同职责的检查点：

1. Graph 调用 Policy Engine 获取 `allow/deny/require_approval`，据此选择执行、拒绝或 Interrupt。这是流程检查。
2. Tool Runtime 在 Handler 执行前验证 PolicyDecision、操作参数和 ApprovalGrant 仍然匹配，并在必要时调用同一 Policy Engine 重新评估。这是执行检查。

第二次检查不是第二套 Policy，也不是重复决定业务流程。它保证即使未来出现新的调用入口、恢复路径或编程错误，未经授权的操作仍不能到达 Handler。真正的 Tool Handler 只能由 Tool Runtime 调用。

```python
class PolicyInput(BaseModel):
    principal: Principal
    session_id: str
    run_id: str
    agent_name: str
    tool: ToolSpec
    normalized_arguments: dict
    workspace_roots: list[str]
    grants: list[Grant]

class PolicyDecision(BaseModel):
    decision: Literal["allow", "deny", "require_approval"]
    decision_id: str
    policy_version: str
    reason_code: str
    constraints: dict
```

Tool Runtime 在真正执行前重新验证 decision 与当前操作完全匹配。这是防御式重复校验，不是第二套策略。

### 4.2 审批票据

```python
class ApprovalGrant(BaseModel):
    approval_id: str
    operation_id: str
    principal_id: str
    tool_name: str
    arguments_hash: str
    policy_version: str
    decision: Literal["approved", "rejected"]
    expires_at: datetime
    consumed_at: datetime | None
```

票据一次性消费，并绑定操作、参数 hash、用户和 Policy 版本。参数变化、策略升级、过期或 Run 不匹配均需重新审批。

安全展示数据由工具提供专用 renderer，只展示必要参数，敏感字段脱敏。不得把原始环境变量、密钥或完整文件内容发送给审批界面。

## 5. Operation Ledger

### 5.1 状态模型

```python
class OperationRecord(BaseModel):
    operation_id: str
    run_id: str
    tool_name: str
    tool_version: str
    arguments_hash: str
    state: Literal[
        "prepared", "approved", "running", "succeeded",
        "failed", "cancelled", "unknown", "compensated"
    ]
    attempt: int
    executor_id: str | None
    execution_lease_until: datetime | None
    external_idempotency_key: str
    result_reference: str | None
    error_code: str | None
```

状态转换使用 CAS。相同 `operation_id` 但参数 hash 不同必须返回 `OPERATION_CONFLICT`。

### 5.2 执行流程

```text
normalize arguments
  -> policy decision
  -> ledger.prepare
  -> approval if required
  -> acquire execution lease
  -> handler.execute(operation_id as idempotency key)
  -> ledger.complete
  -> return stable ToolResult
```

重复调用同一 operation：

- `succeeded/failed/denied/cancelled`：直接返回已记录结果；
- `prepared/approved`：允许获得执行租约后继续；
- `running` 且租约有效：返回 in-progress，不启动第二次；
- `running` 且租约过期：进入 reconcile；
- `unknown`：只允许 reconcile、人工处理或补偿，不自动重放非幂等操作。

### 5.3 对账协议

```python
class ReconciliationResult(BaseModel):
    operation_id: str
    status: Literal["succeeded", "failed", "not_executed", "unknown"]
    result_reference: str | None
    evidence: list[str]
```

Handler 可以实现 `reconcile(operation)`。只有确认 `not_executed` 且工具策略允许时才可执行；确认 succeeded 时补记结果；仍 unknown 时 Graph 进入 `execution_unknown`。

### 5.4 幂等等级

- `none`：纯读取，可安全重试，但结果可能随时间变化；
- `idempotent`：相同 operation key 重复执行效果相同；
- `compensatable`：可能重复产生副作用，但存在可审计补偿操作；
- `non_idempotent`：超时或崩溃后禁止自动重放。

“只读”不代表可以无限重试；仍受成本、速率、时效和预算约束。

## 6. Tool Runtime

```python
class ToolRuntime(Protocol):
    async def prepare(
        self,
        call: ToolCallDraft,
        context: ExecutionContext,
    ) -> PreparedOperation: ...

    async def execute(
        self,
        operation_id: str,
        approval: ApprovalGrant | None,
        cancellation: CancellationView,
    ) -> ToolResult: ...

    async def reconcile(self, operation_id: str) -> ReconciliationResult: ...
```

职责顺序不可绕过：加载 ToolSpec、schema 校验、参数规范化、Policy、账本、审批校验、沙箱/超时/取消、执行、输出限制与脱敏、artifact 收集、结果入账、事件写入。

即使 Graph 已做 Policy 路由，直接调用 `execute` 也必须验证 Operation、PolicyDecision 和 ApprovalGrant。

## 7. Sandbox 与代码执行

代码执行工具必须定义：

- 独立工作目录和允许挂载；
- CPU、内存、磁盘、进程数和运行时间限制；
- 默认无网络，网络访问作为独立授权；
- 环境变量白名单和 secret 注入策略；
- 子进程树终止；
- stdout/stderr 大小限制；
- 执行镜像或环境版本记录。

取消时尝试终止进程树，但只有进程退出并完成账本结算后才能声明 cancelled。无法确认外部副作用时返回 unknown。

依赖安装不得复用普通 execute_code；它是独立高风险工具，参数包含包、版本、来源和目标环境，并记录 lockfile 或环境快照。

## 8. Artifact Runtime

### 8.1 生命周期

```text
staged -> validated -> approved(optional) -> published
                  \-> rejected
```

Agent 生成物先写入：

```text
runtime/artifacts/<run_id>/<artifact_id>/
```

ArtifactReference 至少包含 artifact_id、content hash、media type、size、producer operation、validation status 和 storage URI。

### 8.2 发布

发布到 workspace、下载区或外部服务必须使用独立 `publish_artifact` 工具，并进入 Operation Ledger。发布目标使用规范路径；覆盖已有文件、跨根目录写入或外部发布按 Policy 决定是否审批。

验证不得只检查文件存在，还应按类型检查可打开性、结构和渲染结果。验证失败的 artifact 不得标记 published。

## 9. Memory、Knowledge 与 Skills（未来扩展）

本章定义未来兼容边界。当前阶段不实现长期 Memory 自动写入、Knowledge 摄取或向量检索；SQLite Runtime Store 只保存原始对话与系统运行事实。未来实现不得与 Runtime Store 的权威事实表混用。

### 9.1 统一引用模型

```python
class InformationReference(BaseModel):
    reference_id: str
    kind: Literal["memory", "knowledge", "skill", "session", "web"]
    source_uri: str
    source_revision: str | None
    title: str
    excerpt: str
    retrieved_at: datetime
    published_at: datetime | None
    principal_scope: str
    trust_level: str
    content_hash: str | None
```

所有信息能力必须经过 principal 和租户过滤，再返回最小必要 excerpt。结果进入 Context 时保留来源和检索时间。

### 9.2 Memory

- 只从已提交的会话事实生成；
- 迟到、取消和 unknown 操作结果不得写入长期 Memory；
- 写入前进行敏感数据分类和保留期判断；
- 用户可查看、删除或禁用长期 Memory；
- Memory 是可能过期的辅助信息，不能覆盖当前明确输入。

### 9.3 Knowledge

- 文档摄取记录来源、版本、权限和分块策略；
- 检索结果区分原文事实和生成摘要；
- 权限变化后缓存立即失效或按短 TTL 收敛；
- 没有来源的生成内容不得伪装成 Knowledge 事实。

### 9.4 Skills

Skill 包含版本化说明、适用条件、资源引用和建议工具，不包含不可审计的运行时对象。加载 Skill 后的有效工具集合为：

```text
agent.allowed_tools ∩ principal.grants ∩ policy.allowed_tools
```

Skill 声明的工具不参与并集，因此不能扩大权限。Skill 更新只影响新 Task 或明确重新加载后的步骤，并记录版本。

## 10. 安全与数据治理

- Tool 日志、事件和模型上下文统一经过字段级脱敏；
- secret 只通过受控句柄注入，不进入 GraphState、ToolResult 或 artifact metadata；
- 外部内容视为不可信数据，不得改变系统权限；
- SourceReference 和 artifact 必须带租户/用户访问控制；
- 删除 Session 时按保留策略清理 checkpoint、blob、artifact 和 Memory 引用；
- 审计事件追加写，业务用户可见事件与安全审计事件分开存储。

## 11. 可观测性

记录：

- tool_operation_prepared/approved/started/completed/unknown；
- policy_decided/approval_consumed；
- reconciliation_started/completed；
- artifact_staged/validated/published；
- information_retrieved/filtered；
- sandbox_started/terminated/resource_exceeded。

指标至少包括工具成功率、unknown 数量、审批等待时间、对账耗时、重复 operation 命中率、输出截断率和 sandbox 资源使用。

## 12. 测试与验收

### Contract 测试

每个 Tool Handler 必须通过统一测试套件：schema、取消、超时、输出上限、脱敏、幂等键、账本状态、reconcile 声明和 artifact 引用。

### 安全测试

- Agent 越权调用被 Tool Runtime 拒绝；
- 审批后替换参数导致票据失效；
- 路径穿越、符号链接逃逸和未授权网络被拒绝；
- Skill 不能扩大工具集合；
- 不同 principal 无法读取对方信息引用和 artifact。

### 故障测试

- 工具副作用完成但进程在 ledger.complete 前崩溃；
- 执行租约过期后两个 Worker 同时接管；
- 非幂等工具 unknown 时不会自动重放；
- 幂等工具重复 operation 返回同一结果；
- artifact 发布成功但响应丢失时不会重复覆盖。

### 验收标准

- 每次工具执行都有稳定 operation_id 和账本记录；
- Policy 规则只有一个权威来源，Graph 负责路由，Tool Runtime 负责执行前不可绕过的复核；
- 审批与操作内容不可分离；
- unknown 副作用可被显式表示和对账；
- 大型结果不进入 Graph checkpoint；
- Artifact 发布是独立、可审计、幂等的操作；
- Memory、Knowledge 和 Skills 不突破 principal 权限。
