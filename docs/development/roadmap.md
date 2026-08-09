# V2 开发路线图

## 1. 开发策略

采用“后端闭环优先、最小 UI 紧随其后”的纵向迭代：

```text
M0 工程骨架
 -> M1 可通过 API/CLI 使用的后端闭环
 -> M2 可交互的最小桌面界面
 -> M3/M4 文件能力
 -> M5/M6 安全执行与审批
 -> M7 并行编排
 -> M8 扩展 Agent 与 Artifact
```

不先实现四层的全部抽象再集成。每个里程碑必须产生可以运行、测试和观察的系统增量。

## 2. 全局完成定义

每个里程碑同时满足：

- 功能文档中的当前范围已经实现；
- 数据库 migration 可从空库执行；
- 核心接口有类型定义；
- 单元和集成测试通过；
- 错误不会使 Run 永久停留在非终态；
- 新状态和错误码有事件记录；
- 本地启动步骤可复现；
- 文档与实现保持一致。

## 3. 里程碑

### M0：工程骨架

交付模块目录、配置加载、领域模型、SQLite migration、日志、测试框架和开发启动命令。不接入真实 Agent 工作流。

完成后可以创建数据库、执行 migration、启动 API health endpoint 并运行测试。

### M1：后端最小闭环

交付：

- Session 创建与查询；
- 原始用户消息提交；
- SQLite 原子 `begin_run`；
- Minimal Control Graph；
- 简单 Coordinator；
- 单个 GeneralAgent；
- 一次真实模型结构化调用；
- 最终 Assistant 消息原子提交；
- RuntimeEvent；
- REST API 和 CLI/集成测试入口。

不包含 Planner、并行任务、文件工具、命令执行、审批和正式 GUI。

### M2：最小桌面交互

交付会话列表、消息列表、输入框、Run 状态、Cancel、错误展示和事件更新。UI 只验证真实交互协议，不建设复杂工作台。

### M3：只读文件能力

交付 workspace 授权根、`list_tree`、`read_file`、`file_info`、`search_files`、路径规范化和输出限制。不得写文件或执行命令。

### M4：文件创建与修改

交付 staging、`create_file`、`apply_patch`、内容 hash 冲突检查、diff、原子 replace 和 `publish_artifact`。删除与跨根目录写入继续拒绝或审批。

### M5：命令沙箱

交付 Windows Sandbox Broker、受限进程、Job Object、资源限制、默认无网络、进程树终止和 Operation Ledger。

### M6：Interrupt 与审批

交付 ApprovalGrant、Resume 幂等、一次/任务/会话/workspace 授权、权限展示和进程重启恢复。

### M7：Planner 与并行任务

交付 TaskPlan DAG、TaskExecution、fork/join、任务租约、独立预算、取消传播和资源互斥。先以并发度 1 验证协议，再提高并发度，不改变数据模型。

### M8：扩展 Agent 与 Artifact

交付 ResearchAgent、CodeDataAgent、artifact 类型验证、发布和更完整的 GUI 状态展示。

## 4. 当前不进入路线图

- 长期 Memory 自动提取；
- Knowledge Base 摄取与向量索引；
- 无中心 Swarm；
- 跨机器 Durable Execution；
- 多 Agent 自由互发消息；
- 全自动高风险操作。

