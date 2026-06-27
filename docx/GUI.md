# GUI 说明

前端位于 `GUI/`，当前使用 Vite + React 实现。主要目标是提供聊天、模型选择、会话管理、文件操作入口和 Agent 调试信息展示。

## 技术栈

- React
- Vite
- lucide-react 图标
- 原生 CSS
- FastAPI `/api/v1` 后端接口

## 主要界面

### 会话

- 新建会话。
- 会话列表。
- 发送消息。
- 展示 assistant 回复。
- 展示模型 Provider/Model。
- 显示后端错误详情。

### 模型选择

前端从后端模型接口获取可用 Provider 和模型列表。发送消息时会把当前模型选择、温度、最大输出长度、thinking 开关等参数传给后端。

如果后端模型配置不可用，前端会显示“暂无可用模型”。

### 文件

文件能力由后端 workspace 工具提供，前端不直接绕过 Agent 调工具。

当前后端文件工具默认限制在 `workspace/`：

- 列目录。
- 读文件。
- 搜索文件。
- 查看文件信息。
- 上传文件接口在缺少 `python-multipart` 时会返回明确 503 提示。

### Agent 调试面板

调试面板默认关闭，面向开发调试使用。

开启方式：

```powershell
$env:VITE_AGENT_DEBUG_PANEL = "true"
```

或在浏览器控制台设置：

```javascript
localStorage.setItem("agentDebugPanel", "true")
localStorage.setItem("agentDebugPanel", "false")
```

侧边栏提供“调试信息”开关，点击后会同步到 localStorage。

开启后，每条 assistant 消息下方会出现“调试信息”折叠面板。面板优先展示 `agent_flow`，如果旧消息没有 `agent_flow`，则 fallback 展示 `debug_trace` 原始 JSON。

展示内容：

- 主 Agent
  - 名称
  - route
  - complexity
  - status
  - route reason
  - `TaskBrief`
  - 事件列表
- 子 Agent
  - 使用了哪个 Agent
  - 类型和能力说明
  - 输入
  - 输出
  - 状态
  - 错误
  - 相关 tool calls
- 工具调用
  - 工具名
  - payload
  - ok/status
  - error
- 最终输出
- 原始 JSON

## API 数据字段

`ChatResponse` 和 `MessageOut` 目前包含以下调试相关字段：

- `route`
- `complexity`
- `status`
- `interrupt`
- `plan_status`
- `task`
- `steps`
- `tool_calls`
- `debug_trace`
- `agent_flow`
- `task_brief`

前端内部会做字段兼容：

- 后端 snake_case: `tool_calls`、`debug_trace`、`agent_flow`、`task_brief`
- 前端 camelCase: `toolCalls`、`debugTrace`、`agentFlow`、`taskBrief`

## 错误显示

`GUI/src/api.js` 会尽量拼接后端错误详情。

如果后端返回：

```json
{
  "detail": {
    "message": "模型调用失败",
    "reason": "api/deepseek-v4-flash: Error code ..."
  }
}
```

前端应显示：

```text
模型调用失败：api/deepseek-v4-flash: Error code ...
```

这样开发时不用只看后端日志，也能在页面上看到模型失败原因。

## 运行

开发模式：

```powershell
cd GUI
npm run dev
```

构建：

```powershell
cd GUI
npm run build
```

详见 [运行说明](运行说明.md)。
