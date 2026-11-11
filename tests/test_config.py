import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from agent_core import DirectorySkillSource, SkillLoader, load_agent_config
from agent_runtime.config import (
    EXECUTION_CONFIG_FILENAMES,
    ModelConfig,
    configured_role_names,
    default_config_directory,
    initialize_config_directory,
    load_config,
    load_fast_config,
    load_model_options,
    load_prompt_templates,
    load_scratch_workspace_root,
    memory_store,
)
from agent_runtime.plugins import PluginManager


class ConfigTest(unittest.TestCase):
    def test_existing_agent_config_can_omit_session_title_generation_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_directory = Path(directory)
            initialize_config_directory(config_directory)
            path = config_directory / "agent_config.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            del document["generate_session_title"]
            path.write_text(json.dumps(document), encoding="utf-8")
            preserved = path.read_bytes()

            initialize_config_directory(config_directory)

            self.assertEqual(path.read_bytes(), preserved)
            self.assertTrue(load_agent_config(path).generate_session_title)

    def test_fast_model_selection_and_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            document = {
                "main_agent": {"provider": "first"},
                "fast_model": {"provider": ""},
                "providers": {
                    "first": {"model": "openai/first"},
                    "second": {"model": "openai/second"},
                },
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertIsNone(load_fast_config(path))
            document["fast_model"]["provider"] = None
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertIsNone(load_fast_config(path))
            document["fast_model"]["provider"] = "second"
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(load_fast_config(path).model, "openai/second")
            document["fast_model"]["provider"] = "missing"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not configured"):
                load_fast_config(path)

    def test_requires_main_agent_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "providers": {"first": {"model": "openai/first"}},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "main_agent"):
                load_config(path)

    def test_empty_subagent_provider_reuses_selected_main_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "first"},
                        "subagent": {"provider": ""},
                        "providers": {
                            "first": {"model": "openai/first"},
                            "second": {"model": "openai/second"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(load_config(path, provider="second").model, "openai/second")
            self.assertEqual(
                load_config(path, provider="second", role="researcher").model,
                "openai/second",
            )

    def test_lists_models_without_resolving_keys_and_loads_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            content = json.dumps({
                "main_agent": {"provider": "first"},
                "subagent": {"provider": ""},
                "providers": {
                    "first": {"model": "openai/first", "key": "${FIRST_KEY}"},
                    "second": {"model": "openai/second", "key": "${SECOND_KEY}"},
                },
            })
            path.write_text(content, encoding="utf-8")
            with patch.dict("os.environ", {"SECOND_KEY": "secret"}, clear=True):
                self.assertEqual(load_model_options(path), (
                    "first", {"first": "openai/first", "second": "openai/second"},
                ))
                selected = load_config(path, provider="second")
                self.assertEqual(selected.model, "openai/second")
                self.assertEqual(selected.key, "secret")
                with self.assertRaisesRegex(ValueError, "FIRST_KEY"):
                    load_config(path)
                with self.assertRaisesRegex(ValueError, "not configured"):
                    load_config(path, provider="unknown")
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_uses_project_root_for_default_directory(self) -> None:
        with patch("agent_runtime.config.Path.cwd", return_value=Path("/project")):

            self.assertEqual(
                default_config_directory(),
                Path("/project/.RADE"),
            )

    def test_memory_paths_are_derived_in_one_place(self) -> None:
        store = memory_store(Path("/config"))

        self.assertEqual(
            store.sessions_directory,
            Path("/config/sessions"),
        )

    def test_loads_and_expands_scratch_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps({"scratch_workspace_root": "~/scratch"}),
                encoding="utf-8",
            )

            self.assertEqual(
                load_scratch_workspace_root(path),
                Path("~/scratch").expanduser().resolve(),
            )

    def test_resolves_relative_scratch_workspace_root_from_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps({"scratch_workspace_root": "workspaces/scratch"}),
                encoding="utf-8",
            )

            self.assertEqual(
                load_scratch_workspace_root(path),
                path.parent.resolve() / "workspaces" / "scratch",
            )

    def test_missing_scratch_workspace_root_has_an_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "missing required config field 'scratch_workspace_root'.*add a path",
            ):
                load_scratch_workspace_root(path)

    def test_initializes_packaged_default_configs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_directory = Path(directory) / "RADE"

            created = initialize_config_directory(config_directory)

            self.assertEqual(
                created,
                (
                    config_directory / "provider_config.json",
                    config_directory / "agent_config.json",
                    config_directory / "AGENTS.md",
                    config_directory / "prompts" / "Soul.md",
                    config_directory / "prompts" / "SubAgent.md",
                    config_directory / "prompts" / "Consolidator.md",
                    config_directory / "prompts" / "WorkspaceMemory.md",
                    config_directory / "prompts" / "SessionTitle.md",
                    config_directory / "skills" / "plugin-creator",
                    config_directory / "skills" / "skill-creator",
                ),
            )
            provider_config = json.loads(created[0].read_text(encoding="utf-8"))
            agent_config = json.loads(created[1].read_text(encoding="utf-8"))
            self.assertEqual(
                provider_config["main_agent"]["provider"],
                "qwen",
            )
            self.assertEqual(provider_config["subagent"]["provider"], "")
            self.assertEqual(provider_config["fast_model"]["provider"], "")
            self.assertIn("openai", provider_config["providers"])
            self.assertEqual(
                provider_config["providers"]["qwen"],
                {
                    "model": "qwen3.8-27b",
                    "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "key": "${DASHSCOPE_API_KEY}",
                    "api": "chat_completions",
                    "max_context_tokens": 131072,
                },
            )
            self.assertEqual(agent_config["max_same_tool_calls"], 5)
            self.assertEqual(
                agent_config["provider"],
                {"request_timeout_seconds": 300, "max_retries": 2},
            )
            self.assertEqual(
                agent_config["scratch_workspace_root"],
                "workspaces/scratch",
            )
            self.assertTrue(agent_config["mcp"]["enabled"])
            self.assertEqual(
                agent_config["workspace_instruction_files"],
                ["CLAUDE.md", "AGENTS.md"],
            )
            self.assertEqual(
                agent_config["skills"],
                {
                    "global_paths": ["skills"],
                    "workspace_paths": [
                        "skills",
                        ".agents/skills",
                        ".RADE/skills",
                    ],
                },
            )
            self.assertEqual(
                (config_directory / "AGENTS.md").read_text(encoding="utf-8"),
                "",
            )
            self.assertFalse((config_directory / "MEMORY.md").exists())
            self.assertFalse((config_directory / "workspaces").exists())
            self.assertEqual(
                agent_config["context"]["compression"]["keep_recent_units"],
                8,
            )
            self.assertEqual(
                agent_config["context"]["compression"]["user_anchor_token_budget"],
                2000,
            )
            self.assertTrue((config_directory / "skills").is_dir())
            self.assertTrue((config_directory / "plugins").is_dir())
            prompts = load_prompt_templates(config_directory / "prompts")
            self.assertIn("I am RADE", prompts.system)
            self.assertIn("{{role}}", prompts.subagent)
            self.assertIn("compact replacement checkpoint", prompts.consolidator)
            self.assertIn("currently bound workspace", prompts.workspace_memory)
            skill_directory = config_directory / "skills" / "skill-creator"
            self.assertTrue((skill_directory / "SKILL.md").is_file())
            self.assertTrue((skill_directory / "license.txt").is_file())
            self.assertTrue((skill_directory / "scripts" / "init_skill.py").is_file())
            self.assertTrue((skill_directory / "scripts" / "quick_validate.py").is_file())
            self.assertEqual(
                SkillLoader().load(
                    (DirectorySkillSource(config_directory / "skills"),)
                ).names,
                ("plugin-creator", "skill-creator"),
            )
            plugins = PluginManager.discover(config_directory / "plugins")
            self.assertEqual(plugins.warnings, ())
            self.assertEqual(plugins.plugins, ())
            self.assertTrue(agent_config["main_agent"]["tools"]["read_file"])
            self.assertTrue(agent_config["main_agent"]["tools"]["job_control"])
            # Every shipped role carries an explicit enable switch.
            for name, role in agent_config["subagent_roles"].items():
                self.assertIsInstance(role["enabled"], bool, name)

    def test_initialization_does_not_overwrite_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_directory = Path(directory) / "RADE"
            config_directory.mkdir()
            provider_path = config_directory / "provider_config.json"
            provider_path.write_text("custom", encoding="utf-8")

            existing_skill = config_directory / "skills" / "skill-creator"
            existing_skill.mkdir(parents=True)
            skill_path = existing_skill / "SKILL.md"
            skill_path.write_text("custom skill", encoding="utf-8")
            existing_plugin = config_directory / "plugins" / "custom"
            existing_plugin.mkdir(parents=True)
            plugin_path = existing_plugin / "plugin.json"
            plugin_path.write_text("custom plugin", encoding="utf-8")
            prompts_directory = config_directory / "prompts"
            prompts_directory.mkdir()
            soul_path = prompts_directory / "Soul.md"
            soul_path.write_text("custom prompt", encoding="utf-8")
            instructions_path = config_directory / "AGENTS.md"
            instructions_path.write_text("custom instructions", encoding="utf-8")

            created = initialize_config_directory(config_directory)

            self.assertEqual(
                created,
                (
                    config_directory / "agent_config.json",
                    config_directory / "prompts" / "SubAgent.md",
                    config_directory / "prompts" / "Consolidator.md",
                    config_directory / "prompts" / "WorkspaceMemory.md",
                    config_directory / "prompts" / "SessionTitle.md",
                    config_directory / "skills" / "plugin-creator",
                ),
            )
            self.assertEqual(
                provider_path.read_text(encoding="utf-8"),
                "custom",
            )
            self.assertTrue((config_directory / "skills").is_dir())
            self.assertEqual(skill_path.read_text(encoding="utf-8"), "custom skill")
            self.assertEqual(
                plugin_path.read_text(encoding="utf-8"),
                "custom plugin",
            )
            self.assertEqual(soul_path.read_text(encoding="utf-8"), "custom prompt")
            self.assertEqual(
                instructions_path.read_text(encoding="utf-8"),
                "custom instructions",
            )
            self.assertFalse((existing_skill / "scripts").exists())

    def test_initializes_only_first_level_skill_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaults = root / "defaults"
            defaults.mkdir()
            for filename in EXECUTION_CONFIG_FILENAMES:
                (defaults / filename).write_text("{}", encoding="utf-8")
            prompts = defaults / "prompts_template"
            prompts.mkdir()
            for filename in (
                "Soul.md", "SubAgent.md", "Consolidator.md",
                "WorkspaceMemory.md", "SessionTitle.md",
            ):
                (prompts / filename).write_text(filename, encoding="utf-8")

            direct = defaults / "skills" / "direct"
            direct.mkdir(parents=True)
            (direct / "SKILL.md").write_text("direct", encoding="utf-8")
            nested = defaults / "skills" / "group" / "nested"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("nested", encoding="utf-8")
            without_entrypoint = defaults / "skills" / "not-a-skill"
            without_entrypoint.mkdir()
            (without_entrypoint / "resource.txt").write_text(
                "resource", encoding="utf-8"
            )

            config_directory = root / "RADE"
            with patch(
                "agent_runtime.config.files",
                return_value=defaults,
            ):
                initialize_config_directory(config_directory)

            self.assertTrue(
                (config_directory / "skills" / "direct" / "SKILL.md").is_file()
            )
            self.assertFalse((config_directory / "skills" / "group").exists())
            self.assertFalse(
                (config_directory / "skills" / "not-a-skill").exists()
            )

    def test_installs_a_skill_without_caches_or_finder_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaults = root / "defaults"
            defaults.mkdir()
            for filename in EXECUTION_CONFIG_FILENAMES:
                (defaults / filename).write_text("{}", encoding="utf-8")
            prompts = defaults / "prompts_template"
            prompts.mkdir()
            for filename in (
                "Soul.md", "SubAgent.md", "Consolidator.md",
                "WorkspaceMemory.md", "SessionTitle.md",
            ):
                (prompts / filename).write_text(filename, encoding="utf-8")

            packaged = defaults / "skills" / "demo"
            scripts = packaged / "scripts"
            cache = scripts / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "helper.cpython-312.pyc").write_bytes(b"cache")
            (scripts / "helper.py").write_text("print()", encoding="utf-8")
            (packaged / "SKILL.md").write_text("demo", encoding="utf-8")
            (packaged / ".DS_Store").write_bytes(b"finder")

            config_directory = root / "RADE"
            with patch(
                "agent_runtime.config.files",
                return_value=defaults,
            ):
                initialize_config_directory(config_directory)

            installed = config_directory / "skills" / "demo"
            self.assertTrue((installed / "scripts" / "helper.py").is_file())
            self.assertFalse((installed / "scripts" / "__pycache__").exists())
            self.assertFalse((installed / ".DS_Store").exists())

    def test_loads_selected_provider_from_json_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "deepseek"},
                        "providers": {
                            "openai": {
                                "model": "openai/test-model",
                                "key": "${OPENAI_KEY}",
                            },
                            "deepseek": {
                                "model": "deepseek/test-model",
                                "url": "https://example.com",
                                "key": "${DEEPSEEK_API_KEY}",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"DEEPSEEK_API_KEY": "secret"},
                clear=True,
            ):
                self.assertEqual(
                    load_config(path),
                    ModelConfig(
                        model="deepseek/test-model",
                        url="https://example.com",
                        key="secret",
                        max_context_tokens=None,
                    ),
                )

    def test_loads_reasoning_policy_from_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "gateway"},
                        "providers": {
                            "gateway": {
                                "model": "gpt-6-astra",
                                "reasoning_effort": " MINIMAL ",
                                "reasoning_parameters": {
                                    "extra_body": {"enable_thinking": True},
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_config(path),
                ModelConfig(
                    model="gpt-6-astra",
                    url=None,
                    key=None,
                    max_context_tokens=None,
                    reasoning_effort="minimal",
                    reasoning_parameters={
                        "extra_body": {"enable_thinking": True},
                    },
                ),
            )

    def test_rejects_invalid_provider_reasoning_policy(self) -> None:
        for field, value in (
            ("reasoning_effort", "balanced"),
            ("reasoning_parameters", []),
            ("reasoning_parameters", {"typo": True}),
            ("reasoning_parameters", {"thinking": True}),
            ("api", "automatic"),
            ("api", []),
        ):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(
                    json.dumps(
                        {
                            "main_agent": {"provider": "test"},
                            "providers": {
                                "test": {
                                    "model": "openai/test-model",
                                    field: value,
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                with self.subTest(field=field), self.assertRaises(ValueError):
                    load_config(path)

    def test_loads_explicit_relay_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "main_agent": {"provider": "relay"},
                "providers": {"relay": {"model": "custom-model", "api": "chat_completions"}},
            }), encoding="utf-8")
            self.assertEqual(load_config(path).api, "chat_completions")

    def test_strips_whitespace_from_provider_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": " test "},
                        "providers": {
                            "test": {
                                "model": " openai/test-model ",
                                "url": " https://example.com/v1 ",
                                "key": " secret ",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_config(path),
                ModelConfig(
                    model="openai/test-model",
                    url="https://example.com/v1",
                    key="secret",
                    max_context_tokens=None,
                ),
            )

    def test_allows_inline_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "local"},
                        "providers": {
                            "local": {
                                "model": "openai/local-model",
                                "url": "http://localhost:8000/v1",
                                "key": "local-key",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_config(path),
                ModelConfig(
                    model="openai/local-model",
                    url="http://localhost:8000/v1",
                    key="local-key",
                    max_context_tokens=None,
                ),
            )

    def test_loads_optional_provider_context_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "test"},
                        "providers": {
                            "test": {
                                "model": "openai/test-model",
                                "max_context_tokens": 128000,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_config(path),
                ModelConfig(
                    model="openai/test-model",
                    url=None,
                    key=None,
                    max_context_tokens=128000,
                ),
            )

    def test_rejects_boolean_provider_context_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "test"},
                        "providers": {
                            "test": {
                                "model": "openai/test-model",
                                "max_context_tokens": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(path)

    def test_rejects_unknown_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "missing"},
                        "providers": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(path)

    def test_rejects_missing_key_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "test"},
                        "providers": {
                            "test": {
                                "model": "test/model",
                                "key": "${TEST_KEY}",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    load_config(path)


class ProviderResolutionTest(unittest.TestCase):
    """Provider resolution follows role → subagent → main."""

    def write(self, directory: str, **blocks: object) -> Path:
        path = Path(directory) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "main_agent": {"provider": "main"},
                    "providers": {
                        "main": {"model": "openai/main"},
                        "shared": {"model": "openai/shared"},
                        "special": {"model": "openai/special"},
                        **blocks.pop("providers", {}),
                    },
                    **blocks,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_role_override_wins_over_subagent_and_main(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                subagent={"provider": "shared"},
                subagent_roles={"coder": {"provider": "special"}},
            )

            self.assertEqual(
                load_config(path, role="coder").model, "openai/special"
            )
            self.assertEqual(
                load_config(path, role="researcher").model, "openai/shared"
            )
            self.assertEqual(load_config(path).model, "openai/main")

    def test_empty_role_override_falls_through_the_chain(self) -> None:
        """"" means inherit, so the chain keeps walking past it."""
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                subagent={"provider": "shared"},
                subagent_roles={"coder": {"provider": ""}},
            )

            self.assertEqual(
                load_config(path, role="coder").model, "openai/shared"
            )

    def test_rejects_a_blank_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                subagent_roles={"coder": {"provider": "   "}},
            )

            with self.assertRaisesRegex(
                ValueError, "subagent_roles.coder.provider"
            ):
                load_config(path, role="coder")

    def test_rejects_a_non_object_role_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, subagent_roles={"coder": "invalid"})

            with self.assertRaisesRegex(
                ValueError, "subagent_roles.coder.*object"
            ):
                load_config(path)

    def test_reports_the_roles_that_override_a_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                subagent_roles={
                    "coder": {"provider": "special"},
                    "writer": {"provider": "shared"},
                },
            )

            self.assertEqual(
                configured_role_names(path), frozenset({"coder", "writer"})
            )

    def test_reports_no_roles_when_the_block_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                configured_role_names(self.write(directory)), frozenset()
            )


if __name__ == "__main__":
    unittest.main()
