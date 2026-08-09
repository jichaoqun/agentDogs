# 第一层：Session Runtime 详细设计

## 1. 目标

Session Runtime 是整个系统的运行隔离层，目标是确保：

- 不同会话互不污染并可并行运行；
- 同一会话中的任务按确定顺序执行；
- 每次请求都有独立、可追踪的 Run；
- 取消、恢复和迟到结果不会破坏会话；
- API、GUI 和 Graph 使用统一的会话状态；
- SQLite3 从首个可运行版本开始就是权威 Runtime Store；内存实现只用于单元测试，不作为应用运行模式。

本层不理解用户任务语义，不调用 LLM 判断任务类型，也不决定使用哪个专业 Agent。

## 2. 核心概念

### 2.1 Session

Session 表示一段持续对话，是消息、任务和长期会话状态的归属边界。

```python
class SessionRecord(BaseModel):
    session_id: str
    title: str
    status: SessionStatus
    active_run_id: str | None
    created_at: datetime
    updated_at: datetime
    version: int
```

### 2.2 Run

Run 表示一次用户输入触发的完整执行过程。一个 Session 可以依次包含多个 Run，但同一时刻最多存在一个活动 Run。

```python
class RunRecord(BaseModel):
    run_id: str
    session_id: str
    status: RunLifecycleStatus
    user_message_id: str
    checkpoint_id: str | None
    cancellation_requested: bool
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None
```

### 2.3 Message

主会话消息只保存用户可感知的对话，不保存领域 Agent 的全部内部推理轨迹。

```python
class SessionMessage(BaseModel):
    message_id: str
    session_id: str
    run_id: str | None
    role: Literal["user", "assistant", "system"]
    content: str
    status: Literal["committed", "pending", "cancelled"]
    metadata: dict
    created_at: datetime
```

### 2.4 Event

Event 是运行事实，用于 GUI 调试、审计和问题诊断，不直接作为主会话消息。

```python
class RuntimeEvent(BaseModel):
    event_id: str
    session_id: str
    run_id: str
    sequence: int
    type: str
    payload: dict
    created_at: datetime
```

## 3. 状态设计

### 3.1 SessionStatus

```python
SessionStatus = Literal[
    "idle",
    "running",
    "waiting_user",
    "cancelling",
]
```

含义：

| 状态 | 含义 | 是否允许新消息 |
|---|---|---|
| `idle` | 没有活动 Run | 是 |
| `running` | Graph 正在执行 | 否 |
| `waiting_user` | 等待审批或澄清 | 否，只允许 Resume/Cancel |
| `cancelling` | 已请求取消，等待执行退出 | 否 |

### 3.2 状态转换

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Running: submit_message
    Running --> WaitingUser: graph_interrupt
    WaitingUser --> Running: resume
    Running --> Cancelling: cancel
    WaitingUser --> Cancelling: cancel
    Cancelling --> Idle: run_stopped
    Running --> Idle: completed
    Running --> Idle: failed
```

Session Runtime 只允许表中定义的转换。非法转换返回稳定错误码，而不是尝试自动修正。

## 4. 并发模型

### 4.1 每会话串行

每个 Session 拥有一个逻辑 mailbox 或锁：

```python
class LiveSession:
    lock: asyncio.Lock
    active_run_id: str | None
    cancellation_token: CancellationToken | None
```

同一会话：

```text
Run A 完成/取消
    → Run B 才能开始
```

不同会话：

```text
Session A / Run A ─┐
Session B / Run B ─┼─ 可以并行
Session C / Run C ─┘
```

### 4.2 乐观版本

持久化更新携带 `version`：

```sql
UPDATE sessions
SET status = ?, active_run_id = ?, version = version + 1
WHERE session_id = ? AND version = ?
```

更新行数为零表示发生并发冲突，必须重新加载，不得覆盖。

### 4.3 迟到结果

所有模型、工具和子任务结果在写入前检查：

```python
if result.run_id != session.active_run_id:
    return DiscardedResult(reason="stale_run")
