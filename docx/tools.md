# Tools

功能：根据 Agent 的任务调度，调用 Tools 完成基础能力。

当前工具层已经采用内部注册表设计：

- `ToolSpec`: 描述工具名称、说明、输入结构、风险等级和能力标签。
- `ToolResult`: 统一返回 `ok`、`content`、`data`、`error`、`artifacts`。
- `ToolRegistry`: 统一注册、列出、按名称调用工具。

工具层只做基础能力，不直接承担复杂任务推理。专业任务由 sub_agents 组合工具完成；明确、低风险、一步可完成的工具任务由 SimpleTaskAgent 直接调用低风险工具完成。

## 文件工具

当前已实现：

- `list_workspace_tree`: 列出 workspace 文件树，默认排除 `.trash`。
- `read_file`: 读取文本类文件和 DOCX 纯文本。
- `write_file`: 写入文本类文件，高风险工具，自动执行前必须人工确认。
- `search_files`: 按文件名和文本内容搜索 workspace。
- `file_info`: 读取文件或目录元信息。

安全规则：

- 所有路径限制在 `workspace/` 内。
- 禁止绝对路径、盘符跳转和 `..` 路径穿越。
- 默认禁止直接操作 `workspace/.trash`。
- 文本读取默认限制 2MB。
- 写入类工具标记为 `high` 风险，当前 SimpleTaskAgent/FileAgent/TaskAgent 不会静默调用。

## SimpleTaskAgent 调用策略

SimpleTaskAgent 可以访问工具注册表，但第一版只自动执行低风险工具：

- `list_workspace_tree`: 处理“当前项目中有哪些文件”“当前工作目录有哪些文件”等目录列表问题。
- `read_file`: 读取明确路径的文本或 DOCX 纯文本文件。
- `search_files`: 搜索明确关键词。
- `file_info`: 查看明确路径的文件或目录信息。

中风险和高风险工具必须进入计划确认或人工确认流程，不能被简单任务静默执行。

## 搜索工具
支持：
web_search
local_search
keyword_search

## 文档工具
支持：
PDF解析
Word解析
Excel解析
Markdown解析
OCR解析

## 代码工具
支持：
Python执行
Shell执行
Notebook执行
项目扫描

## AI工具
支持：
文本总结
信息抽取
分类
翻译
向量化

## 执行沙箱（Sandbox/Safe Execution）： 
你的需求提到了“代码能力”和“文件操作”。这具有极大的安全隐患。 如果LLM理解错误，执行了删除系统文件或死循环代码怎么办？本地助手必须有一个隔离的沙箱环境（如 Docker、WASM，或者严格的本地目录权限限制），限制其只能操作指定的“工作区（Workspace）”文件夹。

## 调试接口

后端提供只读调试接口：

- `GET /api/v1/tools`: 查看已注册工具。
- `GET /api/v1/agents`: 查看已注册子 Agent。

前端不直接开放工具调用接口，避免绕过 Agent 的权限控制和人工确认流程。
