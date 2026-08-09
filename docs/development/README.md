# Development 文档

## 使用方式

开发工作按功能切片，而不是按架构层整体推进。每个功能切片可以同时涉及 API、Session Runtime、Graph、SQLite、事件和最小 UI。

主入口：

- [开发路线图](roadmap.md)
- [工程实现规范](engineering-guidelines.md)
- [本地开发说明](local-development.md)
- [功能文档模板](feature-template.md)

## 功能索引

| 编号 | 功能 | 目标里程碑 | 状态 |
|---|---|---|---|
| 00 | [工程骨架](features/00-project-foundation.md) | M0 | planned |
| 01 | [后端最小闭环](features/01-minimal-backend-loop.md) | M1 | planned |
| 02 | [最小桌面交互](features/02-minimal-desktop-ui.md) | M2 | planned |
| 03 | [只读文件能力](features/03-readonly-file-tools.md) | M3 | planned |
| 04 | [文件创建与修改](features/04-file-editing.md) | M4 | planned |
| 05 | [命令沙箱](features/05-command-sandbox.md) | M5 | planned |
| 06 | [Interrupt 与审批](features/06-approval-flow.md) | M6 | planned |
| 07 | [Planner 与并行任务](features/07-planner-parallel-tasks.md) | M7 | planned |
| 08 | [ResearchAgent 与 Web Research Tools](features/08-web-research-tools.md) | M8 | planned |
| 09 | [CodeDataAgent、Artifact 与 Research 集成](features/09-agents-artifacts.md) | M9 | planned |

## 状态定义

- `planned`：范围已定义，尚未开始。
- `in_progress`：存在正在开发的验收项。
- `blocked`：存在明确外部阻塞，并记录在功能文档中。
- `completed`：所有当前阶段验收项通过。
