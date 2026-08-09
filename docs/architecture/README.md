# Architecture 文档

## 文档索引

1. [总体架构](overview.md)
2. [第一层：Session Runtime](layer-1-session-runtime.md)
3. [第二层：Main Control Graph](layer-2-control-graph.md)
4. [第三层：Coordinator 与 ReAct Runtime](layer-3-coordination-react-runtime.md)
5. [第四层：Capability Runtime](layer-4-capability-runtime.md)
6. [SQLite Runtime Store](persistence-sqlite-runtime-store.md)
7. [Runtime 状态与结果契约](runtime-status-contract.md)
8. [Web Tool 与网络安全契约](web-tool-security.md)

这些文档描述 V2 的目标状态。开发顺序和阶段性裁剪以 [开发路线图](../development/roadmap.md) 为准，但阶段性实现不得破坏架构中的身份、事务、权限和恢复协议。
