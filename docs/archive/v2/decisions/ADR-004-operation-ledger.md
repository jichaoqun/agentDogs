# ADR-004：副作用工具使用 Operation Ledger

状态：`accepted`

## 背景

进程可能在工具已经产生副作用、结果尚未写入 checkpoint 时崩溃。仅从 Graph checkpoint 重放会重复写文件、发布或执行外部操作。

## 决策

每个工具调用使用稳定 operation_id、规范参数 hash、幂等键和持久 Operation Ledger。恢复时先查询或对账，非幂等 unknown 操作不得自动重放。

## 后果

工具实现需要声明副作用等级、幂等和 reconcile 能力。系统增加账本状态，但可以正确表达未知副作用并避免危险重试。

## 参考

- [Control Graph](../architecture/layer-2-control-graph.md)
- [Capability Runtime](../architecture/layer-4-capability-runtime.md)

