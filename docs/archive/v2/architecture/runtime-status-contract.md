# Runtime 状态与结果契约

## 1. 原则

生命周期状态与执行结果质量分开表示。`partial`、`blocked` 不是 Run 生命周期状态；`failed` 不作为 Session 的长期状态。

系统统一使用四套互不混用的枚举。

## 2. SessionStatus

```python
SessionStatus = Literal["idle", "running", "waiting_user", "cancelling"]
```

SessionStatus 只回答当前会话能否接受新工作：

- `idle`：没有 active Run，可以提交消息；
- `running`：active Run 正在执行；
- `waiting_user`：等待审批、澄清或 unknown 处理；
- `cancelling`：取消已持久化，正在收敛。

Run 成功、失败或取消后，Session 最终都回到 `idle`；上一次结果通过 `runs.status` 和 `RunOutcome` 查询，不把 Session 留在 failed。

## 3. RunLifecycleStatus

```python
RunLifecycleStatus = Literal[
    "initializing", "coordinating", "planning", "running_tasks",
    "waiting_user", "joining", "compressing", "composing_final",
    "committing_final", "cancelling", "execution_unknown",
    "completed", "cancelled", "failed",
]
```

终态只有 `completed/cancelled/failed`。`execution_unknown` 是需要对账或人工处理的非终态，不得自动转换为 completed 或允许新的冲突性写操作。

## 4. TaskExecutionStatus

```python
TaskExecutionStatus = Literal[
    "pending", "ready", "running", "tool_preparing", "waiting_approval",
    "waiting_clarification", "tool_executing", "tool_reconciling",
    "joining", "completed", "cancelled", "failed", "blocked",
    "execution_unknown",
]
```

`blocked` 表示任务缺少依赖、权限或用户信息，是否使父 Run 等待或结束由 JoinPolicy 和 Coordinator 决定。

## 5. OutcomeStatus

```python
OutcomeStatus = Literal["success", "partial", "blocked", "no_result"]
```

Outcome 描述完成质量，不驱动生命周期：

| RunLifecycleStatus | 允许的 OutcomeStatus |
|---|---|
| `completed` | `success/partial/blocked/no_result` |
| `cancelled` | `partial/no_result` |
| `failed` | `partial/no_result` |

Run 可以成功完成状态机并返回 partial；也可以因基础设施错误 failed，但保留 partial 结果。UI 分别展示“运行是否正常结束”和“目标完成程度”。

## 6. RunOutcome

```python
class RunOutcome(BaseModel):
    run_id: str
    status: OutcomeStatus
    summary: str
    completed_task_ids: list[str]
    incomplete_task_ids: list[str]
    blocking_reasons: list[str]
    result_references: list[str]
```

RunOutcome 在 Run 进入终态时与最终消息同事务提交。没有最终用户消息的取消/失败也必须保存 Outcome 记录。

## 7. 转换约束

- 生命周期转换只由 Graph/Session Runtime 执行；
- TaskResult 不能直接修改 RunLifecycleStatus；
- OutcomeStatus 不能用于决定 Session 是否接受新消息；
- 非终态必须具有 deadline、取消、恢复或人工处理路径；
- 未知枚举值默认拒绝并返回 schema/version 错误。
