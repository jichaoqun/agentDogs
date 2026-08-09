# Agent Dogs 文档

## 文档类型

| 目录 | 回答的问题 | 主要读者 |
|---|---|---|
| [architecture](architecture/README.md) | 最终系统是什么、边界和协议是什么 | 架构与模块负责人 |
| [development](development/README.md) | 下一步开发什么、如何实现、何时算完成 | 开发人员 |
| [decisions](decisions/README.md) | 为什么选择这一方案 | 所有维护者 |
| [testing](testing/README.md) | 如何证明实现正确且可恢复 | 开发与测试人员 |

## 推荐阅读顺序

新参与开发的人按以下顺序阅读：

1. [总体架构](architecture/overview.md)
2. [开发路线图](development/roadmap.md)
3. 当前里程碑对应的功能文档
4. 相关 ADR
5. [测试策略](testing/test-strategy.md)

## 文档规则

- Architecture 描述稳定目标，不记录每日开发进度。
- Development 描述阶段范围、接口、步骤和验收标准。
- ADR 记录已经作出的重要技术决策，不用于写实现教程。
- Testing 保存跨功能的测试方法；具体测试用例仍写入对应功能文档。
- 实现改变架构协议时，先更新 Architecture 或 ADR，再更新功能文档。
- 功能完成后保留开发文档，并将状态更新为 `completed`，不要删除历史范围。

