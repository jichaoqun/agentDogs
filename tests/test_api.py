from __future__ import annotations

import tempfile
import unittest
import zipfile
import importlib
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import AIMessage, HumanMessage

api_app = importlib.import_module("agent.api.app")
from agent.api.app import create_app
from agent.api.sessions import SessionManager
from agent.core.state import ClarificationQuestion, TaskAnalysis
from agent.core.utils.llm_config import AppConfig, CodeExecutionConfig, ProviderConfig
from agent.core.utils.llm_models import ModelInvocationError, ModelResponse
from agent.core.utils.time_utils import isoformat


class FakeAgent:
    def __init__(self, config):
        self.history = InMemoryChatMessageHistory()

    def chat(self, text, *, selection=None, options=None, thread_id=None, is_cancelled=None):
        self.history.add_message(HumanMessage(content=text, additional_kwargs={"created_at": isoformat()}))
        message = AIMessage(content=f"收到：{text}", additional_kwargs={"created_at": isoformat()})
        self.history.add_message(message)
        reasoning = "测试思考" if options and options.thinking_enabled else None
        return ModelResponse(
            message.content,
            message,
            selection.provider,
            selection.model,
            reasoning=reasoning,
        )


class ClarifyFakeAgent(FakeAgent):
    def chat(self, text, *, selection=None, options=None, thread_id=None, is_cancelled=None):
        self.history.add_message(HumanMessage(content=text, additional_kwargs={"created_at": isoformat()}))
        clarification = {
            "original_message": text,
            "questions": [
                {
                    "id": "target",
                    "question": "目标是什么？",
                    "options": ["目标 A", "目标 B"],
                    "allow_custom": True,
                    "required": True,
                }
            ],
        }
        interrupt = {
            "id": "interrupt-1",
            "type": "clarification",
            "message": "这个任务还需要补充信息",
            "clarification": clarification,
            "plan": None,
        }
        message = AIMessage(
            content="这个任务还需要补充信息",
            additional_kwargs={
                "route": "clarify",
                "complexity": "needs_info",
                "status": "interrupted",
                "created_at": isoformat(),
                "clarification": clarification,
                "interrupt": interrupt,
            },
        )
        self.history.add_message(message)
        question = ClarificationQuestion(
            id="target",
            question="目标是什么？",
            options=["目标 A", "目标 B"],
        )
        self.last_state = {
            "user_input": text,
            "route": "clarify",
            "task_analysis": TaskAnalysis(
                intent=text,
                complexity="needs_info",
                missing_info=["目标是什么？"],
                clarification_questions=[question],
            ),
            "clarification_questions": [question],
            "plan_steps": [],
            "status": "interrupted",
            "interrupt": interrupt,
        }
        return ModelResponse(
            message.content,
            message,
            selection.provider,
            selection.model,
        )