```

迟到结果不得进入：

- conversation messages；
- completed tasks；
- memory；
-最终回答；
-当前 Graph checkpoint。

## 5. 取消设计

### 5.1 CancellationToken

```python
class CancellationToken:
    run_id: str
    _event: threading.Event

    def cancel(self) -> None: ...
    def is_cancelled(self) -> bool: ...
    def raise_if_cancelled(self) -> None: ...
```

### 5.2 取消流程

```mermaid
sequenceDiagram
    participant UI
    participant SR as SessionRuntime
    participant G as Graph
    participant T as Tool/Model

    UI->>SR: cancel(session_id, run_id)
    SR->>SR: status = cancelling
    SR->>SR: token.cancel()
    SR->>G: CancellationRequested
    G->>T: stop/discard
    T-->>G: stopped or late result
    G-->>SR: RunCancelled
    SR->>SR: active_run_id = null
    SR->>SR: status = idle
```

模型 SDK 无法真正取消 HTTP 请求时，可以放弃等待，但必须丢弃响应。Sandbox 和本地进程必须尝试终止进程树。

## 6. Interrupt与Resume

Graph 产生 interrupt 时，Session Runtime：

1. 保存 checkpoint；
2. 将 Session 状态设置为 `waiting_user`；
3. 返回 `interrupt_id` 和安全展示数据；
4. 仅接受匹配的 Resume 或 Cancel。

Resume 必须校验：

- session_id；
- active_run_id；
- interrupt_id；
- interrupt_type；
- payload schema；
- checkpoint 是否仍有效。

```python
class ResumeRequest(BaseModel):
    session_id: str
    run_id: str
    interrupt_id: str
    type: str
    payload: dict
```

## 7. SessionStore接口

业务层不得直接操作全局字典。

```python
class SessionStore(Protocol):
    def create_session(self, title: str) -> SessionRecord: ...
    def get_session(self, session_id: str) -> SessionRecord | None: ...
    def list_sessions(self) -> list[SessionRecord]: ...
    def delete_session(self, session_id: str) -> bool: ...

    def begin_run(
        self,
        session_id: str,
        user_message: SessionMessage,
    ) -> RunRecord: ...

    def set_session_status(
        self,
        session_id: str,
        expected_version: int,
        status: SessionStatus,
        active_run_id: str | None,
    ) -> SessionRecord: ...

    def append_message(self, message: SessionMessage) -> None: ...
    def append_event(self, event: RuntimeEvent) -> None: ...
    def finish_run(self, run_id: str, status: str) -> None: ...
```

应用实现：

```text
SQLiteSessionStore
```

测试替身：

```text
InMemorySessionStore（仅用于不涉及崩溃恢复和并发一致性的单元测试）
```

SQLite 的 schema、事务、迁移、备份和数据生命周期见 [SQLite Runtime Store 设计](persistence-sqlite-runtime-store.md)。

## 8. 对外接口

```python
class SessionRuntime:
    async def create_session(self, title: str = "新会话") -> SessionView: ...
    async def submit_message(
        self,
        session_id: str,
        message: str,
        options: RunOptions,
    ) -> RunResponse: ...
    async def resume(self, request: ResumeRequest) -> RunResponse: ...
    async def cancel(self, session_id: str, run_id: str) -> CancelResponse: ...
    async def get_session(self, session_id: str) -> SessionView: ...
