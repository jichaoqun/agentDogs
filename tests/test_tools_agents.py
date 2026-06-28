from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.core.sub_agents import CodeAgent, FileAgent, SearchAgent, SimpleTaskAgent, TaskAgent, create_default_sub_agent_registry
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
        (self.root / "02.xlsx").write_bytes(b"fake xlsx fixture")
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
        self.assertEqual(self.registry.get("create_directory").spec.risk_level, "high")
        self.assertEqual(self.registry.get("publish_artifact").spec.risk_level, "high")

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
        self.assertIn("code_agent", names)
        self.assertIn("task_agent", names)
        with self.assertRaises(ValueError):
            registry.register(registry.get("file_agent").spec, None)

    def test_code_agent_uses_sandbox_for_data_analysis(self):
        class FakeSandbox:
            def __init__(self):
                self.code = ""

            def run_python(self, code):
                from agent.core.sandbox import SandboxRunResult

                self.code = code
                return SandboxRunResult(
                    ok=True,
                    run_id="run-1",
                    exit_code=0,
                    stdout="CodeAgent 数据分析摘要\n文件: data.csv\n行数: 2",
                    artifacts=[],
                    duration_ms=12,
                )

        sandbox = FakeSandbox()
        agent = CodeAgent(self.registry, sandbox)
        from agent.core.state import TaskBrief

        result = agent.handle_brief(
            TaskBrief(
                intent="data_analysis",
                user_goal="分析 data.csv",
                normalized_input="分析 data.csv",
                context={"path": "data.csv"},
                source_policy="workspace_only",
                expected_output="返回数据摘要。",
                delegate_to="code_agent",
            )
        )

        self.assertTrue(result.ok)
        self.assertIn("CodeAgent 数据分析摘要", result.content)
        self.assertIn("/workspace", sandbox.code)
        self.assertEqual(result.tool_calls[0]["tool"], "docker_sandbox")

    def test_code_agent_generates_excel_analysis_script(self):
        class FakeSandbox:
            def __init__(self):
                self.code = ""

            def run_python(self, code):
                from agent.core.sandbox import SandboxRunResult

                self.code = code
                return SandboxRunResult(
                    ok=True,
                    run_id="run-2",
                    exit_code=0,
                    stdout="CodeAgent 数据分析摘要\n文件: 02.xlsx\n行数: 10\n数值列 销量: count=10 min=1 max=9 mean=5",
                    artifacts=[],
                    duration_ms=10,
                )

        sandbox = FakeSandbox()
        agent = CodeAgent(self.registry, sandbox)
        from agent.core.state import TaskBrief

        result = agent.handle_brief(
            TaskBrief(
                intent="data_analysis",
                user_goal="分析 02.xlsx 表格数据",
                normalized_input="分析 02.xlsx 表格数据",
                context={"path": "02.xlsx"},
                source_policy="workspace_only",
                expected_output="返回数据摘要。",
                delegate_to="code_agent",
            )
        )

        self.assertTrue(result.ok)
        self.assertIn("02.xlsx", sandbox.code)
        self.assertIn("openpyxl", sandbox.code)
        self.assertIn("缺失值", sandbox.code)
        self.assertEqual(result.tool_calls[0]["payload"]["path"], "02.xlsx")

    def test_code_agent_generates_excel_chart_script(self):
        class FakeSandbox:
            def __init__(self):
                self.code = ""

            def run_python(self, code):
                from agent.core.sandbox import SandboxRunResult

                self.code = code
                return SandboxRunResult(
                    ok=True,
                    run_id="run-3",
                    exit_code=0,
                    stdout="CodeAgent 图表生成完成\n图表: /artifacts/chart.png",
                    artifacts=[{"filename": "chart.png", "url": "/api/v1/artifacts/run-3/chart.png"}],
                    duration_ms=15,
                )

        sandbox = FakeSandbox()
        agent = CodeAgent(self.registry, sandbox)
        from agent.core.state import TaskBrief

        result = agent.handle_brief(
            TaskBrief(
                intent="chart_generation",
                user_goal="生成 02.xlsx 的分析结果图",
                normalized_input="生成 02.xlsx 的分析结果图",
                context={"path": "02.xlsx"},
                source_policy="workspace_only",
                expected_output="返回图表。",
                delegate_to="code_agent",
            )
        )

        self.assertTrue(result.ok)
        self.assertIn("02.xlsx", sandbox.code)
        self.assertIn("openpyxl", sandbox.code)
        self.assertIn("chart.png", sandbox.code)
        self.assertEqual(result.data["artifacts"][0]["filename"], "chart.png")

    def test_code_agent_code_generation_does_not_execute_sandbox(self):
        class FakeSandbox:
            def __init__(self):
                self.called = False

            def run(self, request):
                self.called = True
                raise AssertionError("code_generation must not execute sandbox")

        sandbox = FakeSandbox()
        agent = CodeAgent(self.registry, sandbox)
        from agent.core.state import TaskBrief

        result = agent.handle_brief(
            TaskBrief(
                intent="code_generation",
                user_goal="帮我生成一个读取 csv 的脚本",
                normalized_input="帮我生成一个读取 csv 的脚本",
                context={"path": "data.csv"},
                source_policy="workspace_only",
                expected_output="返回代码。",
                delegate_to="code_agent",
            )
        )

        self.assertTrue(result.ok)
        self.assertFalse(sandbox.called)
        self.assertIn("```python", result.content)

    def test_code_agent_user_script_execution_uses_sandbox(self):
        from agent.core.sandbox import SandboxRunResult
        from agent.core.utils.llm_config import CodeExecutionConfig

        class FakeSandbox:
            def __init__(self):
                self.config = CodeExecutionConfig(enabled=True, allow_user_script_execution=True)
                self.request = None

            def run(self, request):
                self.request = request
                return SandboxRunResult(ok=True, run_id="run-script", exit_code=0, stdout="hello", duration_ms=1)

        sandbox = FakeSandbox()
        agent = CodeAgent(self.registry, sandbox)
        from agent.core.state import TaskBrief

        result = agent.handle_brief(
            TaskBrief(
                intent="script_execution",
                user_goal="运行这段 Python 代码",
                normalized_input="运行这段 Python 代码\n```python\nprint('hello')\n```",
                context={"user_code": "print('hello')", "language": "python"},
                source_policy="workspace_only",
                expected_output="返回 stdout。",
                delegate_to="code_agent",
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.tool_calls[0]["tool"], "docker_sandbox")
        self.assertIn("print('hello')", sandbox.request.code)
        self.assertEqual(result.tool_calls[0]["payload"]["workspace"], "readonly")

    def test_code_agent_user_script_execution_can_be_disabled(self):
        from agent.core.utils.llm_config import CodeExecutionConfig

        class FakeSandbox:
            config = CodeExecutionConfig(enabled=True, allow_user_script_execution=False)

            def run(self, request):
                raise AssertionError("disabled script execution must not run")

        agent = CodeAgent(self.registry, FakeSandbox())
        from agent.core.state import TaskBrief

        result = agent.handle_brief(
            TaskBrief(
                intent="script_execution",
                user_goal="运行这段 Python 代码",
                normalized_input="```python\nprint('hello')\n```",
                context={"user_code": "print('hello')"},
                source_policy="workspace_only",
                expected_output="返回 stdout。",
                delegate_to="code_agent",
            )
        )

        self.assertFalse(result.ok)
        self.assertIn("未启用", result.error)

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

    def test_task_agent_dispatches_code_steps_to_code_agent(self):
        class FakeCodeAgent:
            def handle_brief(self, brief):
                from agent.core.sub_agents.registry import SubAgentResult

                return SubAgentResult.success(
                    "CodeAgent handled",
                    data={"artifacts": [{"filename": "chart.png", "url": "/api/v1/artifacts/run-1/chart.png"}]},
                    tool_calls=[{"tool": "docker_sandbox", "payload": {"path": brief.context.get("path")}, "ok": True}],
                )

        task_agent = TaskAgent(FileAgent(self.registry), SearchAgent(self.registry), FakeCodeAgent())

        result = task_agent.execute(
            user_input="分析 02.xlsx 并生成图表",
            plan_steps=["读取并分析 02.xlsx 表格数据", "生成分析结果图"],
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.steps[0].assigned_agent, "CodeAgent")
        self.assertEqual(result.steps[1].assigned_agent, "CodeAgent")
        self.assertEqual(result.tool_calls[0]["tool"], "docker_sandbox")
        self.assertEqual(result.artifacts[0]["filename"], "chart.png")

    def test_task_agent_classifies_workspace_write_as_confirmation(self):
        class FakeCodeAgent:
            def handle_brief(self, brief):
                from agent.core.sub_agents.registry import SubAgentResult

                return SubAgentResult.success(
                    "CodeAgent analyzed 02.xlsx",
                    data={"artifacts": [{"filename": "chart.png", "url": "/api/v1/artifacts/run-1/chart.png"}]},
                    tool_calls=[{"tool": "docker_sandbox", "payload": {"path": brief.context.get("path")}, "ok": True}],
                )

        task_agent = TaskAgent(FileAgent(self.registry), SearchAgent(self.registry), FakeCodeAgent())

        result = task_agent.execute(
            user_input="帮我查看02.xlsx表格中的内容，并对他进行数据分析，将分析的结果图新建一个02_analys文件夹存放",
            plan_steps=[
                "检查当前目录下是否存在02.xlsx文件，并确认其可读性。",
                "打开并读取02.xlsx文件，查看数据结构和内容。",
                "对数据进行初步探索性分析（如统计摘要、缺失值检查等）。",
                "根据需要选择适合的分析方法和图表类型（如折线图、柱状图、散点图等）。",
                "生成分析图表并保存为图片或PDF格式。",
                "在当前目录下新建名为02_analys的文件夹。",
                "将所有生成的图表文件移动到或直接保存到02_analys文件夹中。",
                "确认图表已正确保存至02_analys文件夹，并列出文件列表。",
            ],
        )

        self.assertEqual(result.status, "waiting_confirmation")
        self.assertEqual(result.steps[0].step_type, "file_info")
        self.assertEqual(result.steps[0].assigned_agent, "FileAgent")
        self.assertEqual(result.steps[0].status, "completed")
        self.assertEqual(result.steps[1].step_type, "data_analysis")
        self.assertEqual(result.steps[1].assigned_agent, "CodeAgent")
        self.assertEqual(result.steps[3].step_type, "manual_review")
        self.assertEqual(result.steps[4].step_type, "chart_generation")
        self.assertEqual(result.steps[4].assigned_agent, "CodeAgent")
        self.assertEqual(result.steps[5].step_type, "workspace_write")
        self.assertEqual(result.steps[6].step_type, "workspace_write")
        self.assertEqual(result.steps[7].step_type, "manual_review")
        self.assertEqual(result.steps[5].status, "waiting_confirmation")
        self.assertEqual(result.steps[6].status, "waiting_confirmation")
        self.assertTrue(result.pending_confirmations)
        self.assertIn("02_analys", {item["target_directory"] for item in result.pending_confirmations})
        self.assertFalse(any(step.assigned_agent == "CodeAgent" for step in result.steps[5:]))
        self.assertTrue(any(event["stage"] == "TaskAgent.step" and event["agent"] == "CodeAgent" for event in result.debug_events))
        self.assertTrue(any(call["tool"] == "file_info" for call in result.tool_calls))

    def test_task_agent_surfaces_code_agent_failure_details(self):
        class FakeCodeAgent:
            def handle_brief(self, brief):
                from agent.core.sub_agents.registry import SubAgentResult

                return SubAgentResult.failure(
                    "Sandbox execution failed.",
                    data={"sandbox": {"stderr": "ModuleNotFoundError: No module named 'openpyxl'", "exit_code": 1}},
                    next_actions=["启用依赖安装或使用包含 openpyxl 的镜像。"],
                    tool_calls=[
                        {
                            "tool": "docker_sandbox",
                            "payload": {"path": brief.context.get("path"), "task_type": brief.intent},
                            "ok": False,
                            "error": "Sandbox execution failed.",
                        }
                    ],
                )

        task_agent = TaskAgent(FileAgent(self.registry), SearchAgent(self.registry), FakeCodeAgent())

        result = task_agent.execute(
            user_input="分析 02.xlsx",
            plan_steps=["使用Python的pandas库读取02.xlsx文件，查看列名、数据类型、缺失值和前几行数据。"],
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.steps[0].assigned_agent, "CodeAgent")
        self.assertIn("ModuleNotFoundError", result.steps[0].error)
        self.assertIn("openpyxl", result.steps[0].result)
        self.assertTrue(any(event["error"] and "ModuleNotFoundError" in event["error"] for event in result.debug_events))

    def test_task_agent_does_not_treat_every_confirmation_as_file_info(self):
        class FakeCodeAgent:
            def handle_brief(self, brief):
                from agent.core.sub_agents.registry import SubAgentResult

                artifacts = []
                if brief.intent == "chart_generation":
                    artifacts = [{"filename": "chart.png", "url": "/api/v1/artifacts/run-1/chart.png"}]
                return SubAgentResult.success(
                    f"CodeAgent handled {brief.intent}",
                    data={"artifacts": artifacts},
                    tool_calls=[
                        {
                            "tool": "docker_sandbox",
                            "payload": {"path": brief.context.get("path"), "task_type": brief.intent},
                            "ok": True,
                        }
                    ],
                )

        task_agent = TaskAgent(FileAgent(self.registry), SearchAgent(self.registry), FakeCodeAgent())

        result = task_agent.execute(
            user_input="帮我查看02.xlsx表格中的内容，并对他进行数据分析，将分析的结果图新建一个02_analys文件夹存放",
            plan_steps=[
                "定位并读取02.xlsx文件，确认文件存在且为有效Excel格式。",
                "展示表格内容摘要（行列数、列名、样本数据），供用户确认数据概况。",
                "基于用户确认的分析目标（如分布、趋势、相关性等）进行数据分析，生成图表。",
                "在当前目录下创建名为02_analys的文件夹（如已存在则跳过）。",
                "将生成的图表保存到02_analys文件夹中，并列出保存的文件清单。",
                "确认所有图表已成功保存，提供分析总结。",
            ],
        )

        self.assertEqual(result.status, "waiting_confirmation")
        self.assertEqual(result.steps[0].step_type, "file_info")
        self.assertEqual(result.steps[1].step_type, "data_analysis")
        self.assertEqual(result.steps[1].assigned_agent, "CodeAgent")
        self.assertEqual(result.steps[2].step_type, "chart_generation")
        self.assertEqual(result.steps[2].assigned_agent, "CodeAgent")
        self.assertEqual(result.steps[3].step_type, "workspace_write")
        self.assertEqual(result.steps[4].step_type, "workspace_write")
        self.assertEqual(result.steps[5].step_type, "manual_review")
        self.assertFalse(any(step.step_type == "file_info" for step in result.steps[1:]))

    def test_task_agent_extracts_explicit_output_directory(self):
        task_agent = TaskAgent(FileAgent(self.registry), SearchAgent(self.registry))

        result = task_agent.execute(
            user_input="分析 02.xlsx 并把图表放到 02_analys",
            plan_steps=[
                "在当前目录下创建02_analys文件夹（如不存在），将生成的图表保存为PNG或PDF文件",
            ],
        )

        self.assertEqual(result.status, "waiting_confirmation")
        self.assertEqual(result.pending_confirmations[0]["target_directory"], "02_analys")


if __name__ == "__main__":
    unittest.main()
