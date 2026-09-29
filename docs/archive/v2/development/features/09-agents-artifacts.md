# CodeDataAgent、Artifact 与 Research 集成

状态：`planned`

目标里程碑：`M9`

## 当前范围

在 M8 已交付 ResearchAgent 的基础上增加 CodeDataAgent，完善 artifact 生成、类型验证、预览和发布，并增强 Research 结果在 artifact 与 GUI 中的展示。M9 不重复实现 ResearchAgent 的核心搜索能力。

## 实现重点

- AgentDescriptor 版本锁定；
- AgentTask 和 TaskResult contract tests；
- Research 来源在报告 artifact 和 GUI 中的引用展示；
- CodeData sandbox 与 artifact staging；
- 文档、表格、图片等按类型验证；
- GUI 展示结果引用和发布状态。

## 验收标准

- Agent 不能扩大自己的工具权限；
- M8 ResearchAgent contract tests 持续通过；
- CodeData 生成物先验证后发布；
- 同一 ReAct Runtime 支撑三类 Agent；
- 大型 artifact 不进入 SQLite checkpoint。