```

HTTP 层只做：

- Schema 校验；
-调用 SessionRuntime；
-将领域异常映射为 HTTP 状态码。

HTTP 层不直接调用 Agent。

## 9. 错误模型

稳定错误码：

```text
SESSION_NOT_FOUND
SESSION_BUSY
RUN_NOT_FOUND
RUN_MISMATCH
RUN_CANCELLING
INTERRUPT_NOT_FOUND
INTERRUPT_EXPIRED
INVALID_RESUME_PAYLOAD
CONCURRENT_UPDATE
STORE_FAILURE
```

异常消息可以本地化，但错误码不得随文案变化。

## 10. 观测要求

必须记录：

- session_created；
- run_started；
- session_status_changed；
- interrupt_created；
- run_resumed；
- cancellation_requested；
- stale_result_discarded；
- run_completed；
- run_failed。

日志不得包含：

- API key；
-完整敏感文件内容；
-未经脱敏的环境变量；
-执行脚本中的秘密。

## 11. 测试要求

### 单元测试

- 状态转换表；
-重复 begin_run；
-错误 run_id 取消；
-错误 interrupt_id Resume；
-迟到结果丢弃；
-乐观版本冲突；
-删除活动会话。

### 并发测试

- 同会话两个消息同时提交，只有一个成功；
-不同会话可以同时运行；
- Cancel 后立即发送新消息会被拒绝；
-旧模型结果在新 Run 开始后返回，不得写入。

### 持久化测试

- checkpoint 保存与读取；
-进程模拟重启后等待审批状态仍存在；
-已完成 Run 不会重新执行。

## 12. 验收标准

- 同一 Session 不发生并发状态写入；
-不同 Session 可并行；
-取消后旧结果绝不进入主对话；
-等待用户时只能 Resume 或 Cancel；
-存储实现可替换；
-API 层不持有 Agent 实例状态；
-所有状态变化都有事件记录。

## 13. 一致性与故障恢复修订

本章是第一层的强制实现约束。前文中与本章冲突的示例接口，以本章为准。

### 13.1 正确性边界

`asyncio.Lock` 只用于减少单进程内的竞争，不承担跨进程正确性。正确性必须由持久化存储保证：

- 每个 Session 最多存在一个非终态 Run；
- Run 的每次状态提交都使用 `run_id + revision` 比较并交换；
- Worker 必须持有未过期租约才可驱动 Run；
- Session、Run、Message、Checkpoint 和 Outbox Event 的关联修改必须原子提交；
- API 重试必须通过 `idempotency_key` 返回同一个 Run，而不是创建新 Run。

数据库应至少具有以下约束：

```sql
UNIQUE(session_id) WHERE terminal = false
UNIQUE(session_id, idempotency_key)
UNIQUE(run_id, revision)
UNIQUE(run_id, message_kind) WHERE message_kind = 'final_assistant'
```

若存储不支持部分唯一索引，必须使用 Session 行上的 `active_run_id` CAS 在同一事务中实现等价约束。

### 13.2 Run 租约

```python
class RunLease(BaseModel):
    run_id: str
    worker_id: str
    lease_token: str
    lease_until: datetime
    heartbeat_at: datetime

class RunRecord(BaseModel):
    run_id: str
    session_id: str
    status: RunLifecycleStatus
    revision: int
    worker_id: str | None
    lease_token: str | None
    lease_until: datetime | None
    heartbeat_at: datetime | None
    cancellation_requested: bool
    recovery_attempts: int
    checkpoint_id: str | None
```

规则：

1. Worker 在执行 Graph 节点前获取或续约租约。
2. 节点提交必须同时校验 `expected_revision`、`worker_id` 和 `lease_token`。
3. 租约过期后，原 Worker 的迟到提交一律拒绝。
4. 新 Worker 只能从最近一次已提交 checkpoint 接管。
5. 租约不是工具副作用的互斥锁；工具副作用由第四层的 Operation Ledger 保证。

租约操作必须由 Store 提供原子接口：

```python
class RunLeaseStore(Protocol):
    def acquire_run_lease(
        self, run_id: str, expected_revision: int, worker_id: str, ttl_seconds: int
    ) -> RunLease: ...

    def renew_run_lease(
        self, run_id: str, expected_revision: int, worker_id: str,
        lease_token: str, ttl_seconds: int
    ) -> RunLease: ...

    def take_over_expired_run(
        self, run_id: str, expected_revision: int, worker_id: str,
        now: datetime, ttl_seconds: int
    ) -> RunLease: ...
