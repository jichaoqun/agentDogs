"""Interactive command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core.main_agent import MainAgent
from .core.utils.llm_config import ConfigError, load_config
from .core.utils.llm_models import LLMError


HELP = """命令：
  /help     显示帮助
  /clear    清空当前会话上下文
  /models   扫描并显示各后端模型
  /status   显示配置和回退顺序
  /exit     退出
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="agentDogs 命令行对话")
    parser.add_argument("--config", type=Path, help="LLM YAML 配置文件路径")
    parser.add_argument("--message", "-m", help="发送单条消息后退出")
    return parser


def _print_status(agent: MainAgent) -> None:
    enabled = ", ".join(agent.models.providers)
    print(f"配置: {agent.config.source}")
    print(f"默认模型: {agent.config.default_provider}/{agent.config.default_model}")
    print(f"已启用: {enabled or '无'}")


def _ask(agent: MainAgent, text: str) -> bool:
    try:
        result = agent.chat(text)
    except LLMError as exc:
        print(f"\n[错误] {exc}", file=sys.stderr)
        return False
    except ValueError as exc:
        print(f"[提示] {exc}")
        return False
    print(f"\n助手 [{result.provider}/{result.model}]> {result.content}\n")
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        agent = MainAgent(load_config(args.config))
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if args.message:
        return 0 if _ask(agent, args.message) else 1

    print("agentDogs V1 - 命令行对话（输入 /help 查看命令）")
    _print_status(agent)
    while True:
        try:
            text = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not text:
            continue
        command = text.lower()
        if command in {"/exit", "exit", "quit"}:
            print("再见。")
            return 0
        if command == "/help":
            print(HELP)
        elif command == "/clear":
            agent.clear()
            print("会话上下文已清空。")
        elif command == "/status":
            _print_status(agent)
        elif command == "/models":
            try:
                for model in agent.models.list_models():
                    print(f"{model.provider}: {model.model}")
            except LLMError as exc:
                print(f"模型列表读取失败: {exc}")
        else:
            _ask(agent, text)


if __name__ == "__main__":
    raise SystemExit(main())
