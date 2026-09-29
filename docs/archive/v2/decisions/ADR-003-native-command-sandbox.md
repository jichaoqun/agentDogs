# ADR-003：桌面端使用 OS 原生命令沙箱

状态：`accepted`

## 背景

Docker 对桌面应用过重，但 Agent 执行任意命令又不能继承完整用户权限。文件工具和任意命令具有不同风险与执行需求。

## 决策

- 内置文件操作通过可信 File Broker 和结构化 Tool 执行；
- 任意命令通过 Sandbox Broker 启动受限子进程；
- Windows 使用 AppContainer/LPAC 等原生访问限制，并使用 Job Object 管理资源和进程树；
- 默认禁止网络，只开放显式文件路径；
- 沙箱能力不可用时 fail closed，不自动退化为完整权限。

## 后果

避免 Docker 依赖并保持桌面体验，但需要平台 adapter、Windows 特性检测和更完整的安全测试。审批不能代替 OS 级限制。

## 参考

- [命令沙箱功能](../development/features/05-command-sandbox.md)
- [Capability Runtime](../architecture/layer-4-capability-runtime.md)

