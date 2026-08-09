# Interrupt 与审批

状态：`planned`

目标里程碑：`M6`

## 当前范围

交付 Tool Policy、ApprovalGrant、Graph Interrupt/Resume、授权生命周期和重启恢复。

## 实现重点

- `allow/deny/require_approval`；
- 审批绑定 operation_id、参数 hash、principal 和 policy version；
- once/task/session/workspace grant；
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

