# Agent Dogs 对话测试用例

本文档用于人工测试当前 Agent Dogs 是否能完整工作。测试时建议同时打开前端“调试信息”面板，确认 MainAgent、子 Agent、Tools、TaskBrief 和最终汇总是否符合预期。

## 测试前准备

启动后端：

```powershell
python -m uvicorn agent.api.app:app --host 127.0.0.1 --port 8000 --reload --reload-dir agent --reload-dir config
```

启动前端：

```powershell
cd GUI
npm run dev
```

打开调试面板：

```javascript
localStorage.setItem("agentDebugPanel", "true")
```

建议准备 workspace 文件：

```text
workspace/readme.md
workspace/add_new.md
workspace/data.csv
workspace/sales.csv
workspace/02.xlsx
```

示例 `workspace/data.csv`：

```csv
name,score,age
Alice,90,18
Bob,75,20
Cindy,88,19
```

示例 `workspace/sales.csv`：

```csv
month,sales
2026-01,120
2026-02,180
2026-03,160
2026-04,220
```

如需测试联网搜索：

```yaml
search:
  enabled: true
```

如需测试 CodeAgent 执行后端：

```yaml
code_execution:
  enabled: true
  backend: local_process # opensandbox | local_process
  local_process:
    require_human_approval: true
```

如果配置的执行后端不可用或 `code_execution.enabled=false`，CodeAgent 应返回明确失败，且不能自动切换到其他后端。`local_process` 不是强安全沙箱，首次执行应进入 `execution_approval` 等待人工确认。

## 自动化回归测试

后端完整测试：

```powershell
python -m unittest discover -s tests -q
```

当前覆盖点包括：

- API 会话与模型错误返回。
- MainAgent 路由与 TaskBrief。
- `debug_trace` / `agent_flow`。
- metadata 不污染模型请求。
- prompt 构造函数。
- 文件读取路径提取。
- SearchAgent 与 FileAgent。
- CodeAgent 执行后端、Excel 路由、图表 artifact。
- TaskAgent 对 File/Search/Code 步骤的委派。

## 通用检查维度

每条用例建议检查：

- 最终回答是否符合任务。
- `route` 是否符合预期。
- `TaskBrief.delegate_to` 是否符合预期。
- `subAgents` 是否出现正确子 Agent。
- `tools` 是否出现正确工具调用。
- `finalOutput` 是否是 MainAgent 汇总后的结果，而不是工具原始长输出。
- 没有 `tool_calls: empty array` 一类模型协议错误。
- 没有“没有工具调用却声称已读取/已保存/已生成”的虚假执行。

## Level 1：普通聊天

### 1.1 闲聊问候

用户输入：

```text
你好
```

期望：

- `route = simple_chat`
- 子 Agent：`SimpleChatAgent`
- 不调用工具。
- 最终回答自然、简短。

调试检查：

- `agent_flow.mainAgent.route = simple_chat`
- `agent_flow.subAgents` 包含 `SimpleChatAgent`
- `agent_flow.tools` 为空

### 1.2 概念解释

用户输入：

```text
什么是 Agent？
```

期望：

- `route = simple_chat`
- 子 Agent：`SimpleChatAgent`
- 不调用工具。

## Level 2：workspace 文件读取

### 2.1 直接读取文件

用户输入：

```text
读取 readme.md
```

期望：

- `route = simple_task`
- `TaskBrief.delegate_to = file_agent`
- 子 Agent：`FileAgent`
- 工具：`read_file`
- 路径解析为 `readme.md`

### 2.2 中文自然语言读取文件

用户输入：

```text
帮我查看add_new.md中的内容
```

期望：

- 不应返回“文件不存在”。
- `TaskBrief.context.path = add_new.md`
- 工具 payload 中 `path = add_new.md`
- 最终回答包含文件内容预览。

### 2.3 反引号路径读取

用户输入：

```text
帮我查看 `add_new.md` 中的内容
```

期望：

- 优先提取反引号中的路径。
- 工具：`read_file`

## Level 3：workspace 搜索

### 3.1 搜索 workspace 文件内容

用户输入：

```text
搜索 workspace 中的 protein
```

期望：

- `route = simple_task`
- `TaskBrief.delegate_to = search_agent`
- 子 Agent：`SearchAgent`
- 工具：`workspace_search`
- 不走联网搜索。

### 3.2 搜索文件名或路径

用户输入：

```text
搜索 workspace 中的 api.py
```

期望：

- `SearchAgent` 选择 `workspace_search`。
- 最终回答列出匹配文件或说明未找到。

## Level 4：实时/联网搜索

