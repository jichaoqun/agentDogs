from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.core.sub_agents import FileAgent, SimpleTaskAgent, TaskAgent, create_default_sub_agent_registry
from agent.core.tools import ToolRegistry, ToolSpec, create_file_tool_registry


class ToolAndAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "notes.md").write_text("hello workspace\nimportant protein note", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
        self.registry = create_file_tool_registry(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tool_registry_registers_lists_and_rejects_duplicates(self):
        registry = ToolRegistry()
        spec = ToolSpec("demo", "demo tool", {}, "low", ["demo"])
        registry.register(spec, lambda payload: None)

        self.assertEqual(registry.get("demo").spec.name, "demo")
        self.assertEqual([item.name for item in registry.list_specs()], ["demo"])
        with self.assertRaises(ValueError):
            registry.register(spec, lambda payload: None)

    def test_file_tools_list_read_search_and_reject_path_traversal(self):
        tree = self.registry.call("list_workspace_tree")
        self.assertTrue(tree.ok)
        names = {item["name"] for item in tree.data["children"]}
        self.assertIn("notes.md", names)

        content = self.registry.call("read_file", {"path": "notes.md"})
        self.assertTrue(content.ok)
        self.assertIn("important protein", content.data["content"])

        search = self.registry.call("search_files", {"query": "protein"})
        self.assertTrue(search.ok)
        self.assertEqual(search.data["matches"][0]["path"], "notes.md")

        rejected = self.registry.call("read_file", {"path": "../README.md"})
        self.assertFalse(rejected.ok)
        self.assertIn("非法路径", rejected.error)

    def test_file_tools_report_binary_and_large_file_errors(self):
        (self.root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        binary = self.registry.call("read_file", {"path": "image.png"})
        self.assertFalse(binary.ok)
        self.assertIn("不支持文本读取", binary.error)

        large = self.root / "large.md"
        large.write_text("x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
        result = self.registry.call("read_file", {"path": "large.md"})
        self.assertFalse(result.ok)
        self.assertIn("超过 2MB", result.error)

    def test_write_file_tool_is_high_risk(self):
        write_spec = self.registry.get("write_file").spec
        self.assertEqual(write_spec.risk_level, "high")

    def test_sub_agent_registry_lists_default_agents(self):
        registry = create_default_sub_agent_registry(self.registry)
        names = {item.name for item in registry.list_specs()}

        self.assertIn("simple_chat", names)
        self.assertIn("simple_task", names)
        self.assertIn("file_agent", names)
        self.assertIn("task_agent", names)
        with self.assertRaises(ValueError):
            registry.register(registry.get("file_agent").spec, None)

    def test_file_agent_reads_files_and_defers_writes(self):
        agent = FileAgent(self.registry)

        read = agent.handle_step("读取 notes.md 并总结")
        self.assertTrue(read.ok)
        self.assertIn("已读取 notes.md", read.content)
        self.assertEqual(read.tool_calls[0]["tool"], "read_file")

        write = agent.handle_step("修改 notes.md 的内容")
        self.assertTrue(write.ok)
        self.assertEqual(write.status, "waiting_confirmation")
        self.assertEqual((self.root / "notes.md").read_text(encoding="utf-8").splitlines()[0], "hello workspace")

    def test_simple_task_agent_executes_low_risk_file_tools(self):
        agent = SimpleTaskAgent(self.registry)

        tree = agent.handle("当前项目中有哪些文件")
        self.assertTrue(tree.ok)
        self.assertIn("notes.md", tree.content)
        self.assertEqual(tree.tool_calls[0]["tool"], "list_workspace_tree")

        read = agent.handle("读取 notes.md")
        self.assertTrue(read.ok)
        self.assertIn("important protein", read.content)
        self.assertEqual(read.tool_calls[0]["tool"], "read_file")

        search = agent.handle("搜索 protein")
        self.assertTrue(search.ok)
        self.assertIn("notes.md", search.content)
        self.assertEqual(search.tool_calls[0]["tool"], "search_files")

    def test_simple_task_agent_defers_high_risk_tools(self):
        agent = SimpleTaskAgent(self.registry)

        result = agent.handle("修改 notes.md")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "waiting_confirmation")
        self.assertEqual((self.root / "notes.md").read_text(encoding="utf-8").splitlines()[0], "hello workspace")

    def test_task_agent_records_step_statuses(self):
        file_agent = FileAgent(self.registry)
        task_agent = TaskAgent(file_agent)

        result = task_agent.execute(
            user_input="请处理 notes.md",
            plan_steps=["读取 notes.md", "修改 notes.md"],
        )

        self.assertEqual(result.status, "waiting_confirmation")
        self.assertEqual(result.steps[0].status, "completed")
        self.assertEqual(result.steps[1].status, "waiting_confirmation")
        self.assertTrue(result.tool_calls)


if __name__ == "__main__":
    unittest.main()