class ResumeFakeAgent(ClarifyFakeAgent):
    def __init__(self, config):
        super().__init__(config)
        self.pending = False

    def has_pending_interrupt(self):
        return self.pending

    def chat(self, text, *, selection=None, options=None, thread_id=None, is_cancelled=None):
        self.pending = True
        return super().chat(text, selection=selection, options=options, thread_id=thread_id, is_cancelled=is_cancelled)

    def resume(self, interrupt_id, payload, *, thread_id=None, is_cancelled=None):
        if interrupt_id != "interrupt-1":
            from agent.core.main_agent import AgentInterruptError

            raise AgentInterruptError("bad interrupt")
        self.pending = False
        self.history.add_message(HumanMessage(content="补充信息", additional_kwargs={"created_at": isoformat()}))
        plan = {
            "summary": "处理目标 A",
            "steps": ["确认目标", "执行分析"],
            "risks": ["第一阶段不执行"],
            "requires_confirmation": True,
        }
        interrupt = {
            "id": "interrupt-2",
            "type": "plan_confirmation",
            "message": "我已经整理出执行计划，请确认后再继续。",
            "clarification": None,
            "plan": plan,
        }
        message = AIMessage(
            content="我已经整理出执行计划，请确认后再继续。",
            additional_kwargs={
                "route": "future_task",
                "complexity": "needs_info",
                "status": "interrupted",
                "created_at": isoformat(),
                "plan_steps": plan["steps"],
                "plan_status": "pending",
                "interrupt": interrupt,
            },
        )
        self.history.add_message(message)
        self.last_state = {
            "route": "future_task",
            "task_analysis": TaskAnalysis(intent="处理", complexity="needs_info"),
            "status": "interrupted",
            "plan_steps": plan["steps"],
            "plan_status": "pending",
            "interrupt": interrupt,
        }
        return ModelResponse(message.content, message, "builtin", "builtin")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig(
            "system",
            10,
            {name: ProviderConfig(enabled=name == "builtin") for name in ("api", "ollama", "builtin")},
            Path("test.yaml"),
            default_provider="builtin",
            default_model="builtin",
        )
        manager = SessionManager(self.config, agent_factory=FakeAgent)
        self.client = TestClient(create_app(self.config, manager))

    def test_session_chat_flow(self):
        created = self.client.post("/api/v1/sessions", json={}).json()
        response = self.client.post(
            f"/api/v1/sessions/{created['id']}/messages", json={"message": "你好"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"]["content"], "收到：你好")
        self.assertEqual(response.json()["provider"], "builtin")
        self.assertEqual(response.json()["model"], "builtin")
        self.assertIsNotNone(response.json()["message"]["created_at"])
        detail = self.client.get(f"/api/v1/sessions/{created['id']}").json()
        self.assertEqual([item["role"] for item in detail["messages"]], ["user", "assistant"])
        self.assertTrue(all(item["created_at"] for item in detail["messages"]))
        self.assertEqual(detail["title"], "你好")

    def test_cancel_endpoint_marks_active_session_run(self):
        created = self.client.post("/api/v1/sessions", json={}).json()
        session = self.client.app.state.sessions.get(created["id"])
        run_id = self.client.app.state.sessions.begin_run(session)

        response = self.client.post(f"/api/v1/sessions/{created['id']}/cancel")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["cancelled"])
        self.assertEqual(payload["status"], "cancelling")
        self.assertTrue(self.client.app.state.sessions.is_run_cancelled(created["id"], run_id))
        next_run_id = self.client.app.state.sessions.begin_run(session)
        self.assertNotEqual(next_run_id, run_id)
        self.assertFalse(self.client.app.state.sessions.is_run_cancelled(created["id"], next_run_id))
        self.assertFalse(self.client.app.state.sessions.finish_run(session, next_run_id))
        self.assertTrue(self.client.app.state.sessions.finish_run(session, run_id))

    def test_missing_session_is_404(self):
        response = self.client.post("/api/v1/sessions/missing/messages", json={"message": "test"})
        self.assertEqual(response.status_code, 404)

    def test_model_selection_and_options_are_forwarded(self):
        self.config.providers["ollama"].enabled = True
        created = self.client.post("/api/v1/sessions", json={}).json()
        response = self.client.post(
            f"/api/v1/sessions/{created['id']}/messages",
            json={
                "message": "分析一下",
                "provider": "ollama",
                "model": "qwen3:8b",
                "temperature": 0.4,
                "max_tokens": 500,
                "thinking_enabled": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["provider"], response.json()["model"]), ("ollama", "qwen3:8b"))
        self.assertTrue(response.json()["thinking_enabled"])
        self.assertEqual(response.json()["reasoning"], "测试思考")

    def test_clarify_response_includes_structured_questions(self):
        manager = SessionManager(self.config, agent_factory=ClarifyFakeAgent)
        client = TestClient(create_app(self.config, manager))
        created = client.post("/api/v1/sessions", json={}).json()

        response = client.post(
            f"/api/v1/sessions/{created['id']}/messages",
            json={"message": "帮我处理这个文件"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["route"], "clarify")
        self.assertEqual(payload["complexity"], "needs_info")
        self.assertEqual(payload["clarification"]["original_message"], "帮我处理这个文件")
        self.assertEqual(payload["clarification"]["questions"][0]["question"], "目标是什么？")
        self.assertTrue(payload["clarification"]["questions"][0]["allow_custom"])
        self.assertEqual(payload["message"]["route"], "clarify")
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["interrupt"]["type"], "clarification")
        self.assertEqual(payload["interrupt"]["id"], "interrupt-1")

    def test_resume_endpoint_continues_pending_interrupt(self):
        manager = SessionManager(self.config, agent_factory=ResumeFakeAgent)
        client = TestClient(create_app(self.config, manager))
        created = client.post("/api/v1/sessions", json={}).json()
        first = client.post(
            f"/api/v1/sessions/{created['id']}/messages",
            json={"message": "帮我处理这个文件"},
        )
        self.assertEqual(first.status_code, 200)

        response = client.post(
            f"/api/v1/sessions/{created['id']}/resume",
            json={
                "interrupt_id": "interrupt-1",
                "type": "clarification",
                "answers": {"target": "目标 A"},
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["route"], "future_task")
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["interrupt"]["type"], "plan_confirmation")
        self.assertEqual(payload["plan_status"], "pending")

    def test_models_list_uses_configured_api_models_and_builtin_label(self):
        config = AppConfig(
            "system",
            10,
            {
                "api": ProviderConfig(
                    enabled=True,
                    model="gpt-4.1-mini",
                    extra={"models": ["gpt-4.1-mini", "gpt-4.1"]},
                ),
                "ollama": ProviderConfig(enabled=False),
                "builtin": ProviderConfig(enabled=True, model="model.gguf"),
            },
            Path("test.yaml"),
            default_provider="builtin",
            default_model="builtin",
        )
        client = TestClient(create_app(config, SessionManager(config, agent_factory=FakeAgent)))

        response = client.get("/api/v1/models")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(
            {
                "provider": "api",
                "model": "gpt-4.1-mini",
                "display_name": "gpt-4.1-mini",
                "supports_thinking": False,
            },
            payload,
        )
        self.assertIn(
            {
                "provider": "api",
                "model": "gpt-4.1",
                "display_name": "gpt-4.1",
                "supports_thinking": False,
            },
            payload,
        )
        self.assertIn(
            {
                "provider": "builtin",
                "model": "builtin",
                "display_name": "内置模型",
                "supports_thinking": True,
            },
            payload,
        )

    def test_models_list_ignores_failed_ollama_scan(self):
        config = AppConfig(
            "system",
            10,
            {
                "api": ProviderConfig(enabled=True, model="gpt-4.1-mini"),
                "ollama": ProviderConfig(enabled=True),
                "builtin": ProviderConfig(enabled=True, model="model.gguf"),
            },
            Path("test.yaml"),
            default_provider="builtin",
            default_model="builtin",
        )
        manager = SessionManager(config, agent_factory=FakeAgent)

        class BrokenOllama:
            def list_models(self):
                raise ModelInvocationError("offline")

        manager.models.providers["ollama"] = BrokenOllama()
        client = TestClient(create_app(config, manager))

        response = client.get("/api/v1/models")

        self.assertEqual(response.status_code, 200)
        providers = {item["provider"] for item in response.json()}
        self.assertIn("api", providers)
        self.assertIn("builtin", providers)
        self.assertNotIn("ollama", providers)

    def test_tools_and_agents_debug_endpoints(self):
        tools = self.client.get("/api/v1/tools")
        self.assertEqual(tools.status_code, 200)
        tool_payload = tools.json()
        tool_names = {item["name"] for item in tool_payload}
        self.assertIn("read_file", tool_names)
        self.assertIn("write_file", tool_names)
        self.assertIn("workspace_search", tool_names)
        self.assertIn("web_search", tool_names)
        write_tool = next(item for item in tool_payload if item["name"] == "write_file")
        self.assertEqual(write_tool["risk_level"], "high")

        agents = self.client.get("/api/v1/agents")
        self.assertEqual(agents.status_code, 200)
        agent_names = {item["name"] for item in agents.json()}
        self.assertIn("simple_task", agent_names)
        self.assertIn("file_agent", agent_names)
        self.assertIn("search_agent", agent_names)
        self.assertIn("code_agent", agent_names)
        self.assertIn("task_agent", agent_names)


class FileApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_workspace = api_app.WORKSPACE_ROOT
        api_app.WORKSPACE_ROOT = Path(self.tmp.name)
        self.config = AppConfig(
            "system",
            10,
            {name: ProviderConfig(enabled=name == "builtin") for name in ("api", "ollama", "builtin")},
            Path("test.yaml"),
            default_provider="builtin",
            default_model="builtin",
        )
        manager = SessionManager(self.config, agent_factory=FakeAgent)
        self.client = TestClient(api_app.create_app(self.config, manager))

    def tearDown(self):
        api_app.WORKSPACE_ROOT = self.previous_workspace
        self.tmp.cleanup()

    def test_tree_excludes_trash(self):
        (api_app.WORKSPACE_ROOT / "notes.md").write_text("hello", encoding="utf-8")
        trash = api_app.WORKSPACE_ROOT / ".trash"
        trash.mkdir()
        (trash / "old.md").write_text("old", encoding="utf-8")

        response = self.client.get("/api/v1/files/tree")

        self.assertEqual(response.status_code, 200)
        names = {item["name"] for item in response.json()["children"]}
        self.assertIn("notes.md", names)
        self.assertNotIn(".trash", names)

    def test_file_create_read_save_rename_and_delete_flow(self):
        response = self.client.post("/api/v1/files", json={"path": "", "name": "demo.md", "type": "file"})
        self.assertEqual(response.status_code, 201)

        response = self.client.put("/api/v1/files/content", json={"path": "demo.md", "content": "# Demo"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "# Demo")

        response = self.client.get("/api/v1/files/content", params={"path": "demo.md"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["editable"])

        response = self.client.patch("/api/v1/files", json={"path": "demo.md", "name": "renamed.md"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], "renamed.md")

        response = self.client.delete("/api/v1/files", params={"path": "renamed.md"})
        self.assertEqual(response.status_code, 204)
        self.assertFalse((api_app.WORKSPACE_ROOT / "renamed.md").exists())
        self.assertTrue((api_app.WORKSPACE_ROOT / ".trash" / "renamed.md").exists())

    def test_rejects_path_traversal_and_name_conflicts(self):
        (api_app.WORKSPACE_ROOT / "same.md").write_text("one", encoding="utf-8")

        response = self.client.get("/api/v1/files/content", params={"path": "../README.md"})
        self.assertEqual(response.status_code, 400)

        response = self.client.post("/api/v1/files", json={"path": "", "name": "same.md", "type": "file"})
        self.assertEqual(response.status_code, 409)

    def test_upload_raw_and_docx_text_preview(self):
        response = self.client.post(
            "/api/v1/files/upload",
            files={"file": ("upload.txt", b"uploaded", "text/plain")},
        )
        if api_app.MULTIPART_AVAILABLE:
            self.assertEqual(response.status_code, 201)
            self.assertEqual((api_app.WORKSPACE_ROOT / "upload.txt").read_text(encoding="utf-8"), "uploaded")
        else:
            self.assertEqual(response.status_code, 503)
        self.assertIn("python-multipart", response.text)

    def test_artifact_download_is_scoped_to_artifacts_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "artifacts"
            run_dir = artifacts / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "chart.png").write_bytes(b"png")
            config = AppConfig(
                "system",
                10,
                {name: ProviderConfig(enabled=name == "builtin") for name in ("api", "ollama", "builtin")},
                Path("test.yaml"),
                default_provider="builtin",
                default_model="builtin",
                code_execution=CodeExecutionConfig(artifacts_dir=str(artifacts)),
            )
            client = TestClient(api_app.create_app(config, SessionManager(config, agent_factory=FakeAgent)))

            response = client.get("/api/v1/artifacts/run-1/chart.png")
            escaped = client.get("/api/v1/artifacts/run-1/%2e%2e")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"png")
        self.assertEqual(escaped.status_code, 400)

        (api_app.WORKSPACE_ROOT / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        response = self.client.get("/api/v1/files/raw", params={"path": "image.png"})
        self.assertEqual(response.status_code, 200)

        docx_path = api_app.WORKSPACE_ROOT / "sample.docx"
        with zipfile.ZipFile(docx_path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p></w:body></w:document>"
                ),
            )
        response = self.client.get("/api/v1/files/content", params={"path": "sample.docx"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["editable"])
        self.assertEqual(response.json()["content"], "Hello DOCX")


if __name__ == "__main__":
    unittest.main()
