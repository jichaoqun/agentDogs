from __future__ import annotations

import unittest

from agent.core.utils.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    SIMPLE_CHAT_TOOL_GUARD_PROMPT,
    build_simple_chat_system_prompt,
    build_task_analysis_prompt,
    build_task_plan_prompt,
)


class PromptBuilderTests(unittest.TestCase):
    def test_task_analysis_prompt_contains_contract_context_and_input(self):
        prompt = build_task_analysis_prompt("分析 02.xlsx", "当前日期：2026-06-28")

        self.assertIn("只返回 JSON", prompt)
        self.assertIn("complexity", prompt)
        self.assertIn("route_hint", prompt)
        self.assertIn("分析 02.xlsx", prompt)
        self.assertIn("当前日期：2026-06-28", prompt)

    def test_task_plan_prompt_contains_answers_feedback_and_input(self):
        prompt = build_task_plan_prompt(
            user_input="生成分析图",
            clarification_answers={"output": "artifacts"},
            plan_feedback="先检查数据",
            current_time="当前日期：2026-06-28",
        )

        self.assertIn("只返回 JSON", prompt)
        self.assertIn("summary, steps, risks", prompt)
        self.assertIn("生成分析图", prompt)
        self.assertIn("- output: artifacts", prompt)
        self.assertIn("先检查数据", prompt)
        self.assertIn("当前日期：2026-06-28", prompt)

    def test_simple_chat_prompt_adds_tool_guard_and_time(self):
        prompt = build_simple_chat_system_prompt(DEFAULT_SYSTEM_PROMPT, "当前日期：2026-06-28")

        self.assertIn(DEFAULT_SYSTEM_PROMPT, prompt)
        self.assertIn(SIMPLE_CHAT_TOOL_GUARD_PROMPT, prompt)
        self.assertIn("不要声称已经完成", prompt)
        self.assertIn("当前日期：2026-06-28", prompt)


if __name__ == "__main__":
    unittest.main()
