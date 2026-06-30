# Tools 设计

Tools 是 Agent Dogs 的基础能力层，只负责原子操作，不承担复杂任务推理。复杂任务理解、工具选择、结果汇总由 MainAgent 和子 Agent 完成。

## 工具注册表

位置：

- `agent/core/tools/base.py`
- `agent/core/tools/__init__.py`

核心对象：

- `ToolSpec`: 描述工具名称、说明、输入结构、风险等级和能力标签。
- `ToolResult`: 统一返回 `ok`、`content`、`data`、`error`、`artifacts`。
- `ToolRegistry`: 注册、列出、按名称调用工具。

后端只读调试接口：

- `GET /api/v1/tools`: 查看已注册工具。
- `GET /api/v1/agents`: 查看已注册子 Agent 及能力说明。

前端不直接开放任意工具调用接口，避免绕过 Agent 权限控制和人工确认流程。

## 文件工具

位置：

- `agent/core/tools/file_tools.py`

当前工具：

- `list_workspace_tree`: 列出 workspace 文件树。
- `read_file`: 读取文本类文件和 DOCX 纯文本。
- `write_file`: 写入文本文件，高风险工具，当前不会被普通 Agent 静默执行。
- `create_directory`: 在 workspace 内创建目录，高风险工具，必须二次确认后才能执行。
- `publish_artifact`: 把 `runtime/artifacts` 中的产物发布到 workspace，高风险工具，必须二次确认后才能执行。
- `search_files`: 按文件名和文本内容搜索 workspace。
- `file_info`: 查看文件或目录元信息。

安全规则：

- 所有相对路径默认相对于 `workspace/`。
- 禁止绝对路径、盘符跳转和 `..` 路径穿越。
- 默认禁止直接操作 `workspace/.trash`。
- 文本读取有大小限制。
- 写入类工具标记为 `high` 风险，必须进入人工确认或后续审批流程。
- `publish_artifact` 只允许从 `runtime/artifacts/` 复制产物，不能从任意宿主机路径复制文件。
- 未二次确认前，TaskAgent 只会生成 `pending_confirmations`，不会调用 `create_directory` 或 `publish_artifact`。

## 搜索工具

位置：

- `agent/core/tools/search_tools.py`

当前工具：

- `workspace_search`: 搜索 workspace 中的文件名和文本内容，返回统一搜索结果结构。
- `keyword_search`: 在给定文本或结果列表中做关键词匹配、排序和摘要。
- `web_search`: 联网搜索入口，默认使用 DuckDuckGo HTML best-effort provider，可抓取前几个结果页正文片段。

`web_search` 返回的结果包含：

- `title`
- `url`
- `source`
- `summary`
- `content_excerpt`
- `fetched`
- `error`
- `match_reason`

联网搜索配置在 `config/llm.yaml` 顶层 `search`：

```yaml
search:
  enabled: false
  provider: duckduckgo
  max_results: 5
  fetch_pages: 3
  timeout: 10
  user_agent: AgentDogs/0.1
```

也可以用环境变量临时启用：

```powershell
$env:AGENT_WEB_SEARCH_ENABLED = "1"
```

搜索安全规则：

- 搜索工具均为只读低风险工具。
- `workspace_search` 只访问 workspace。
- `web_search` 只支持 `http/https`。
- `web_search` 拒绝 `localhost`、私有 IP、内网域名、`file://` 和非 HTTP 协议，避免 SSRF。
- `web_search` 限制结果数量、请求超时、响应大小和正文片段长度。
- 搜索失败时返回结构化错误，不让 Agent 编造联网结果。

## 子 Agent 如何使用工具

### SimpleChatAgent

不调用工具，只做模型对话。

### SearchAgent

处理搜索类任务，可调用：

- `workspace_search`
- `keyword_search`
- `web_search`

`SearchAgent` 会根据 `TaskBrief.source_policy` 和 `context.source_scope` 判断搜索源：

- `requires_fresh_external_info` 或 `source_scope=web`: 使用 `web_search`。
- `source_scope=workspace`: 使用 `workspace_search`。
- `source_scope=keyword`: 使用 `keyword_search`。

