# SQLite Runtime Store 详细设计

## 1. 定位

SQLite Runtime Store 是 Agent Dogs 桌面应用的本地权威事实库，保存：

- 原始用户和 Assistant 对话消息；
- Session 与 Run 生命周期；
- Planner 计划和 TaskExecution 状态；
- Graph checkpoint 与 Interrupt；
- Tool Operation Ledger 与审批记录；
- Artifact 元数据；
- Runtime 事件、错误和预算用量；
- 非敏感系统配置、schema 版本和恢复信息。

它不等同于：

- **Memory**：模型从历史中筛选出的长期事实；
- **Knowledge Base**：经过摄取、分块和索引的外部资料；
- **日志文件**：面向运维诊断的文本日志；
- **Artifact Blob Store**：大型文件、网页正文、stdout 或二进制内容。

当前阶段只实现 Runtime Store。Memory 和 Knowledge 仅保留接口边界，不自动提取、不建立向量索引，也不从原始对话隐式生成。

## 2. 为什么使用 SQLite3

SQLite 适合单机桌面 Runtime：

- 无独立数据库服务；
- 数据库是单个可备份文件；
- 支持事务、外键、唯一约束和崩溃恢复；
- WAL 模式支持多个读取者与一个写入者；
- Python 标准库可直接使用 `sqlite3`；
- 后续可通过 Repository 接口迁移，而不让业务层依赖 SQL。

SQLite 的限制也必须接受：同一时刻只有一个 Writer。高并发 TaskExecution 的结果可以并行产生，但最终通过短事务串行提交；不得在数据库事务中等待模型、工具、网络或用户审批。

## 3. 数据库文件

建议目录：

```text
runtime/
  data/
    agent_dogs.db
    backups/
  blobs/
  artifacts/
```

数据库路径由应用数据目录决定，不放进用户 workspace。开发环境允许配置覆盖，但必须使用规范绝对路径。

大型内容不直接写入 SQLite：

- 小型结构化 payload 使用 JSON text；
- 大型 stdout、网页、文件快照和二进制写入 blob/artifact store；
- SQLite 保存 URI、内容 hash、大小、media type 和访问范围。

## 4. 连接配置

每个数据库连接初始化时执行：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

说明：

- `foreign_keys=ON` 必须在每个连接设置；
- WAL 提高读写并发，但不能把 SQLite 变成多 Writer 数据库；
- 普通桌面运行使用 `synchronous=NORMAL`；明确要求更强掉电保障时可配置为 `FULL`；
- `busy_timeout` 只处理短暂锁竞争，超过后返回稳定错误，不无限等待。

使用一个应用级 Writer Queue 或受控连接池。读连接可以多个，写事务应短小，并禁止跨 `await` 持有。

## 5. 核心 Schema

### 5.1 元数据和系统配置

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE runtime_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
```

`runtime_settings` 只保存非敏感配置，例如默认模型别名、并发度、UI 偏好和最近 workspace。API key、refresh token 和系统凭据必须进入操作系统凭据存储，不得明文写入 SQLite。

### 5.2 Principal、Workspace 与权限

```sql
CREATE TABLE principals (
    principal_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    os_user_fingerprint TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,
    owner_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    canonical_root TEXT NOT NULL,
    root_identity TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_principal_id, root_identity)
);