### 4.1 今日天气，地点明确

用户输入：

```text
今天北京的天气怎么样
```

期望：

- `route = simple_task`
- `TaskBrief.intent = weather_lookup`
- `TaskBrief.delegate_to = search_agent`
- `TaskBrief.context.location = 北京`
- `TaskBrief.context.date = 当前日期`
- 工具：`web_search`
- 最终回答是天气摘要，不是原始搜索结果列表。

调试检查：

- `MainAgent.synthesize_result` 存在。
- 原始搜索结果保留在调试 JSON。

### 4.2 今日天气，地点缺失

用户输入：

```text
今天天气怎么样
```

期望：

- `route = clarify`
- `complexity = needs_info`
- 前端弹出补充信息窗口。
- 问题应询问城市或地区。

### 4.3 动态体育信息

用户输入：

```text
搜索足球的相关知识，尤其是今年的比赛信息
```

期望：

- `TaskBrief.delegate_to = search_agent`
- 工具：`web_search`
- 不应默认搜索 workspace。
- 最终回答应是摘要，不是长搜索结果直出。

## Level 5：CodeAgent 数据分析

### 5.1 执行后端未启用时的安全失败

前置条件：

```yaml
code_execution:
  enabled: false
```

用户输入：

```text
用 Python 分析 data.csv
```

期望：

- `route = simple_task`
- `TaskBrief.delegate_to = code_agent`
- 子 Agent：`CodeAgent`
- 工具调用：`code_sandbox`
- 最终回答明确说明执行后端未启用或不可用。
- 不能回退到宿主机 Python。

### 5.2 CSV 数据分析

前置条件：

```yaml
code_execution:
  enabled: true
```

配置的执行后端可用。使用 `opensandbox` 时需要 OpenSandbox Server；使用 `local_process` 时需要人工批准执行。

用户输入：

```text
用 Python 分析 data.csv
```

期望：

- `TaskBrief.intent = data_analysis`
- `TaskBrief.delegate_to = code_agent`
- CodeAgent 通过配置的 `code_sandbox` 后端执行。
- workspace 只读。
- 最终回答包含行数、列名、数值列统计等摘要。

调试检查：

- `generated_code` 可见。
- `stdout` 可见。
- `exit_code = 0`
- `artifacts` 可为空。

### 5.3 CSV 图表生成

用户输入：

```text
给 sales.csv 生成一张趋势图
```

期望：

- `TaskBrief.intent = chart_generation`
- `TaskBrief.context.artifact_expected = true`
- 子 Agent：`CodeAgent`
- 成功时返回 artifact，例如 `chart.png`。
- 前端调试面板显示 artifact URL。

注意：

- 如果执行环境缺少 `matplotlib` 且依赖安装不可用，应返回明确错误。
- 不应编造图表已生成。

### 5.4 Excel 表格数据分析

用户输入：

```text
分析 02.xlsx 表格数据
```

期望：

- `route = simple_task`
- `TaskBrief.delegate_to = code_agent`
- `TaskBrief.intent = data_analysis`
- `TaskBrief.context.path = 02.xlsx`
- 工具调用：`code_sandbox`
- 如果缺少 `openpyxl`，返回明确错误。
- 不应进入 `SimpleChatAgent`。
- 不应编造 Excel 内容。

### 5.5 Excel 图表生成

用户输入：

```text
生成 02.xlsx 的分析结果图
```

期望：

- `route = simple_task`
- `TaskBrief.delegate_to = code_agent`
- `TaskBrief.intent = chart_generation`
- `TaskBrief.context.path = 02.xlsx`
- 成功时 artifact 输出到 `runtime/artifacts/<run_id>/chart.png`。

### 5.6 Excel 分析并要求写 workspace 文件夹

用户输入：

```text
帮我查看02.xlsx表格中的内容，并对他进行数据分析，将分析的结果图新建一个02_analys文件夹存放
```

期望：

- `route = future_task`
- 进入计划确认。
- `TaskBrief.delegate_to = code_agent`
- 不自动创建 `02_analys`。
- 不调用 `SimpleChatAgent`。
- 不编造“已读取表格/已保存图片”。

计划确认后期望：

- 进入 `TaskAgent`。
- Excel 读取/分析步骤分类为 `data_analysis`，委派给 `CodeAgent` 或复用前序 CodeAgent 结果。
- 图表生成步骤分类为 `chart_generation`，委派给 `CodeAgent`。
- “选择图表类型/分析方法”这类步骤可分类为 `manual_review`。
- “新建 02_analys 文件夹”“移动/保存图表到 02_analys”分类为 `workspace_write`。
- workspace 写入步骤状态为 `waiting_confirmation`，不应显示为 `failed`。
- `pending_confirmations` 中应包含 `target_directory = 02_analys`。
- 未二次确认前，不调用 `create_directory` 或 `publish_artifact`。
- 不再出现“6 个步骤全部 Sandbox execution failed”。

