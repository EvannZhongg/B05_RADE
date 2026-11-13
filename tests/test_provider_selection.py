import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import AgentConfig, ToolConfig, Workspace
from agent_core.providers import OpenAIProvider
from agent_runtime.config import ModelConfig, initialize_config_directory, load_config, load_model_options
from agent_runtime.plane_manager import ExecutionPlaneManager


class ProviderSelectionTest(unittest.TestCase):
    def select(self, model, url, api=None):
        manager = ExecutionPlaneManager.__new__(ExecutionPlaneManager)
        return manager._provider_for(
            ModelConfig(model, url, "test-key", 100000, reasoning_effort="high", api=api),
            Workspace(Path(__file__).parent), AgentConfig(
                max_same_tool_calls=5, output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
            ),
        )

    def test_openai_model_uses_responses_independent_of_hostname(self):
        for url in (None, "https://relay-one.example/v1", "http://localhost:8888/v1"):
            for model in ("gpt-6-astra", "openai/gpt-6-astra"):
                with self.subTest(url=url, model=model):
                    provider = self.select(model, url)
                    self.assertIsInstance(provider, OpenAIProvider)
                    self.assertEqual(provider._api, "responses")
                    self.assertEqual(provider._wire_model, "gpt-6-astra")
                    self.assertEqual(provider._base_url, url)

    def test_direct_provider_owns_model_id_normalization(self):
        for model, wire_model, api in (
            ("openai/gpt-5", "gpt-5", None),
            ("openai/gpt-5", "openai/gpt-5", "chat_completions"),
            ("deepseek/deepseek-flash", "deepseek-flash", None),
            ("deepseek/deepseek-flash", "deepseek-flash", "responses"),
        ):
            with self.subTest(model=model, api=api):
                provider = OpenAIProvider(model, api=api)
                self.assertEqual(provider._wire_model, wire_model)

    def test_explicit_protocol_supports_arbitrary_model_ids_without_inference(self):
        with patch("litellm.get_llm_provider") as infer:
            for api in ("responses", "chat_completions"):
                for model in ("custom-alias", "gateway/custom", "openai/gpt-6-astra"):
                    provider = self.select(model, "https://relay.example/v1", api)
                    self.assertIsInstance(provider, OpenAIProvider)
                    self.assertEqual(provider._api, api)
                    self.assertEqual(provider._wire_model, model)
            infer.assert_not_called()

    def test_unprefixed_model_does_not_require_a_catalog_entry_to_select_protocol(self):
        with patch("agent_core.providers.openai_provider.model_info", side_effect=ValueError("unknown model")):
            provider = self.select("custom-alias", "https://relay.example/v1")
        self.assertIsInstance(provider, OpenAIProvider)
        self.assertEqual(provider._api, "responses")
        self.assertEqual(provider._wire_model, "custom-alias")

    def test_namespaced_model_without_api_uses_native_responses(self):
        provider = self.select("gateway/custom", "https://relay.example/v1")
        self.assertIsInstance(provider, OpenAIProvider)
        self.assertEqual(provider._api, "responses")
        self.assertEqual(provider._wire_model, "gateway/custom")

    @patch.dict("os.environ", {
        "OPENAI_KEY": "test-key",
        "DEEPSEEK_API_KEY": "test-key",
        "DASHSCOPE_API_KEY": "test-key",
        "OPENROUTER_KEY": "test-key",
    })
    def test_default_providers_use_supported_protocols_and_wire_model_ids(self):
        expected = {
            "openai": ("responses", "gpt-5"),
            "deepseek": ("chat_completions", "deepseek-flash"),
            "qwen": ("chat_completions", "qwen3.8-27b"),
            "openrouter": ("chat_completions", "openai/gpt-5"),
            "ollama": ("chat_completions", "llama3.1"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_config_directory(root)
            path = root / "provider_config.json"
            self.assertEqual(set(load_model_options(path)[1]), set(expected))
            for name, (api, wire_model) in expected.items():
                with self.subTest(provider=name):
                    config = load_config(path, name)
                    provider = OpenAIProvider(
                        config.model, base_url=config.url, max_context_tokens=config.max_context_tokens,
                        api=config.api,
                    )
                    self.assertEqual(provider._api, api)
                    self.assertEqual(provider._wire_model, wire_model)

    def test_deepseek_uses_native_chat_and_wire_model(self):
        for api in (None, "chat_completions"):
            provider = self.select("deepseek/deepseek-flash", "https://api.deepseek.com", api)
            self.assertIsInstance(provider, OpenAIProvider)
            self.assertEqual(provider._api, "chat_completions")
            self.assertEqual(provider._wire_model, "deepseek-flash")
