from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import AIMessage

import agent.api.app as api_app
from agent.api.app import create_app
from agent.api.sessions import SessionManager
from agent.core.utils.llm_config import AppConfig, ProviderConfig
from agent.core.utils.llm_models import ModelInvocationError, ModelResponse


class FakeAgent:
    def __init__(self, config):
        self.history = InMemoryChatMessageHistory()

    def chat(self, text, *, selection=None, options=None):
        self.history.add_user_message(text)
        message = AIMessage(content=f"收到：{text}")
        self.history.add_message(message)
        reasoning = "测试思考" if options and options.thinking_enabled else None
        return ModelResponse(
            message.content,
            message,
            selection.provider,
            selection.model,
            reasoning=reasoning,
        )


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
        detail = self.client.get(f"/api/v1/sessions/{created['id']}").json()
        self.assertEqual([item["role"] for item in detail["messages"]], ["user", "assistant"])
        self.assertEqual(detail["title"], "你好")

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
        self.assertEqual(response.status_code, 201)
        self.assertEqual((api_app.WORKSPACE_ROOT / "upload.txt").read_text(encoding="utf-8"), "uploaded")

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
