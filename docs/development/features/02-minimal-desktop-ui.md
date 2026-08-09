# 最小桌面交互

状态：`planned`

目标里程碑：`M2`

## 1. 用户目标

用户可以在桌面界面创建会话、发送消息、看到运行状态和结果，并能取消正在运行的请求。

## 2. 当前阶段范围

### 包含

- 会话列表和新建会话；
- 当前会话消息列表；
- 消息输入和提交状态；
- running/cancelling/failed/completed 展示；
- Cancel；
- 可恢复的 API 错误展示；
- RuntimeEvent 增量更新；
- 空状态、加载状态和断线重连。

### 不包含

- Agent 拓扑、DAG 可视化；
- 权限中心和审批弹窗；
- 文件 diff 与 artifact 管理；
- 富文本编辑器；
- 完整桌面打包与自动更新。

## 3. 架构约束

GUI 不持有 Agent、Graph 或 SQLite connection。所有状态来自 API；本地 UI store 只是缓存，刷新后可以从后端重建。

## 4. API 与事件

复用 M1 API。事件传输首选 Server-Sent Events；如果第一阶段采用轮询，事件 cursor 协议必须与未来 SSE 兼容：

```text
GET /api/runs/{run_id}/events?after_sequence=42
```

UI 使用稳定状态码和错误码，不解析后端日志文本。

## 5. 交互规则

- 提交消息后立即显示已提交的用户消息和 Run 状态；
- 同一 Session 存在 active Run 时禁用再次提交；
- Cancel 后显示 cancelling，直到后端返回终态；
- 页面重载后根据 active_run_id 恢复状态订阅；
- API 暂时断开不把 Run 标记为 failed；
- 错误信息保留重试或返回会话的明确操作。

## 6. 实现步骤

1. 建立 API client 和类型定义。
2. 实现会话列表与选择。
3. 实现消息加载、输入和幂等提交。
4. 实现 Run 状态与 Cancel。
5. 实现事件 cursor、重连和页面恢复。
6. 补充桌面与窄窗口视觉测试。

## 7. 测试

- 新建和切换会话；
- 重复点击发送不会创建两个 Run；
- active Run 期间输入状态正确；
- Cancel 状态最终收敛；
- 刷新后继续展示当前 Run；
- 断网恢复后补齐事件且不重复；
- 长消息和错误文案不破坏布局。

## 8. 验收标准

- 非开发人员可完成一轮对话；
- UI 不直接访问数据库；
- 所有 Run 状态都可解释且不会永久 loading；
- 页面刷新不丢失对话；
- 后端重启后 UI 可以重新连接和恢复。

