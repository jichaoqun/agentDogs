from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agent.core.main_agent import AgentInterruptError, AgentRunCancelled, MainAgent
from agent.core.tools import ToolRegistry, ToolResult, ToolSpec, create_default_tool_registry, create_file_tool_registry
from agent.core.utils.llm_config import AppConfig, CodeExecutionConfig, ProviderConfig, load_config
from agent.core.utils.llm_models import (
    GenerationOptions,
    ModelInfo,
    ModelInvocationError,
    ModelManager,
    ModelProvider,
    ModelSelection,
    OllamaProvider,
)


class FakeProvider(ModelProvider):
    provider_id = "fake"

    def __init__(self, config, reply="ok", fail=False):
        super().__init__(config)
        self.reply, self.fail, self.seen = reply, fail, []

    def list_models(self):
        return [ModelInfo(self.provider_id, "demo", "Demo")]

    def invoke(self, messages, model, options):
        self.seen.append((messages, model, options))
        if self.fail:
            raise ModelInvocationError("offline")
        if isinstance(self.reply, AIMessage):
            return self.reply
        return AIMessage(content=self.reply)


class FakeChatClient:
    def __init__(self):
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return AIMessage(content="ok")


def make_config():
    return AppConfig(
        "system",
        4,
        {name: ProviderConfig(enabled=True) for name in ("api", "ollama", "builtin")},
        Path("test.yaml"),
        default_provider="builtin",
        default_model="builtin",
    )


def make_manager(*, provider="api", reply="answer", fail=False):
    manager = ModelManager(make_config())
    fake = FakeProvider(ProviderConfig(), reply=reply, fail=fail)
    manager.providers = {provider: fake}
    manager.default_selection = ModelSelection(provider, "demo")
    return manager, fake


