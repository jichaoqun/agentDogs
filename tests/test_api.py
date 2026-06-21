from __future__ import annotations

import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import AIMessage

from agent.api.app import create_app
from agent.api.sessions import SessionManager
from agent.core.utils.llm_config import AppConfig, ProviderConfig
from agent.core.utils.llm_models import ModelResponse


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


if __name__ == "__main__":
    unittest.main()