```

每次 acquire/takeover 生成新的高熵 `lease_token`。renew 保持 token 不变。节点提交同时匹配 `run_id + revision + worker_id + lease_token + lease_until > now`；任一条件不满足都返回 `LEASE_LOST`，不得尝试覆盖。

`recovery_attempts` 只在 takeover 成功时递增。超过配置的 `max_recovery_attempts` 后，恢复器将 Run 收敛到 `failed` 或 `execution_unknown`，并使 Session 回到 `idle` 或 `waiting_user`，不能永久占用 Session。

### 13.3 原子存储接口

原有细粒度 `append_message`、`finish_run` 仅可作为事务内部 repository 方法，业务层不得组合调用。业务层只使用以下原子操作：

```python
class SessionStore(Protocol):
    def begin_run(
        self,
        *,
        session_id: str,
        expected_session_version: int,
        user_message: SessionMessage,
        idempotency_key: str,
    ) -> RunSnapshot: ...

    def commit_step(
        self,
        *,
        run_id: str,
        expected_revision: int,
        lease_token: str,
        checkpoint: GraphCheckpoint,
        events: list[RuntimeEvent],
    ) -> RunSnapshot: ...

    def commit_interrupt(
        self,
        *,
        run_id: str,
        expected_revision: int,
        lease_token: str,
        checkpoint: GraphCheckpoint,
        interrupt: InterruptRecord,
        events: list[RuntimeEvent],
    ) -> RunSnapshot: ...

    def complete_run(
        self,
        *,
        run_id: str,
        expected_revision: int,
        lease_token: str,
        assistant_message: SessionMessage,
        result: RunResultRecord,
        events: list[RuntimeEvent],
    ) -> RunSnapshot: ...
```

每个操作在一个事务中更新 Run、Session、checkpoint、消息和 outbox。事件发布失败不得回滚业务事实；后台 publisher 从 outbox 重试发布。

### 13.4 Interrupt 幂等

`ResumeRequest` 增加 `idempotency_key`。Interrupt 记录包含状态 `open/consumed/cancelled/expired`、`payload_schema_version` 和 `checkpoint_revision`。

同一个 Resume 重试时返回第一次处理结果；不同 payload 使用同一 `idempotency_key` 时返回 `IDEMPOTENCY_CONFLICT`。消费 Interrupt 和提交恢复后的 checkpoint 必须在一个事务中完成。

### 13.5 重启恢复矩阵

| 持久状态 | 恢复动作 |
|---|---|
| `initializing/coordinating/running_tasks/planning/compressing/composing_final` | 从最近 checkpoint 重新执行无副作用节点；running_tasks 按各 TaskExecution checkpoint 分别接管 |
| `waiting_approval/waiting_clarification` | 恢复 Interrupt，继续等待用户 |
| `tool_preparing/tool_executing/tool_reconciling` | 查询 Operation Ledger，禁止直接重放 |
| `cancelling` | 重建取消意图，终止可定位的执行并等待租约收敛 |
| `execution_unknown` | 执行工具对账；无法确认时请求人工处理 |
| 终态 | 不重新执行，只补发未发布 outbox event |

启动恢复器扫描租约过期的非终态 Run，执行 CAS 接管。超过 `max_recovery_attempts` 后进入 `failed` 或 `execution_unknown`，不得无限恢复。

### 13.6 取消的持久语义

取消请求必须先原子写入 `cancellation_requested = true` 和事件，再通知内存 Token。进程重启后，新 Worker 从持久字段重建 Token。

取消是协作式的，不等同于副作用回滚。已经成功的工具操作保留在账本中；需要回滚时必须调用显式补偿工具。无法确认工具状态时 Run 进入 `execution_unknown`，不能直接回到 `idle`。

### 13.7 新增测试

- 两个进程同时 `begin_run`，只产生一个 Run；
- 同一 `idempotency_key` 重试返回相同 Run；
- Worker 租约过期后迟到提交被拒绝；
- 每个原子提交点前后强制崩溃，恢复后消息和事件不重复；
- Resume 请求重复提交只消费一次 Interrupt；
- 重启后 `cancelling` Run 可以继续收敛；
- 工具执行状态恢复时不会未经对账重放工具。

