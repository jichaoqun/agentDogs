# Agent Dogs

Agent Dogs 是一个本地运行的 Agent 助手项目，包含 FastAPI 后端、Vite + React 前端、可配置模型层、workspace 文件工具、搜索工具、可配置 code_sandbox CodeAgent 和分层 Agent 调度。

## 当前核心能力

- 多模型配置：支持 OpenAI-compatible API、Ollama、本地内置模型。
- MainAgent 编排：负责 LangGraph 流程、任务理解、路由、计划确认、子 Agent 委派和最终回答汇总。
- 子 Agent 分工：`SimpleChatAgent`、`SearchAgent`、`FileAgent`、`CodeAgent`、`SimpleTaskAgent`、`TaskAgent`。
- Workspace 工具：列目录、读文件、搜索文件、查看文件信息，以及二次确认后可用的创建目录、发布 artifact 到 workspace，默认限制在 `workspace/` 沙盒内。
- 搜索工具：workspace 搜索、关键词搜索、可选联网搜索。
- CodeAgent：支持 OpenSandbox 或需人工授权的 local_process 后端，用 Python 完成数据分析、图表生成、代码/项目分析、代码生成和受控脚本执行。

### CodeAgent 执行后端

`code_execution.backend` 支持：

- `opensandbox`：连接 OpenSandbox Server 执行，适合作为强隔离沙箱入口。
- `local_process`：不依赖 Docker/OpenSandbox Server，使用本机 Python 进程执行可信任务。它不是强安全沙箱，默认需要人工批准后才会运行。

当前示例配置使用 `local_process`，执行前前端会弹出 `execution_approval` 确认，展示 run id、读取文件、artifact 目录、依赖和网络需求。OpenSandbox 失败不会自动降级到本地执行。
- TaskAgent：复杂任务计划确认后的分步协调器，可复用上下文并把 workspace 写入动作转为二次确认。
- 前端调试面板：可展开查看 MainAgent、SubAgent、Tools、TaskBrief、debug_trace、agent_flow、最终输出和原始 JSON。

## 后端结构

`agent/core/main_agent.py` 现在只保留主编排入口和 LangGraph 节点流转。原先混在 MainAgent 里的领域逻辑已拆分：

- `agent/core/agent_routing.py`：任务规则判断、LLM 任务解析、TaskBrief 构造、路由辅助。
- `agent/core/agent_debug.py`：`debug_trace` 和分层 `agent_flow` 生成。
- `agent/core/agent_responses.py`：Search/Weather/CodeAgent 等结果的最终回答汇总。
- `agent/core/agent_metadata.py`：`response_metadata["agent_dogs"]` metadata 隔离、历史消息记录。
- `agent/core/utils/prompt.py`：默认系统提示、任务解析 prompt、任务规划 prompt、SimpleChat 防幻觉提示。

## 快速启动

后端：

```powershell
python -m uvicorn agent.api.app:app --host 127.0.0.1 --port 8000 --reload --reload-dir agent --reload-dir config
```

前端：

```powershell
cd GUI
npm run dev
```

浏览器访问：

```text
http://127.0.0.1:5173
```

更多运行说明见 [docx/运行说明.md](docx/运行说明.md)。

## 文档入口

- [Agent 技术设计](docx/agent.md)
- [Tools 设计](docx/tools.md)
- [前后端运行说明](docx/运行说明.md)
- [GUI 说明](docx/GUI.md)
- [对话测试用例](docx/test.md)
- [CLI 说明](docs/cli.md)

## 开发验证

后端测试：

```powershell
python -m unittest discover -s tests -q
```

前端构建：

```powershell
cd GUI
npm run build
```
