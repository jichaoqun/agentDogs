from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agent.core.main_agent import MainAgent
from agent.core.utils.llm_config import AppConfig, ProviderConfig, load_config
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
        return AIMessage(content=self.reply)


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


if __name__ == "__main__":
    unittest.main()