## Level 6：CodeAgent 代码分析

### 6.1 分析 Python 文件结构

用户输入：

```text
分析 agent/core/main_agent.py 的代码结构
```

期望：

- `TaskBrief.delegate_to = code_agent`
- `TaskBrief.intent = code_analysis`
- 子 Agent：`CodeAgent`
- 最终回答包含行数、类、函数、导入信息等摘要。
- 不写文件。

### 6.2 分析前端文件

用户输入：

```text
分析 GUI/src/App.jsx 的代码结构
```

期望：

- `TaskBrief.delegate_to = code_agent`
- `TaskBrief.intent = code_analysis`
- 如果文件不在 workspace 沙盒内，应返回明确路径/权限错误。

### 6.3 代码生成但不执行

用户输入：

```text
帮我生成一个读取 csv 的脚本
```

期望：

- `TaskBrief.delegate_to = code_agent`
- `TaskBrief.intent = code_generation`
- 不调用 `code_sandbox`。
- 最终回答包含代码块。
- 不写入 workspace。

### 6.4 用户 Python 脚本执行

前置条件：

```yaml
code_execution:
  enabled: true
  allow_user_script_execution: true
```

用户输入：

````text
运行这段 Python 代码
```python
print("hello")
```
````

期望：

- `TaskBrief.intent = script_execution`
- 子 Agent：`CodeAgent`
- 工具调用：`code_sandbox`
- workspace 只读。
- 输出只能写入 `/artifacts`。
- stdout 中包含 `hello`。

### 6.5 项目结构分析

用户输入：

```text
分析整个项目代码结构
```

期望：

- `TaskBrief.intent = project_analysis`
- 子 Agent：`CodeAgent`
- 工具调用：`code_sandbox`
- 只读扫描 workspace。
- 返回文件数量、文件类型、关键文件和示例文件。

## Level 7：复杂任务计划确认

### 7.1 项目分析报告

用户输入：

```text
帮我分析整个项目并生成一份报告
```

期望：

- `route = future_task`
- 前端显示计划确认弹窗。
- `plan_status = pending`
- 未确认前不执行写入。

### 7.2 计划确认后执行

在上一条计划弹窗中点击确认。

期望：

- `plan_status = approved`
- 进入 `TaskAgent`
- `task_steps` 记录每步状态。
- 搜索步骤委派给 `SearchAgent`。
- 文件步骤委派给 `FileAgent`。
- 数据/图表/代码步骤委派给 `CodeAgent`。
- 高风险写操作仍等待二次确认。
- `task_steps[].step_type` 应能看到 `file_read`、`search`、`data_analysis`、`chart_generation`、`code_analysis`、`workspace_write`、`manual_review` 等分类。
- `workspace_write` 步骤应包含 `requires_confirmation = true` 和 `confirmation_payload`。
- 如 CodeAgent 生成 artifacts，后续写入确认 payload 应能引用这些 artifacts。

## Level 8：协议和调试面板回归

### 8.1 连续对话不污染模型协议

步骤：

1. 先发送：`读取 readme.md`
2. 再发送：`你好`

期望：

- 第二轮普通聊天正常。
- 后端/前端不出现 `messages[x].tool_calls: empty array` 错误。
- 历史消息中的应用 metadata 不进入模型请求协议字段。

### 8.2 调试面板结构

任意发送一条工具任务，例如：

```text
读取 readme.md
```

期望调试面板包含：

- 主 Agent
- 子 Agent
- 工具调用
- 最终输出
- 原始 JSON

`agent_flow` 中应包含：

- `mainAgent`
- `subAgents`
- `tools`
- `finalOutput`
- `errors`

### 8.3 SimpleChat 防幻觉

用户输入：

```text
查看 02.xlsx 表格内容并分析
```

期望：

- 不进入 `simple_chat`。
- 即使模型曾经想回答“我已经读取并保存图片”，最终也不能直接返回这类虚假执行结果。

## Level 9：后端结构回归

本级别主要通过自动化测试验证。

检查点：

- `main_agent.py` 只作为主编排入口。
- prompt 构造函数集中在 `agent/core/utils/prompt.py`。
- routing/debug/responses/metadata 分别在独立模块中。
- `python -m unittest tests.test_prompts -q` 通过。
- `python -m unittest discover -s tests -q` 通过。