class MainFlowTests(unittest.TestCase):
    def test_ollama_generation_settings_are_passed_in_options(self):
        class RecordingClient:
            def __init__(self):
                self.kwargs = None

            def invoke(self, messages, **kwargs):
                self.kwargs = kwargs
                return AIMessage(content="ok")

        provider = OllamaProvider(ProviderConfig(temperature=0.6, max_tokens=900))
        client = RecordingClient()
        provider._clients["qwen"] = client

        provider.invoke(
            [],
            "qwen",
            GenerationOptions(temperature=0.3, max_tokens=256, thinking_enabled=True),
        )

        self.assertEqual(client.kwargs["options"], {"temperature": 0.3, "num_predict": 256})
        self.assertTrue(client.kwargs["reasoning"])

    def test_manager_invokes_only_the_explicitly_selected_provider(self):
        manager = ModelManager(make_config())
        api = FakeProvider(ProviderConfig(), reply="api answer")
        ollama = FakeProvider(ProviderConfig(), reply="ollama answer")
        manager.providers = {"api": api, "ollama": ollama}

        result = manager.chat([], ModelSelection("ollama", "qwen3:8b"))

        self.assertEqual((result.provider, result.model), ("ollama", "qwen3:8b"))
        self.assertEqual(result.content, "ollama answer")
        self.assertEqual(len(api.seen), 0)
        self.assertEqual(len(ollama.seen), 1)

    def test_manager_does_not_fallback_after_selected_provider_fails(self):
        manager = ModelManager(make_config())
        api = FakeProvider(ProviderConfig(), fail=True)
        ollama = FakeProvider(ProviderConfig(), reply="must not be used")
        manager.providers = {"api": api, "ollama": ollama}

        with self.assertRaises(ModelInvocationError):
            manager.chat([], ModelSelection("api", "demo"))

        self.assertEqual(len(ollama.seen), 0)

    def test_manager_extracts_ollama_native_thinking_metadata(self):
        manager = ModelManager(make_config())
        fake = FakeProvider(
            ProviderConfig(),
            reply=AIMessage(content="最终回答", additional_kwargs={"thinking": "内部思考"}),
        )
        manager.providers = {"ollama": fake}

        result = manager.chat(
            [],
            ModelSelection("ollama", "demo"),
            GenerationOptions(thinking_enabled=True),
        )

        self.assertEqual(result.content, "最终回答")
        self.assertEqual(result.reasoning, "内部思考")

    def test_manager_reports_thinking_only_ollama_response(self):
        manager = ModelManager(make_config())
        fake = FakeProvider(
            ProviderConfig(),
            reply=AIMessage(content="", additional_kwargs={"thinking": "还在思考"}),
        )
        manager.providers = {"ollama": fake}

        with self.assertRaisesRegex(ModelInvocationError, "只返回了思考过程"):
            manager.chat(
                [],
                ModelSelection("ollama", "demo"),
                GenerationOptions(thinking_enabled=True),
            )

    def test_ollama_provider_passes_generation_params_as_options(self):
        provider = OllamaProvider(ProviderConfig())
        client = FakeChatClient()
        provider._clients["demo"] = client

        result = provider.invoke(
            [],
            "demo",
            GenerationOptions(
                temperature=0.4,
                max_tokens=512,
                thinking_enabled=True,
                extra={"options": {"top_p": 0.8}},
            ),
        )

        self.assertEqual(result.content, "ok")
        kwargs = client.calls[0][1]
        self.assertEqual(kwargs["options"]["temperature"], 0.4)
        self.assertEqual(kwargs["options"]["num_predict"], 512)
        self.assertEqual(kwargs["options"]["top_p"], 0.8)
        self.assertTrue(kwargs["reasoning"])
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("num_predict", kwargs)

    def test_simple_question_routes_to_simple_chat_agent(self):
        manager, fake = make_manager(reply="你好，我在。")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("你好")

        self.assertEqual(result.content, "你好，我在。")
        self.assertEqual(agent.last_state["route"], "simple_chat")
        self.assertEqual(len(fake.seen), 1)

    def test_cancelled_run_does_not_record_late_model_response(self):
        manager = ModelManager(make_config())
        cancelled = [False]

        class CancellingProvider(FakeProvider):
            def invoke(self, messages, model, options):
                response = super().invoke(messages, model, options)
                cancelled[0] = True
                return response

        fake = CancellingProvider(ProviderConfig(), reply="late answer")
        manager.providers = {"fake": fake}
        manager.default_selection = ModelSelection("fake", "demo")
        agent = MainAgent(make_config(), manager)

        with self.assertRaises(AgentRunCancelled):
            agent.chat("你好", is_cancelled=lambda: cancelled[0])

        self.assertEqual(agent.history.messages, [])
        self.assertEqual(len(fake.seen), 1)

    def test_complex_project_task_routes_to_future_task_plan(self):
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("帮我分析整个项目并生成报告")

        self.assertEqual(agent.last_state["route"], "future_task")
        self.assertEqual(agent.last_state["status"], "interrupted")
        self.assertEqual(agent.last_state["interrupt"]["type"], "plan_confirmation")
        self.assertIn("执行计划", result.content)
        self.assertEqual(agent.last_state["plan_status"], "pending")
        self.assertEqual(len(fake.seen), 1)
        self.assertEqual([item.type for item in agent.history.messages], ["human", "ai"])

    def test_missing_file_target_routes_to_clarify(self):
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("帮我处理这个文件")

        self.assertEqual(agent.last_state["route"], "clarify")
        self.assertEqual(agent.last_state["status"], "interrupted")
        self.assertEqual(agent.last_state["interrupt"]["type"], "clarification")
        self.assertIn("需要补充信息", result.content)
        questions = agent.last_state["clarification_questions"]
        self.assertGreaterEqual(len(questions), 1)
        self.assertTrue(all(item.question for item in questions))
        self.assertTrue(all(item.allow_custom for item in questions))
        self.assertEqual(len(fake.seen), 0)

    def test_file_list_routes_to_simple_task_without_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("hello", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
            manager, fake = make_manager(reply="must not be used")
            agent = MainAgent(make_config(), manager, create_file_tool_registry(root))

            result = agent.chat("当前项目中有哪些文件")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["status"], "completed")
        self.assertIn("notes.md", result.content)
        self.assertIn("src/", result.content)
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "list_workspace_tree")
        self.assertEqual(len(fake.seen), 0)
        self.assertIsNone(agent.pending_interrupt)

    def test_simple_task_after_chat_does_not_reuse_previous_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("hello", encoding="utf-8")
            manager, fake = make_manager(reply="上一轮聊天回答")
            agent = MainAgent(make_config(), manager, create_file_tool_registry(root))

            first = agent.chat("你好", thread_id="same-session")
            second = agent.chat("当前项目中有哪些文件", thread_id="same-session")

        self.assertEqual(first.content, "上一轮聊天回答")
        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["status"], "completed")
        self.assertIn("notes.md", second.content)
        self.assertNotEqual(second.content, first.content)
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "list_workspace_tree")
        self.assertEqual(len(fake.seen), 1)

    def test_agent_metadata_is_not_replayed_as_model_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("hello", encoding="utf-8")
            manager, fake = make_manager(reply="chat answer")
            agent = MainAgent(make_config(), manager, create_file_tool_registry(root))

            agent.chat("璇诲彇 notes.md", thread_id="metadata-session")
            result = agent.chat("浣犲ソ", thread_id="metadata-session")

        self.assertEqual(result.content, "chat answer")
        messages = fake.seen[-1][0]
        ai_messages = [item for item in messages if item.type == "ai"]
        self.assertTrue(ai_messages)
        for message in ai_messages:
            self.assertNotIn("tool_calls", getattr(message, "additional_kwargs", {}) or {})
            self.assertNotIn("agent_dogs", getattr(message, "response_metadata", {}) or {})

    def test_simple_chat_returns_agent_flow(self):
        manager, fake = make_manager(reply="chat answer")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("hello", thread_id="flow-chat")

        self.assertEqual(result.content, "chat answer")
        flow = agent.response_metadata()["agent_flow"]
        self.assertEqual(flow["mainAgent"]["name"], "MainAgent")
        self.assertEqual(flow["mainAgent"]["route"], "simple_chat")
        self.assertEqual(flow["finalOutput"], "chat answer")
        self.assertTrue(any(item["name"] == "SimpleChatAgent" for item in flow["subAgents"]))
        self.assertEqual(flow["tools"], [])
        self.assertEqual(len(fake.seen), 1)

    def test_file_read_returns_layered_agent_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("hello file", encoding="utf-8")
            manager, fake = make_manager(reply="must not be used")
            agent = MainAgent(make_config(), manager, create_file_tool_registry(root))

            result = agent.chat("读取 notes.md", thread_id="flow-file")

        self.assertIn("hello file", result.content)
        flow = agent.response_metadata()["agent_flow"]
        self.assertEqual(flow["mainAgent"]["route"], "simple_task")
        self.assertTrue(any(item["name"] == "FileAgent" for item in flow["subAgents"]))
        self.assertTrue(any(item["name"] == "read_file" for item in flow["tools"]))
        self.assertEqual(len(fake.seen), 0)

    def test_explicit_file_read_routes_to_simple_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "你好.md").write_text("hello file", encoding="utf-8")
            manager, fake = make_manager(reply="must not be used")
            agent = MainAgent(make_config(), manager, create_file_tool_registry(root))

            result = agent.chat("读取 你好.md")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["status"], "completed")
        self.assertIn("hello file", result.content)
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "read_file")
        self.assertEqual(len(fake.seen), 0)

    def test_workspace_search_routes_to_simple_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("important protein note", encoding="utf-8")
            manager, fake = make_manager(reply="must not be used")
            agent = MainAgent(make_config(), manager, create_default_tool_registry(root))

            result = agent.chat("搜索 workspace 中的 protein")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["status"], "completed")
        self.assertIn("notes.md", result.content)
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "workspace_search")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "search_agent")
        self.assertEqual(len(fake.seen), 0)

    def test_web_search_without_config_returns_controlled_message(self):
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("联网查一下 langgraph")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["status"], "completed")
        self.assertIn("联网搜索未启用", result.content)
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "web_search")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "search_agent")
        self.assertEqual(len(fake.seen), 0)

    def test_weather_search_builds_task_brief_and_uses_web_search(self):
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("帮我搜素北京今天的天气")

        self.assertEqual(agent.last_state["route"], "simple_task")
        brief = agent.last_state["task_brief"]
        self.assertEqual(brief.intent, "weather_lookup")
        self.assertEqual(brief.delegate_to, "search_agent")
        self.assertEqual(brief.context["location"], "北京")
        self.assertEqual(brief.context["relative_time"], "今天")
        self.assertEqual(brief.source_policy, "requires_fresh_external_info")
        self.assertIn("联网搜索未启用", result.content)
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "web_search")
        self.assertEqual(len(fake.seen), 0)

    def test_weather_search_final_answer_is_synthesized(self):
        registry = ToolRegistry()

        def fake_web_search(payload):
            return ToolResult.success(
                "联网搜索找到 2 个结果。",
                data={
                    "query": payload["query"],
                    "results": [
                        {
                            "title": "北京天气预报_2026年06月28日北京市天气",
                            "source": "www.tianqi.com",
                            "url": "https://www.tianqi.com/tianqi/beijing/20260628.html",
                            "summary": "北京 2026年06月28日 21~29° 小雨 44 优",
                            "content_excerpt": "当前位置 : 北京天气预报北京2026年06月28日天气 21~29° 小雨 44 优",
                            "fetched": True,
                        },
                        {
                            "title": "北京天气预报",
                            "source": "www.weather.com.cn",
                            "url": "https://www.weather.com.cn/weather/101010100.shtml",
                            "summary": "北京天气预报，及时准确发布中央气象台天气信息。",
                        },
                    ],
                },
            )

        registry.register(
            ToolSpec(
                name="web_search",
                description="fake web search",
                input_schema={},
                capabilities=["search.web"],
            ),
            fake_web_search,
        )
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(make_config(), manager, registry)

        result = agent.chat("今天北京的天气怎么样")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertIn("北京", result.content)
        self.assertIn("2026-06-", result.content)
        self.assertIn("小雨", result.content)
        self.assertIn("21~29℃", result.content)
        self.assertIn("空气质量优", result.content)
        self.assertNotIn("联网搜索找到 2 个结果", result.content)
        self.assertTrue(any(item["stage"] == "MainAgent.synthesize_result" for item in agent.last_state["debug_trace"]))
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "web_search")
        self.assertEqual(len(fake.seen), 0)

    def test_weather_without_location_asks_for_clarification(self):
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("今天天气怎么样")

        self.assertEqual(agent.last_state["route"], "clarify")
        self.assertEqual(agent.last_state["status"], "interrupted")
        self.assertIn("需要补充信息", result.content)
        self.assertEqual(agent.last_state["clarification_questions"][0].id, "location")
        self.assertEqual(agent.last_state["task_brief"].intent, "weather_lookup")
        self.assertEqual(len(fake.seen), 0)

    def test_dynamic_sports_search_uses_web_search_not_workspace(self):
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("搜索足球的相关知识，尤其是今年的比赛信息")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "search_agent")
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "web_search")
        self.assertIn("联网搜索未启用", result.content)
        self.assertEqual(len(fake.seen), 0)

    def test_code_task_routes_to_code_agent_without_host_fallback(self):
        config = make_config()
        config.code_execution = CodeExecutionConfig(enabled=False)
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(config, manager)

        result = agent.chat("用 Python 分析 data.csv")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "code_agent")
        self.assertEqual(agent.last_state["task_brief"].intent, "data_analysis")
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "docker_sandbox")
        self.assertIn("sandbox", result.content.lower())
        self.assertTrue(any(item["agent"] == "CodeAgent" for item in agent.last_state["debug_trace"]))
        self.assertEqual(len(fake.seen), 0)

    def test_excel_analysis_routes_to_code_agent(self):
        config = make_config()
        config.code_execution = CodeExecutionConfig(enabled=False)
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(config, manager)

        result = agent.chat("分析 02.xlsx 表格数据")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "code_agent")
        self.assertEqual(agent.last_state["task_brief"].intent, "data_analysis")
        self.assertEqual(agent.last_state["task_brief"].context["path"], "02.xlsx")
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "docker_sandbox")
        self.assertIn("sandbox", result.content.lower())
        self.assertEqual(len(fake.seen), 0)

    def test_excel_chart_routes_to_code_agent(self):
        config = make_config()
        config.code_execution = CodeExecutionConfig(enabled=False)
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(config, manager)

        result = agent.chat("生成 02.xlsx 的分析结果图")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "code_agent")
        self.assertEqual(agent.last_state["task_brief"].intent, "chart_generation")
        self.assertEqual(agent.last_state["task_brief"].context["path"], "02.xlsx")
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "docker_sandbox")
        self.assertIn("sandbox", result.content.lower())
        self.assertEqual(len(fake.seen), 0)

    def test_python_script_execution_routes_to_code_agent(self):
        config = make_config()
        config.code_execution = CodeExecutionConfig(enabled=False, allow_user_script_execution=True)
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(config, manager)

        result = agent.chat("运行这段 Python 代码\n```python\nprint('hello')\n```")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "code_agent")
        self.assertEqual(agent.last_state["task_brief"].intent, "script_execution")
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "docker_sandbox")
        self.assertIn("sandbox", result.content.lower())
        self.assertEqual(len(fake.seen), 0)

    def test_code_generation_routes_to_code_agent_without_sandbox(self):
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("帮我生成一个读取 csv 的脚本")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "code_agent")
        self.assertEqual(agent.last_state["task_brief"].intent, "code_generation")
        self.assertEqual(agent.last_state.get("tool_calls") or [], [])
        self.assertIn("```python", result.content)
        self.assertEqual(len(fake.seen), 0)

    def test_project_analysis_routes_to_code_agent(self):
        config = make_config()
        config.code_execution = CodeExecutionConfig(enabled=False)
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(config, manager)

        result = agent.chat("分析整个项目代码结构")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "code_agent")
        self.assertEqual(agent.last_state["task_brief"].intent, "project_analysis")
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "docker_sandbox")
        self.assertIn("sandbox", result.content.lower())
        self.assertEqual(len(fake.seen), 0)

    def test_excel_analysis_with_workspace_write_routes_to_plan_confirmation(self):
        manager, fake = make_manager(reply="not-json")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("帮我查看02.xlsx表格中的内容，并对他进行数据分析，将分析的结果图新建一个02_analys文件夹存放")

        self.assertEqual(agent.last_state["route"], "future_task")
        self.assertEqual(agent.last_state["status"], "interrupted")
        self.assertEqual(agent.last_state["interrupt"]["type"], "plan_confirmation")
        self.assertEqual(agent.last_state["task_brief"].delegate_to, "code_agent")
        self.assertEqual(agent.last_state["task_brief"].context["path"], "02.xlsx")
        self.assertIn("执行计划", result.content)
        self.assertEqual(agent.last_state.get("tool_calls") or [], [])
        self.assertEqual(len(fake.seen), 1)

    def test_file_execution_request_never_falls_through_to_simple_chat(self):
        manager, fake = make_manager(reply="我已经读取了 02.xlsx，并已保存图片。")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("查看 02.xlsx 表格内容并分析")

        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertNotEqual(agent.last_state["task_brief"].delegate_to, "simple_chat")
        self.assertNotIn("我已经读取了 02.xlsx", result.content)
        self.assertEqual(len(fake.seen), 0)

    def test_complex_research_routes_to_future_task_plan(self):
        manager, fake = make_manager(reply="must not be used")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("帮我调研 LangGraph 并整理对比")

        self.assertEqual(agent.last_state["route"], "future_task")
        self.assertEqual(agent.last_state["status"], "interrupted")
        self.assertEqual(agent.last_state["interrupt"]["type"], "plan_confirmation")
        self.assertIn("执行计划", result.content)
        self.assertEqual(len(fake.seen), 1)

    def test_search_after_chat_does_not_reuse_previous_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("important protein note", encoding="utf-8")
            manager, fake = make_manager(reply="上一轮聊天回答")
            agent = MainAgent(make_config(), manager, create_default_tool_registry(root))

            first = agent.chat("你好", thread_id="search-session")
            second = agent.chat("搜索 protein", thread_id="search-session")

        self.assertEqual(first.content, "上一轮聊天回答")
        self.assertEqual(agent.last_state["route"], "simple_task")
        self.assertIn("notes.md", second.content)
        self.assertNotEqual(second.content, first.content)
        self.assertEqual(agent.last_state["tool_calls"][0]["tool"], "workspace_search")
        self.assertEqual(len(fake.seen), 1)

    def test_high_risk_file_task_does_not_auto_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "notes.md"
            path.write_text("original", encoding="utf-8")
            manager, fake = make_manager(reply="not-json")
            agent = MainAgent(make_config(), manager, create_file_tool_registry(root))

            result = agent.chat("修改 notes.md")
            content = path.read_text(encoding="utf-8")

        self.assertEqual(agent.last_state["route"], "future_task")
        self.assertEqual(agent.last_state["status"], "interrupted")
        self.assertEqual(agent.last_state["interrupt"]["type"], "plan_confirmation")
        self.assertIn("执行计划", result.content)
        self.assertEqual(content, "original")
        self.assertEqual(len(fake.seen), 1)

    def test_clarification_resume_continues_to_plan_confirmation(self):
        manager, fake = make_manager(reply="not-json")
        agent = MainAgent(make_config(), manager)
        first = agent.chat("帮我处理这个文件", thread_id="s1")
        interrupt_id = agent.last_state["interrupt"]["id"]

        second = agent.resume(
            interrupt_id,
            {
                "type": "clarification",
                "answers": {
                    "target": "workspace/demo.md",
                    "action": "分析并总结",
                    "output": "直接回复结论",
                },
            },
            thread_id="s1",
        )

        self.assertIn("执行计划", second.content)
        self.assertEqual(agent.last_state["route"], "future_task")
        self.assertEqual(agent.last_state["interrupt"]["type"], "plan_confirmation")
        self.assertEqual(agent.last_state["plan_status"], "pending")
        self.assertEqual([item.type for item in agent.history.messages], ["human", "ai", "human", "ai"])
        self.assertEqual(len(fake.seen), 1)
        self.assertIn("需要补充信息", first.content)

    def test_plan_approval_and_wrong_interrupt_id(self):
        manager, fake = make_manager(reply="not-json")
        agent = MainAgent(make_config(), manager)
        agent.chat("帮我分析整个项目并生成报告", thread_id="s2")
        interrupt_id = agent.last_state["interrupt"]["id"]

        with self.assertRaises(AgentInterruptError):
            agent.resume("wrong", {"type": "plan_confirmation", "decision": "approve"}, thread_id="s2")

        result = agent.resume(
            interrupt_id,
            {"type": "plan_confirmation", "decision": "approve"},
            thread_id="s2",
        )

        self.assertIn("计划已确认", result.content)
        self.assertIn(agent.last_state["status"], {"completed", "interrupted"})
        if agent.last_state["status"] == "interrupted":
            self.assertEqual(agent.last_state["interrupt"]["type"], "workspace_confirmation")
        self.assertEqual(agent.last_state["plan_status"], "approved")
        self.assertIn(agent.last_state["task_status"], {"completed", "waiting_confirmation", "failed"})
        self.assertTrue(agent.last_state["task_steps"])
        if agent.last_state["status"] == "completed":
            self.assertIsNone(agent.pending_interrupt)
        self.assertEqual(len(fake.seen), 1)

    def test_workspace_write_waits_for_second_confirmation_after_plan_approval(self):
        plan_reply = json.dumps(
            {
                "summary": "分析 Excel 并准备发布结果。",
                "steps": [
                    "定位并检查02.xlsx文件是否存在及可读性。",
                    "在当前目录下新建名为02_analys的文件夹。",
                ],
                "risks": ["创建 workspace 目录需要二次确认。"],
                "requires_confirmation": True,
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "02.xlsx").write_bytes(b"fake")
            manager, fake = make_manager(reply=plan_reply)
            agent = MainAgent(make_config(), manager, create_file_tool_registry(root))

            agent.chat("帮我查看02.xlsx表格中的内容，并对他进行数据分析，将分析的结果图新建一个02_analys文件夹存放", thread_id="s-workspace")
            plan_interrupt_id = agent.last_state["interrupt"]["id"]

            waiting = agent.resume(
                plan_interrupt_id,
                {"type": "plan_confirmation", "decision": "approve"},
                thread_id="s-workspace",
            )

            self.assertEqual(agent.last_state["status"], "interrupted")
            self.assertEqual(agent.last_state["interrupt"]["type"], "workspace_confirmation")
            self.assertIn("workspace 写入", waiting.content)
            self.assertFalse((root / "02_analys").exists())
            workspace_interrupt_id = agent.last_state["interrupt"]["id"]

            done = agent.resume(
                workspace_interrupt_id,
                {"type": "workspace_confirmation", "decision": "approve"},
                thread_id="s-workspace",
            )

            self.assertEqual(agent.last_state["status"], "completed")
            self.assertTrue((root / "02_analys").is_dir())
            self.assertIn("已创建或确认目录存在：02_analys", done.content)
            self.assertIsNone(agent.pending_interrupt)
            self.assertEqual(len(fake.seen), 1)

    def test_plan_cancel_completes_without_execution(self):
        manager, fake = make_manager(reply="not-json")
        agent = MainAgent(make_config(), manager)
        agent.chat("帮我分析整个项目并生成报告", thread_id="s4")
        interrupt_id = agent.last_state["interrupt"]["id"]

        result = agent.resume(
            interrupt_id,
            {"type": "plan_confirmation", "decision": "cancel"},
            thread_id="s4",
        )

        self.assertIn("已取消", result.content)
        self.assertEqual(agent.last_state["status"], "completed")
        self.assertEqual(agent.last_state["plan_status"], "cancelled")
        self.assertIsNone(agent.pending_interrupt)
        self.assertEqual(len(fake.seen), 1)

    def test_plan_revision_generates_another_plan_interrupt(self):
        manager, fake = make_manager(reply="not-json")
        agent = MainAgent(make_config(), manager)
        agent.chat("帮我分析整个项目并生成报告", thread_id="s3")
        interrupt_id = agent.last_state["interrupt"]["id"]

        result = agent.resume(
            interrupt_id,
            {
                "type": "plan_confirmation",
                "decision": "revise",
                "feedback": "把测试验证提前。",
            },
            thread_id="s3",
        )

        self.assertIn("执行计划", result.content)
        self.assertEqual(agent.last_state["status"], "interrupted")
        self.assertEqual(agent.last_state["interrupt"]["type"], "plan_confirmation")
        self.assertEqual(agent.last_state["plan_status"], "pending")
        self.assertEqual(len(fake.seen), 2)

    def test_agent_keeps_bounded_history(self):
        manager, fake = make_manager()
        agent = MainAgent(make_config(), manager)
        for text in ("one", "two", "three"):
            agent.chat(text)
        self.assertEqual(len(agent.history.messages), 4)
        self.assertEqual(agent.history.messages[0].content, "two")
        self.assertEqual(fake.seen[1][0][1].content, "one")

    def test_reasoning_is_returned_but_not_saved_in_history(self):
        manager, _ = make_manager(reply="<think>内部分析</think>最终回答")
        agent = MainAgent(make_config(), manager)

        result = agent.chat("问题", options=GenerationOptions(thinking_enabled=True))

        self.assertEqual(result.reasoning, "内部分析")
        self.assertEqual(result.content, "最终回答")
        self.assertEqual(agent.history.messages[-1].content, "最终回答")

    def test_config_expands_env_and_resolves_builtin_path(self):
        content = """
default_model: {provider: api, model: demo}
providers:
  api: {enabled: true, api_key: '${TEST_AGENT_KEY}', model: demo, base_url: 'http://x/v1'}
  builtin: {enabled: true, model: '../model.gguf'}
  ollama: {enabled: false}
search:
  enabled: true
  provider: duckduckgo
  max_results: 7
  fetch_pages: 2
  timeout: 3
  user_agent: AgentDogsTest/0.1
code_execution:
  enabled: true
  backend: docker
  image: python:3.12-slim
  timeout_seconds: 9
  memory_limit: 256m
  cpu_limit: 0.5
  network_enabled: false
  workspace_readonly: true
  artifacts_dir: runtime/test-artifacts
  max_output_chars: 5000
  allow_user_script_execution: true
  max_artifacts: 9
  max_artifact_bytes: 123456
  dependency_install:
    enabled: true
    timeout_seconds: 33
    allowed_packages: [pandas, openpyxl]
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "llm.yaml"
            path.parent.mkdir()
            path.write_text(content, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_AGENT_KEY": "secret"}):
                config = load_config(path)
        self.assertEqual(config.providers["api"].api_key, "secret")
        self.assertEqual((config.default_provider, config.default_model), ("api", "demo"))
        self.assertTrue(Path(config.providers["builtin"].model).is_absolute())
        self.assertTrue(config.search.enabled)
        self.assertEqual(config.search.provider, "duckduckgo")
        self.assertEqual(config.search.max_results, 7)
        self.assertEqual(config.search.fetch_pages, 2)
        self.assertEqual(config.search.timeout, 3.0)
        self.assertEqual(config.search.user_agent, "AgentDogsTest/0.1")
        self.assertTrue(config.code_execution.enabled)
        self.assertEqual(config.code_execution.image, "python:3.12-slim")
        self.assertEqual(config.code_execution.timeout_seconds, 9)
        self.assertEqual(config.code_execution.memory_limit, "256m")
        self.assertEqual(config.code_execution.cpu_limit, 0.5)
        self.assertEqual(config.code_execution.artifacts_dir, "runtime/test-artifacts")
        self.assertEqual(config.code_execution.max_output_chars, 5000)
        self.assertTrue(config.code_execution.allow_user_script_execution)
        self.assertTrue(config.code_execution.dependency_install_enabled)
        self.assertEqual(config.code_execution.allowed_packages, ["pandas", "openpyxl"])
        self.assertEqual(config.code_execution.install_timeout_seconds, 33)
        self.assertEqual(config.code_execution.max_artifacts, 9)
        self.assertEqual(config.code_execution.max_artifact_bytes, 123456)


if __name__ == "__main__":
    unittest.main()
