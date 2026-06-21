# 命令行对话

请在项目根目录运行 `python -m agent`（`python -m agent.cli` 也可以），或用
`python -m agent -m "你好"` 做单次测试。包内模块使用相对导入，因此不应进入
`agent` 目录后直接执行 `python cli.py`。

默认配置位于 `config/llm.yaml`。API 密钥建议通过 `OPENAI_API_KEY` 环境变量提供。`default_model` 决定 CLI 默认使用的 Provider 和模型；模型调用不会在 API、Ollama 与内置模型之间自动回退。

CLI 支持 `/models`、`/status`、`/clear`、`/help` 和 `/exit`。
