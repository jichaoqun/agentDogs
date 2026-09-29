# Interrupt 与审批

状态：`planned`

目标里程碑：`M6`

## 当前范围

交付 Principal、Workspace、PermissionGrant、Tool Policy、ApprovalGrant、Graph Interrupt/Resume、授权生命周期和重启恢复。

## 实现重点

- `allow/deny/require_approval`；
- 审批绑定 operation_id、参数 hash、principal 和 policy version；
- once/task/session/workspace grant；
- PermissionGrant 与一次 operation ApprovalGrant 分表、分接口和分事件；
- Grant 撤销、过期、revision 与 Policy cache 失效；
- Resume idempotency；
- 审批前安全参数展示；
- waiting_user 恢复与过期处理；
- 任何授权都不突破 OS sandbox。

## 验收标准

- 审批后替换参数必须失效；
- 重复 Resume 只消费一次；
- 重启后审批仍可继续；
- deny 作为结构化结果返回 Agent；
- 高风险操作没有全局默认允许。
- CLI 与 GUI 使用不同 client principal，不隐式共享 session grant。