子 Agent 返回结构化 `summary/findings/evidence`，最终用户回复由 MainAgent 的 `synthesize_result` 汇总。

### FileAgent

处理 workspace 文件读取和搜索，可调用：

- `read_file`
- `search_files`

文件请求示例：

- `读取 readme.md`
- `帮我查看 add_new.md 中的内容`
- ``帮我查看 `add_new.md` 中的内容``

路径提取会去掉中文自然语言前后缀，例如“帮我查看”“中的内容”“的内容是什么”，并优先保留反引号里的路径。

### SimpleTaskAgent

兼容层，支持明确、低风险、一步工具任务：

- 列目录。
- 读取明确文件。
- 搜索明确关键词。
- 查看文件信息。

后续会逐步把搜索能力迁移给 `SearchAgent`，文件能力迁移给 `FileAgent`。

### CodeAgent

处理需要代码能力完成的任务，可使用 OpenSandbox 沙箱执行 Python。

第一阶段能力：

- 数据分析：读取 CSV/Excel/JSON/TXT/MD 并输出统计摘要、缺失值、类型推断、相关性和类别分布。
- 图表生成：生成图片到 artifacts 目录。
- 代码结构分析：读取代码文件并提取类、函数、导入或关键词结构。
- 项目结构分析：只读扫描 workspace 项目结构、关键文件和文件类型。
- 代码生成：只生成代码文本，不执行、不写 workspace。
- 脚本执行：用户明确要求时，在 OpenSandbox 沙箱中执行 Python 脚本。

安全规则：

- 不执行宿主机 Python。
- 不执行任意 shell。
- OpenSandbox Server 不可用时不 fallback。
- workspace 只读。
- artifacts 目录可写。
- 默认无网络，只有运行时依赖安装开启且任务需要依赖时才打开网络。
- 运行时依赖安装受 allowlist 限制。
- 用户脚本执行需要显式启用配置。

配置：

```yaml
code_execution:
  enabled: false
  backend: opensandbox
  image: python:3.11-slim
  timeout_seconds: 20
  memory_limit: 512m
  cpu_limit: 1
  network_enabled: false
  workspace_readonly: true
  artifacts_dir: runtime/artifacts
  max_output_chars: 12000
  allow_user_script_execution: false
  opensandbox:
    domain: ${OPENSANDBOX_DOMAIN:-127.0.0.1:8080}
    protocol: ${OPENSANDBOX_PROTOCOL:-http}
    api_key: ${OPENSANDBOX_API_KEY:-}
    request_timeout_seconds: 60
  dependency_install:
    enabled: false
    allowed_packages: [pandas, numpy, openpyxl, matplotlib, seaborn, scipy, scikit-learn]
```

### TaskAgent

复杂任务第一阶段执行协调器。它接收用户已确认的计划步骤，然后按步骤分类并委派：

- `file_read`: 调用 `FileAgent`。
- `search`: 调用 `SearchAgent`。
- `data_analysis` / `chart_generation` / `code_analysis`: 调用 `CodeAgent`。
- `manual_review`: 记录为执行策略或上下文。
- `workspace_write`: 不直接执行，返回 `waiting_confirmation`。

TaskAgent 会保留共享上下文，例如已识别的 `02.xlsx`、CodeAgent 生成的 artifacts、前序分析摘要。对于“分析 Excel 并把图表存到 02_analys 文件夹”这类任务，它会先完成沙箱分析和 artifact 生成，再把创建目录、发布 artifact 到 workspace 的动作放入 `pending_confirmations`。

## 工具调用调试

每次工具调用会进入 `tool_calls` 和 `debug_trace`：

```json
{
  "tool": "web_search",
  "payload": {
    "query": "北京 2026-06-28 天气 预报",
    "max_results": 5,
    "fetch_pages": 3
  },
  "ok": true
}
```

前端调试面板会在“工具调用”区域展示工具名称、输入、状态和错误。完整结构也可在“原始 JSON”中查看。
