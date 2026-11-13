"""Shared file reference serialization and metadata used by compatible endpoints."""

import unittest
from unittest.mock import patch

from agent_core import FilePart, LLMRequest, Message, TextPart, ToolDefinition
from agent_core.providers import OpenAIProvider


class ProviderMetadataTest(unittest.TestCase):
    def test_describes_attached_files_as_workspace_paths(self) -> None:
        from agent_core.providers.base import (
            _content_to_provider_format,
        )

        content = _content_to_provider_format(
            Message(
                role="user",
                content=(
                    TextPart(text="summarize this"),
                    FilePart(
                        path=".RADE/attachments/report.pdf",
                        filename="Q3 report.pdf",
                        mime_type="application/pdf",
                        size_bytes=12345,
                    ),
                ),
            )
        )

        self.assertEqual(
            content,
            "summarize this\n\n"
            "Attached files are available at these workspace-relative "
            "paths. Use the available tools to inspect or process them as appropriate:\n"
            '- filename="Q3 report.pdf", path=".RADE/attachments/report.pdf", '
            'mime_type="application/pdf", size_bytes=12345',
        )

    @patch(
        "litellm.token_counter",
        return_value=42,
    )
    def test_counts_tools_as_part_of_input(
        self,
        token_counter_mock,
    ) -> None:
        provider = OpenAIProvider(
            model="custom/model",
            max_context_tokens=1000,
        )
        tool = ToolDefinition(
            name="read_file",
            description="Read a workspace file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
            },
        )

        count = provider.count_input_tokens(
            LLMRequest(
                system_prompt="You are helpful.",
                messages=(Message(role="user", content="read it"),),
                tools=(tool,),
            )
        )

        self.assertEqual(count, 42)
        token_counter_mock.assert_called_once_with(
            model="custom/model",
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "read it"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a workspace file.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                            },
                        },
                    },
                }
            ],
        )

    @patch("litellm.get_model_info")
    def test_uses_litellm_context_limit_when_not_configured(
        self,
        get_model_info_mock,
    ) -> None:
        get_model_info_mock.return_value = {
            "max_input_tokens": 128000,
            "max_output_tokens": 8192,
            "supports_vision": True,
        }

        provider = OpenAIProvider(
            model="custom/model",
            base_url="https://example.com/v1",
        )

        self.assertEqual(provider.max_context_tokens, 128000)
        self.assertEqual(provider.max_output_tokens, 8192)
        self.assertEqual(provider._get_model_info(), {
            "max_input_tokens": 128000,
            "max_output_tokens": 8192,
        })
        get_model_info_mock.assert_called_once_with(
            model="custom/model",
            api_base="https://example.com/v1",
        )

    @patch("litellm.get_model_info")
    def test_configured_context_limit_skips_litellm_metadata(
        self,
        get_model_info_mock,
    ) -> None:
        provider = OpenAIProvider(
            model="custom/model",
            max_context_tokens=64000,
        )

        self.assertEqual(provider.max_context_tokens, 64000)
        get_model_info_mock.assert_not_called()

    def test_rejects_invalid_configured_context_limit(self) -> None:
        with self.assertRaises(ValueError):
            OpenAIProvider(
                model="custom/model",
                max_context_tokens=0,
            )

    @patch("litellm.get_model_info")
    def test_requires_config_for_model_without_context_metadata(
        self,
        get_model_info_mock,
    ) -> None:
        get_model_info_mock.return_value = {
            "max_input_tokens": None,
            "max_output_tokens": 8192,
        }

        with self.assertRaises(ValueError):
            OpenAIProvider(model="custom/model")

    @patch("litellm.get_model_info")
    def test_requires_config_when_model_metadata_lookup_fails(
        self,
        get_model_info_mock,
    ) -> None:
        get_model_info_mock.side_effect = RuntimeError("unknown model")

        with self.assertRaisesRegex(
            ValueError,
            "configure 'max_context_tokens'",
        ):
            OpenAIProvider(model="custom/model")

    @patch("litellm.get_model_info")
    def test_unknown_output_limit_is_none(
        self,
        get_model_info_mock,
    ) -> None:
        get_model_info_mock.return_value = {
            "max_input_tokens": 128000,
            "max_output_tokens": None,
            "max_tokens": 8192,
        }

        provider = OpenAIProvider(model="custom/model")

        self.assertIsNone(provider.max_output_tokens)
