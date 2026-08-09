# 故障注入设计

## 1. 目标

证明系统在进程终止、超时、迟到结果、SQLite 竞争和外部结果未知时不会重复消息、覆盖状态或重放危险副作用。

## 2. Failpoint

实现只在测试模式启用的命名 failpoint：

```text
before_begin_run_commit
after_begin_run_commit
after_model_response_before_checkpoint
after_operation_prepare
after_tool_side_effect_before_ledger_complete
after_interrupt_commit_before_response
after_final_message_before_commit
after_finalize_run_commit_before_publish
after_run_lease_takeover_before_resume
after_desktop_backend_ready_before_handshake
```

Failpoint 可以抛出异常或立即终止测试 Worker。生产配置不能动态启用。

## 3. 核心不变量

- 同一 idempotency key 只有一个 Run；
- 每个 Run 至多一个最终 Assistant 消息，cancelled/failed 可以没有；
- 已提交事件 sequence 不重复；
- 迟到 Worker 不能覆盖新 revision；
- unknown 非幂等操作不自动重放；
- outbox 可以重复投递，但消费者按 event_id 去重；
- 恢复最终进入稳定终态或 waiting_user/execution_unknown。

## 4. 执行方式

每个事务和副作用边界分别测试 failpoint 前、后两种情况。恢复器重新打开同一数据库并驱动 Run，断言数据库、事件和外部 fake operation 的调用次数。
