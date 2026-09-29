# ADR-005：Desktop API Trust Boundary

状态：`accepted`

## 背景

桌面 GUI 通过本地 API 使用 Python Runtime。仅绑定 localhost 不能阻止恶意网页、其他本地进程或另一个 OS 用户尝试调用 Agent API。未来文件、命令和审批能力使未认证本地 API 成为高风险入口。

## 决策

### 监听范围

- 默认只监听 `127.0.0.1`，不监听 `0.0.0.0`、局域网地址或 IPv6 wildcard；
- 使用操作系统分配的随机端口；
- 远程访问不属于 V2；
- API 不提供关闭认证的生产配置。

### Bootstrap 与认证

- Electron Main 启动 Python 后端时，通过匿名 pipe/stdin 传入一次性 bootstrap secret，不放在命令行、URL、日志或普通配置文件；
- 后端生成本次进程使用的高熵 bearer token，通过同一受控 pipe 返回端口、token、instance_id 和 protocol_version；
- token 只保存在 Electron Main 内存，Renderer 不接触 token；
- 后端重启必须生成新 token，旧 token 立即失效；
- 每个请求验证 bearer token、instance_id 和 API protocol version；
- SSE、REST 和未来 WebSocket 使用完全相同的认证。

### Renderer 边界

- Renderer 通过类型化 preload IPC 调用 Electron Main；
- Main 代理 REST/SSE，不向 Renderer 暴露通用 HTTP、任意 IPC 或 token；
- `contextIsolation=true`、`nodeIntegration=false`、启用 sandbox；
- preload 只暴露固定 Agent Dogs API 方法；
- 禁止在应用窗口导航到非打包内容，外部链接交给系统浏览器。

### Web 防护

- 后端默认不返回 CORS allow-origin；
- 拒绝浏览器携带的未知 Origin；
- 状态改变请求只接受认证 JSON，不接受 form/simple request；
- 设置严格 Host 校验，只接受实际 loopback host/port；
- 不使用 cookie，因此 CSRF token 不是主认证机制；bearer token 和 Origin/Host 检查同时存在；
- 错误响应不泄露 token、数据库路径或文件授权范围。

### CLI

CLI 是独立 client principal。它不能读取 Electron Main 内存 token。V2 CLI 测试入口默认自己启动受控后端；未来连接已运行桌面实例时，通过仅当前 OS 用户可访问的 named pipe 请求短期、能力受限 token。

### 多实例与 OS 用户

- Electron 使用单实例锁；第二实例把激活请求发送给主实例，不再启动第二个 Writer；
- 数据目录 ACL 仅允许当前 OS 用户；
- instance metadata 不包含 bearer token；
- 后端持有数据库 instance lock，异常退出后依靠进程身份和租约恢复；
- 不同 OS 用户使用不同应用数据目录、principal 和数据库。

## 后果

本地 API 可以继续用于后端测试和 CLI，但 GUI Renderer 不直接访问它。开发环境也不得用无认证 API 替代正式协议；测试使用显式测试 token。

## 验收

- 无 token、错误 token、旧 token和错误 instance_id 均返回 401；
- 未知 Origin、Host 和浏览器 simple request 被拒绝；
- Renderer 无法读取 token 或调用未暴露 IPC；
- SSE 重连携带相同认证并从 cursor 继续；
- 后端重启后旧客户端不能继续调用；
- 恶意网页无法调用运行中的 Agent API。

