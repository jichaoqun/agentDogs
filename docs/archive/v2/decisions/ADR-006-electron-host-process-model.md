# ADR-006：Electron Desktop Host 与进程模型

状态：`accepted`

## 背景

现有 GUI 使用 React/Vite，V2 后端使用 Python 和 SQLite。桌面宿主必须管理后端生命周期、认证、版本、升级、日志和应用数据目录。

## 决策

V2 首个桌面版本使用 Electron：

```text
Electron Main（可信宿主）
  ├─ BrowserWindow / React Renderer（不可信 UI 边界）
  ├─ typed preload IPC
  └─ Python Runtime 子进程
       ├─ loopback authenticated API
       └─ SQLite Runtime Store
```

### 生命周期

1. Main 获取单实例锁并确定当前用户应用数据目录。
2. Main 启动打包的 Python Runtime，传入数据库目录和 bootstrap pipe。
3. 后端执行兼容性检查和 migration，再返回 ready handshake。
4. Main 校验 `protocol_version`、`backend_version` 和 `schema_version` 后创建 Renderer。
5. 后端意外退出时 Main 显示恢复状态，并按有界策略重启；连续失败后停止重启并提供诊断信息。
6. 正常退出时 Main 请求优雅停止，超时后终止子进程树。

### 端口与通信

- 后端绑定随机 loopback 端口；
- 端口和 token 只通过 bootstrap pipe 返回；
- Renderer 只使用 preload IPC；
- Main 负责 SSE 连接、cursor 重连和事件转发；
- API handshake 必须在任何业务请求之前完成。

### 版本兼容

Handshake：

```python
class RuntimeHandshake(BaseModel):
    instance_id: str
    backend_version: str
    api_protocol_version: int
    min_gui_protocol_version: int
    schema_version: int
    port: int
```

主版本不兼容时拒绝进入业务界面。前后端随同一安装包发布，不支持任意版本混用。

### 更新与 Migration

- 更新程序在应用和后端全部退出后替换文件；
- 新版本首次启动先创建一致性数据库备份，再执行 migration；
- migration 失败时不启动旧二进制写入新 schema；
- 自动回滚应用文件不等于自动回滚数据库，数据库恢复必须经过明确兼容检查；
- 用户数据目录与安装目录分离，卸载默认不删除用户数据。

### 打包与数据目录

- React 静态资源打入 Electron 包，不从开发服务器加载生产 UI；
- Python Runtime 和 migration 随应用发布；
- 数据库、日志、blob、artifact 和备份使用 Electron `userData` 下的版本化目录；
- secret 使用 Windows Credential Manager，不写普通配置或 SQLite；
- Windows 安装包采用 per-user 安装作为默认模式，避免运行时管理员权限。

## 后果

Electron 增加安装体积，但最大化复用现有 React UI，并提供成熟的子进程、IPC和更新机制。若未来改用其他宿主，必须以新 ADR 替代本决策，同时保持 API/IPC 信任边界。

## 验收

- 单实例、启动、退出和崩溃重启可测试；
- Renderer 不能访问 Node、SQLite、文件系统或 backend token；
- 不兼容版本无法进入业务界面；
- migration 失败不会继续写库；
- 生产包不依赖 Vite dev server 或系统 Python。

