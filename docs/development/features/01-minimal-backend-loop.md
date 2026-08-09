# 后端最小闭环

状态：`planned`

目标里程碑：`M1`

## 1. 用户目标

用户可以通过 API 或 CLI 创建会话、发送一条消息，并获得真实模型生成的最终回答；应用重启后仍能读取原始对话和 Run 状态。

## 2. 当前阶段范围

### 包含

- Session 创建、读取和列表；
- 同 Session 单 active Run；
- 原始用户消息与最终 Assistant 消息；
- SQLite 原子 begin/complete/fail；
- Minimal Graph：initialize、coordinate、agent_step、compose_final、commit_final、handle_error；
- Minimal Coordinator：delegate GeneralAgent 或 final/fail；
- GeneralAgent 单次或有界多次模型步骤，但没有工具；
- ModelGateway timeout、有限重试、usage；
- RuntimeEvent 和查询；
- Cancel 持久意图和迟到模型结果丢弃；
- REST API 与集成测试 CLI。

### 不包含

- Planner 和 Task 并行；
- 文件、网络、命令和第三方工具；
- Interrupt/Resume；
- 流式 token；
- 正式 GUI；
- Memory 和 Knowledge。

## 3. 架构约束

- [Session Runtime](../../architecture/layer-1-session-runtime.md)
- [Control Graph](../../architecture/layer-2-control-graph.md)
- [Coordinator 与 ReAct Runtime](../../architecture/layer-3-coordination-react-runtime.md)
- [SQLite Runtime Store](../../architecture/persistence-sqlite-runtime-store.md)

M1 可以裁剪节点，但必须使用正式 `run_id`、revision、checkpoint 和事务协议，不能建立一套未来需要推翻的临时会话模型。

## 4. 数据模型与存储

启用表：

- `sessions`；
- `runs`；
- `messages`；
- `checkpoints`；
- `runtime_events`；
- `outbox_events`。

M1 的 TaskExecution 可以内嵌为单个固定 root task，但模型与 schema 应允许 M7 迁移到独立 task 表。

关键事务：

```text
begin_run = Session CAS + Run + user message + event + outbox
complete_run = final message + Run terminal + Session idle + event + outbox
fail_run = Run failed + Session failed/idle policy + event + outbox
```

## 5. HTTP API

```text
POST /api/sessions
GET  /api/sessions
GET  /api/sessions/{session_id}
POST /api/sessions/{session_id}/messages
POST /api/sessions/{session_id}/runs/{run_id}/cancel
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/events
```

提交消息需要客户端 `idempotency_key`。同步 HTTP 可以等待短任务完成，但内部必须以 Run 建模；超出请求等待时间时返回 `run_id` 和当前状态，而不是取消后台 Run。

## 6. Graph 流程

```text
initialize
 -> coordinate
 -> agent_step
 -> coordinate
 -> compose_final
 -> commit_final
 -> completed
```

Coordinator 只有：

```text
delegate(general)
final
fail
```

设定独立 `max_coordinator_calls`、`max_agent_steps`、`max_model_calls` 和 wall deadline。任何耗尽都必须转入 `partial/failed`，不能停留在 coordinating。

## 7. 模型失败

```text
primary call
 -> bounded retry for retryable error
 -> optional fallback model
 -> deterministic failed response
 -> fail_run transaction
```

结构化输出失败允许一次修复。取消、认证失败和不可重试参数错误不重试。迟到结果提交前检查 active run 和 revision。

## 8. 实现步骤

1. 创建领域模型、错误码和事件类型。
2. 完成 SQLite session/run/message repositories 和事务用例。
3. 实现 SessionRuntime create/get/list/submit/cancel。
4. 实现 FakeModelGateway 和 Minimal Graph。
5. 使用 FakeModel 完成全链路测试。
6. 实现真实 ModelGateway adapter 和可选 smoke test。
7. 实现 HTTP API 与 CLI 集成入口。
8. 加入恢复扫描和迟到结果测试。

## 9. 测试

- 同 idempotency key 返回同一 Run；
- 同 Session 两次并发提交只有一个成功；
- 不同 Session 可以并行；
- final message 唯一；
- 模型超时后 Run 进入终态；
- 取消后迟到模型结果不进入消息表；
- 每个事务提交前后崩溃均可恢复；
- 应用重启后能读取全部原始消息。

## 10. 验收标准

- API/CLI 能完成真实模型问答；
- SQLite 是唯一权威状态源；
- 没有全局内存会话字典；
- 模型异常不会无限显示 running；
- 重复请求不产生重复用户或 Assistant 消息；
- 默认自动化测试不依赖真实模型。

