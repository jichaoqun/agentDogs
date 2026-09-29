# 工程实现规范

## 1. 依赖方向

代码依赖遵循架构层级。领域模型不能导入 API、GUI、SQLite connection、模型 SDK 或 Tool Handler。

```text
API/GUI -> Application Use Cases -> Domain Protocols
Infrastructure(SQLite/Model/Tools) -> Domain Protocols
```

Infrastructure 实现 Protocol，而不是让业务层直接依赖具体 SDK。

## 2. 事务与异步

- SQLite 事务内禁止模型调用、网络请求、工具执行和用户等待。
- 写事务使用高层 Store 用例，不在业务代码中拼接 repository 调用。
- 所有外部调用有 timeout、cancellation 和稳定 request/operation ID。
- 事件与业务状态通过 outbox 同事务提交。

## 3. 状态与错误

- 状态必须由显式枚举或判别联合表示。
- 未知状态、未知动作和未知 schema version 默认拒绝。
- 异常在模块边界转换为稳定错误码。
- 用户文案可以本地化，错误码不能随文案变化。
- 非终态必须有 timeout、取消或恢复路径。

## 4. 文件和命令

- 文件操作使用结构化 Tool，不通过 shell 拼命令。
- 路径先规范化，再检查授权根和符号链接。
- 修改使用 expected hash 和原子 replace。
- 命令只由 Sandbox Broker 启动。
- Policy approval 不能代替 OS 级限制。

## 5. 测试

- 领域状态机先写表驱动单元测试。
- Store 同时运行契约测试和真实 SQLite 测试。
- 外部模型在多数测试中使用 FakeModelGateway；保留少量可选真实模型 smoke test。
- 涉及事务和副作用时增加崩溃点测试。
- 每个缺陷修复必须增加能够复现问题的测试。

## 6. 文档同步

PR 或提交涉及以下变化时必须同步文档：

- 新增状态、事件或错误码；
- 数据库 schema 或 migration；
- Tool 权限和审批行为；
- API request/response；
- 当前里程碑范围或验收标准。

