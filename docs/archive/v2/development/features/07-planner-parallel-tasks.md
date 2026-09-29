# Planner 与并行任务

状态：`planned`

目标里程碑：`M7`

## 当前范围

交付 TaskPlan DAG、TaskExecution、Scheduler、fork/join、独立 checkpoint、预算、租约、取消传播和资源互斥。

## 实现步骤

1. 使用正式 TaskExecution schema，以 `max_parallel_tasks=1` 完成协议测试。
2. 实现 ready 计算、DAG 校验和 join policy。
3. 增加 task lease 和并发 CAS 提交。
4. 提高并发度，加入共享预算和资源互斥。
5. 完成 fail-fast、all-settled、min-success 和 best-effort 测试。

## 验收标准

- 无依赖任务真实并行；
- 有依赖任务不会提前执行；
- 每个任务上下文和工具结果隔离；
- 一个任务失败不会污染其他任务；
- 父 Run 取消可收敛所有子任务；
- join 在重启后结果一致。

