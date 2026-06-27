from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.core.sub_agents import FileAgent, SearchAgent, SimpleTaskAgent, TaskAgent, create_default_sub_agent_registry
from agent.core.tools import ToolRegistry, ToolSpec, create_default_tool_registry
from agent.core.utils.llm_config import SearchConfig


class FakeHttpResponse:
    def __init__(self, text: str, status_code: int = 200, headers: dict[str, str] | None = None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}


class ToolAndAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "notes.md").write_text("hello workspace\nimportant protein note", encoding="utf-8")
        (self.root / "add_new.md").write_text("# 新增的测试说明", encoding="utf-8")
        (self.root / "readme.md").write_text("默认文件存储目录", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
        self.registry = create_default_tool_registry(self.root)

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

    def test_search_tools_return_structured_results_and_safe_web_failure(self):
        workspace = self.registry.call("workspace_search", {"query": "protein"})
        self.assertTrue(workspace.ok)
        self.assertEqual(workspace.data["results"][0]["path"], "notes.md")
        self.assertEqual(workspace.data["results"][0]["source"], "workspace")

        keyword = self.registry.call("keyword_search", {"query": "protein", "text": "alpha\nprotein beta"})
        self.assertTrue(keyword.ok)
        self.assertEqual(keyword.data["results"][0]["title"], "文本第 2 行")

        empty = self.registry.call("workspace_search", {"query": ""})
        self.assertFalse(empty.ok)
        self.assertIn("关键词不能为空", empty.error)

        web = self.registry.call("web_search", {"query": "langgraph"})
        self.assertFalse(web.ok)
        self.assertIn("联网搜索未启用", web.error)

    def test_web_search_fetches_and_extracts_page_content(self):
        registry = create_default_tool_registry(
            self.root,
            SearchConfig(enabled=True, max_results=2, fetch_pages=1, timeout=2),
        )
        search_html = """
        <html><body>
          <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Flanggraph">LangGraph docs</a>
          <div class="result__snippet">Build reliable agents with graph orchestration.</div>
          <a class="result__a" href="/l/?uddg=http%3A%2F%2F127.0.0.1%2Fprivate">Unsafe local result</a>
          <div class="result__snippet">Should not be fetched.</div>
        </body></html>
        """
        page_html = """
        <html>
          <head><title>LangGraph Overview</title><script>ignore()</script></head>
          <body><nav>menu</nav><main>LangGraph is a framework for building stateful agents.</main></body>
        </html>
        """

        def fake_get(url, **kwargs):
            if "duckduckgo.com" in url:
                return FakeHttpResponse(search_html)
            if url == "https://example.com/langgraph":
                return FakeHttpResponse(page_html)
            raise AssertionError(f"unexpected URL: {url}")

        with patch("agent.core.tools.search_tools.httpx.get", side_effect=fake_get):
            result = registry.call("web_search", {"query": "LangGraph", "max_results": 2, "fetch_pages": 2})

        self.assertTrue(result.ok)
        results = result.data["results"]
        self.assertEqual(results[0]["url"], "https://example.com/langgraph")
        self.assertTrue(results[0]["fetched"])
        self.assertIn("stateful agents", results[0]["content_excerpt"])
        self.assertEqual(results[1]["url"], "http://127.0.0.1/private")
        self.assertFalse(results[1]["fetched"])
        self.assertIn("安全策略", results[1]["error"])

    def test_web_search_reports_http_failures(self):
        registry = create_default_tool_registry(self.root, SearchConfig(enabled=True))

        def fake_get(url, **kwargs):
            raise TimeoutError("network slow")

        with patch("agent.core.tools.search_tools.httpx.get", side_effect=fake_get):
            result = registry.call("web_search", {"query": "LangGraph"})

        self.assertFalse(result.ok)
        self.assertIn("联网搜索失败", result.error)

    def test_sub_agent_registry_lists_default_agents(self):
        registry = create_default_sub_agent_registry(self.registry)
        names = {item.name for item in registry.list_specs()}

        self.assertIn("simple_chat", names)
        self.assertIn("simple_task", names)
        self.assertIn("file_agent", names)
        self.assertIn("search_agent", names)
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

    def test_file_agent_extracts_paths_from_chinese_read_requests(self):
        agent = FileAgent(self.registry)

        cases = [
            ("帮我查看add_new.md中的内容", "add_new.md", "新增的测试说明"),
            ("帮我查看readme.md中的内容", "readme.md", "默认文件存储目录"),
            ("add_new.md中的内容是什么", "add_new.md", "新增的测试说明"),
            ("读取 add_new.md", "add_new.md", "新增的测试说明"),
            ("查看 add_new.md", "add_new.md", "新增的测试说明"),
            ("帮我查看 `add_new.md` 中的内容", "add_new.md", "新增的测试说明"),
        ]
        for prompt, path, expected in cases:
            with self.subTest(prompt=prompt):
                result = agent.handle_step(prompt)
                self.assertTrue(result.ok)
                self.assertEqual(result.tool_calls[0]["payload"]["path"], path)
                self.assertIn(expected, result.content)

    def test_simple_task_agent_executes_low_risk_file_tools(self):
        agent = SimpleTaskAgent(self.registry, SearchAgent(self.registry))

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
        self.assertEqual(search.tool_calls[0]["tool"], "workspace_search")

    def test_simple_task_agent_extracts_paths_from_chinese_read_requests(self):
        agent = SimpleTaskAgent(self.registry, SearchAgent(self.registry))

        cases = [
            ("帮我查看add_new.md中的内容", "add_new.md", "新增的测试说明"),
            ("帮我查看readme.md中的内容", "readme.md", "默认文件存储目录"),
            ("add_new.md中的内容是什么", "add_new.md", "新增的测试说明"),
            ("读取 add_new.md", "add_new.md", "新增的测试说明"),
            ("查看 add_new.md", "add_new.md", "新增的测试说明"),
            ("帮我查看 `add_new.md` 中的内容", "add_new.md", "新增的测试说明"),
        ]
        for prompt, path, expected in cases:
            with self.subTest(prompt=prompt):
                result = agent.handle(prompt)
                self.assertTrue(result.ok)
                self.assertEqual(result.tool_calls[0]["payload"]["path"], path)
                self.assertIn(expected, result.content)

    def test_simple_task_agent_defers_high_risk_tools(self):
        agent = SimpleTaskAgent(self.registry)

        result = agent.handle("修改 notes.md")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "waiting_confirmation")
        self.assertEqual((self.root / "notes.md").read_text(encoding="utf-8").splitlines()[0], "hello workspace")

    def test_search_agent_formats_workspace_and_web_results(self):
        agent = SearchAgent(self.registry)

        workspace = agent.handle("搜索 protein")
        self.assertTrue(workspace.ok)
        self.assertIn("workspace 搜索找到", workspace.content)
        self.assertIn("notes.md", workspace.content)
        self.assertEqual(workspace.tool_calls[0]["tool"], "workspace_search")

        web = agent.handle("联网查一下 langgraph")
        self.assertTrue(web.ok)
        self.assertIn("联网搜索未启用", web.content)
        self.assertEqual(web.tool_calls[0]["tool"], "web_search")

    def test_task_agent_records_step_statuses(self):
        file_agent = FileAgent(self.registry)
        task_agent = TaskAgent(file_agent, SearchAgent(self.registry))

        result = task_agent.execute(
            user_input="请处理 notes.md",
            plan_steps=["读取 notes.md", "搜索 protein", "修改 notes.md"],
        )

        self.assertEqual(result.status, "waiting_confirmation")
        self.assertEqual(result.steps[0].status, "completed")
        self.assertEqual(result.steps[1].assigned_agent, "SearchAgent")
        self.assertEqual(result.steps[1].status, "completed")
        self.assertEqual(result.steps[2].status, "waiting_confirmation")
        self.assertTrue(result.tool_calls)


if __name__ == "__main__":
    unittest.main()
