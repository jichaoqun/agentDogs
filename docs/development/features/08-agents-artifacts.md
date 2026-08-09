# 扩展 Agent 与 Artifact

状态：`planned`

目标里程碑：`M8`

## 当前范围

在通用 ReAct Runtime 上增加 ResearchAgent 与 CodeDataAgent，完善 artifact 生成、类型验证、预览和发布。

## 实现重点

- AgentDescriptor 版本锁定；
- AgentTask 和 TaskResult contract tests；
- Research 来源、时间和事实/推断区分；
- CodeData sandbox 与 artifact staging；
- 文档、表格、图片等按类型验证；
- GUI 展示结果引用和发布状态。

## 验收标准

- Agent 不能扩大自己的工具权限；
- Research 结果具有来源与不确定性；
- CodeData 生成物先验证后发布；
- 同一 ReAct Runtime 支撑三类 Agent；
- 大型 artifact 不进入 SQLite checkpoint。