CREATE TABLE permission_grants (
    grant_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    workspace_id TEXT REFERENCES workspaces(workspace_id),
    capability TEXT NOT NULL,
    effect TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    lifetime TEXT NOT NULL,
    session_id TEXT,
    task_id TEXT,
    policy_version TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`Principal` 是权限主体，不等同于 Agent。桌面 GUI、CLI 和未来远程入口分别建立认证上下文，但映射到稳定的本地用户 principal。`root_identity` 使用规范路径和操作系统文件身份生成，不能只依赖大小写敏感的路径字符串。

`PermissionGrant` 表示某 principal 在 scope 内拥有某 capability；`ApprovalGrant` 只表示对单个不可变 operation 的审批。两者不能混用。Grant 撤销采用 `revoked_at + revision`，Policy cache key 必须包含 grant revision。

### 5.3 Session、Run 与消息

```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    workspace_id TEXT REFERENCES workspaces(workspace_id),
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    active_run_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    worker_id TEXT,
    lease_token TEXT,
    lease_until TEXT,
    heartbeat_at TEXT,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    cancellation_requested INTEGER NOT NULL DEFAULT 0,
    checkpoint_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(session_id, idempotency_key)
);

CREATE TABLE run_outcomes (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    completed_task_ids_json TEXT NOT NULL DEFAULT '[]',
    incomplete_task_ids_json TEXT NOT NULL DEFAULT '[]',
    blocking_reasons_json TEXT NOT NULL DEFAULT '[]',
    result_references_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    run_id TEXT REFERENCES runs(run_id),
    role TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    content TEXT NOT NULL,
    content_format TEXT NOT NULL DEFAULT 'text',
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX one_final_message_per_run
ON messages(run_id, message_kind)
WHERE message_kind = 'final_assistant';
```

消息保存原始用户输入和最终 Assistant 输出。领域 Agent 的内部工作消息不复制到主对话表，只保存摘要或独立引用。

### 5.4 计划和并行任务

```sql
CREATE TABLE plans (
    plan_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    revision INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, revision)
);

CREATE TABLE task_executions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    agent_name TEXT,
    agent_version TEXT,
    checkpoint_id TEXT,
    pending_operation_id TEXT,
    result_reference TEXT,
    lease_owner TEXT,
    lease_until TEXT,
    budget_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, task_id, attempt)
);
```

从 M1 开始，每个 Run 在 `begin_run` 事务中创建正式 TaskExecution：`task_id='root', attempt=1`。没有 Planner 时所有模型步骤、checkpoint 和工具调用都归属 root task。M7 只增加 DAG 中的任务数量，不改变基本表、外键和身份语义。

不同 TaskExecution 可由不同 Worker 运行，但各自使用 `revision` CAS；修改父 DAG readiness 或 join 状态时同时更新 Run revision。

### 5.5 Checkpoint 与 Interrupt

```sql
CREATE TABLE checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    task_id TEXT,
    task_attempt INTEGER,
    run_revision INTEGER NOT NULL,
    task_revision INTEGER,
    state_json TEXT NOT NULL,
    state_schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE interrupts (
    interrupt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    task_id TEXT,
    task_attempt INTEGER,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_schema_version INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    resume_idempotency_key TEXT,
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
    expires_at TEXT,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
```

Checkpoint 只保存恢复所需的小型状态与引用，不保存 SDK Client、进程句柄、完整网页、完整 stdout 或二进制文件。

### 5.6 工具账本与审批

```sql
CREATE TABLE tool_operations (
    operation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    task_id TEXT NOT NULL,
    task_attempt INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    executor_id TEXT,
    execution_lease_until TEXT,
    idempotency_class TEXT NOT NULL,
    reconciliation_strategy TEXT NOT NULL,
    external_idempotency_key TEXT,
    compensation_tool TEXT,
    result_reference TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id, task_id, task_attempt)
        REFERENCES task_executions(run_id, task_id, attempt)
);

CREATE TABLE approval_grants (
    approval_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES tool_operations(operation_id),
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    arguments_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    decision TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
```

Operation Ledger 是 Runtime Store 的一部分，不是日志。`operation_id` 始终存在，表示系统内部身份；`external_idempotency_key` 只有外部系统真正支持时才填写。`idempotency_class` 和 `reconciliation_strategy` 决定超时、崩溃和 unknown 后能否重试。

### 5.7 Artifact、事件与 Outbox

```sql
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    task_id TEXT,
    task_attempt INTEGER,
    producer_operation_id TEXT,
    storage_uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE runtime_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    sequence INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);

CREATE TABLE outbox_events (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT
);
```

`runtime_events` 是可查询的运行事实；`outbox_events` 用于事务提交后可靠通知 GUI 或事件订阅者。两者不能用普通文本日志替代。

## 6. 事务边界

以下操作必须各自在一个 SQLite 事务内完成：

### begin_run

- CAS 更新 Session 为 running；
- 插入 Run；
- 插入正式 root TaskExecution（`task_id='root', attempt=1`）；
- 插入原始用户消息；
- 插入 RunStarted event 与 outbox。

### commit_task_step

- 校验 Run/Task lease 和 revision；
- 插入 checkpoint；
- 更新 TaskExecution；
- 更新预算计数；
- 插入事件与 outbox。

### commit_interrupt

- 插入 checkpoint 和 Interrupt；
- 更新 Task 与父 Run 聚合状态；
- 插入事件与 outbox。

### complete_run

- 插入唯一最终 Assistant 消息；
- 插入唯一 RunOutcome；
- 更新 Run 为终态；
- 清除 Session.active_run_id；
- 更新 Session 状态和 version；
- 插入 RunCompleted event 与 outbox。

事务使用 `BEGIN IMMEDIATE` 尽早发现 Writer 竞争。事务函数不调用 LLM、不运行工具、不读写大型 artifact，也不等待用户。

### Run Lease 原子操作

获取空闲租约：

```sql
UPDATE runs
SET worker_id = :worker_id,
    lease_token = :new_lease_token,
    lease_until = :lease_until,
    heartbeat_at = :now,
    revision = revision + 1
WHERE run_id = :run_id
  AND revision = :expected_revision
  AND (lease_token IS NULL OR lease_until <= :now)
  AND status NOT IN ('completed', 'cancelled', 'failed');
```

续约：

```sql
UPDATE runs
SET lease_until = :lease_until,
    heartbeat_at = :now,
    revision = revision + 1
WHERE run_id = :run_id
  AND revision = :expected_revision
  AND worker_id = :worker_id
  AND lease_token = :lease_token
  AND lease_until > :now;
```

接管过期 Run：

```sql
UPDATE runs
SET worker_id = :new_worker_id,
    lease_token = :new_lease_token,
    lease_until = :lease_until,
    heartbeat_at = :now,
    recovery_attempts = recovery_attempts + 1,
    revision = revision + 1
WHERE run_id = :run_id
  AND revision = :expected_revision
  AND lease_until <= :now
  AND recovery_attempts < :max_recovery_attempts
  AND status NOT IN ('completed', 'cancelled', 'failed');
```

三种操作都要求更新行数恰好为 1，否则返回 `LEASE_CONFLICT`。每次 acquire/takeover 使用新的不可预测 token；节点提交重复同样的 worker/token/revision 条件。

## 7. Repository 边界

业务代码不得直接拼接 SQL：

```python
class RuntimeStore(Protocol):
    sessions: SessionRepository
    runs: RunRepository
    tasks: TaskExecutionRepository
    checkpoints: CheckpointRepository
    interrupts: InterruptRepository
    operations: OperationRepository
    artifacts: ArtifactRepository
    events: EventRepository

    def transaction(self) -> RuntimeTransaction: ...
```

事务用例由 Store 暴露为 `begin_run`、`commit_task_step`、`commit_interrupt`、`complete_run` 等高层方法。Repository 的细粒度方法仅能在事务内部调用。

SQLite 行转换为领域模型后才能离开 persistence 模块。其他层不能依赖 SQLite row、SQL 字段名或连接对象。

## 8. Schema 迁移

- 所有 schema 变化使用只前进 migration；
- 启动时在独占迁移锁中按版本执行；
- migration 文件有不可变 checksum；
- 应用版本不认识更高 schema 时拒绝启动写模式；
- checkpoint 的 `state_schema_version` 与数据库 schema 分开管理；
- 破坏性迁移前自动创建一致性备份。

开发期间也不得直接修改已发布 migration，应新增下一版本。

## 9. 数据安全

- 数据库文件和备份使用当前用户可访问的应用数据目录 ACL；
- API key、OAuth token、系统密码使用 Windows Credential Manager 等 OS 凭据存储；
- 事件和 metadata 写入前脱敏；
- 对话导出与删除是显式用户功能；
- 如果产品需要数据库静态加密，应选用经过维护的 SQLite 加密方案，并独立管理密钥；不能把加密密钥放在同一数据库旁边；
- 不宣称普通 SQLite 文件本身提供加密。

## 10. 生命周期、删除与备份

Session 默认软删除，后台清理任务按外键顺序删除关联 checkpoint、event、operation 和 artifact 引用，再删除外部 blob。清理过程可恢复且有审计事件。

备份使用 SQLite Online Backup API 或等价一致性快照，不能在 WAL 活跃时简单复制单个 `.db` 文件。恢复前校验：

- `PRAGMA integrity_check`；
- schema version；
- artifact/blob 引用存在性；
- 非终态 Run 的 lease 和恢复状态。

## 11. 性能与容量

建立索引：

```sql
CREATE INDEX idx_runs_session_created ON runs(session_id, created_at);
CREATE INDEX idx_grants_principal_capability ON permission_grants(principal_id, capability, revoked_at, expires_at);
CREATE INDEX idx_workspaces_owner ON workspaces(owner_principal_id, status);
CREATE INDEX idx_messages_session_created ON messages(session_id, created_at);
CREATE INDEX idx_tasks_run_status ON task_executions(run_id, status);
CREATE INDEX idx_operations_run_state ON tool_operations(run_id, state);
CREATE INDEX idx_interrupts_run_status ON interrupts(run_id, status);
CREATE INDEX idx_events_run_sequence ON runtime_events(run_id, sequence);
CREATE INDEX idx_outbox_pending ON outbox_events(status, next_attempt_at);
```

对话列表采用 cursor 分页，不一次加载全部消息。事件和 checkpoint 设置保留策略；已完成 Run 可以压缩旧 checkpoint，但必须保留最终状态、消息、Operation Ledger 和审计需要的记录。

## 12. 测试与验收

### 数据库测试

- migration 从空库升级到最新版；
- 外键和唯一约束实际启用；
- 同 Session 并发 begin_run 只有一个成功；
- CAS revision 冲突不会覆盖新状态；
- WAL 下长读取不阻止短写事务；
- busy timeout 后返回稳定 `STORE_BUSY`。

### 崩溃测试

- 在每个事务提交前后终止进程；
- 用户消息和 Run 不出现半提交；
- 最终 Assistant 消息不重复；
- Interrupt 不会被消费两次；
- Tool Operation 状态可对账；
- Outbox 可以补发但业务事件不重复。

### 数据生命周期测试

- Session 删除清理所有拥有的数据；
- 不会删除其他 Session 共享或引用的数据；
- 备份恢复后 integrity check 通过；
- 大型输出只保留引用，不使数据库无限膨胀；
- 数据库中不存在明文凭据。

### 验收标准

- SQLite 是应用运行时唯一权威状态源；
- Memory 与 Knowledge 不写入 Runtime Store 领域表；
- 所有关键状态变化具有原子事务；
- 进程重启后可以恢复非终态 Run；
- GUI 可从消息和事件表重建用户可见状态；
- 业务层不直接依赖 SQLite API；
- 无需启动独立数据库服务。
