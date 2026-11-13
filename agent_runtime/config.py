import json
import os
import shutil
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from agent_core import DirectorySkillSource, SkillLoader, SkillRegistry, SkillSource
from agent_core.memory import MemoryStore
from agent_core.path_utils import is_path_within
from agent_core.providers import (
    OPENAI_APIS,
    normalize_semantic_effort,
    validate_reasoning_parameters,
)

if TYPE_CHECKING:
    from .plugins import PluginManager

EXECUTION_CONFIG_FILENAMES = ("provider_config.json", "agent_config.json")
PROMPT_FILENAMES = (
    "Soul.md",
    "SubAgent.md",
    "Consolidator.md",
    "WorkspaceMemory.md",
)


@dataclass(frozen=True)
class ModelConfig:
    model: str
    url: str | None
    key: str | None
    max_context_tokens: int | None = None
    reasoning_effort: str | None = None
    reasoning_parameters: dict[str, object] = field(default_factory=dict)
    api: str | None = None


@dataclass(frozen=True)
class PromptTemplates:
    system: str
    subagent: str
    consolidator: str
    workspace_memory: str


def default_config_directory() -> Path:
    return Path.cwd() / ".RADE"


def load_scratch_workspace_root(path: Path) -> Path:
    with path.open(encoding="utf-8") as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError(f"configuration must be a JSON object: {path}")
    value = document.get("scratch_workspace_root")
    if value is None:
        raise ValueError(
            "missing required config field 'scratch_workspace_root' in "
            f"{path}; add a path such as 'workspaces/scratch'"
        )
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "config field 'scratch_workspace_root' must be a non-empty "
            f"path in {path}"
        )
    root = Path(value.strip()).expanduser()
    return (path.parent / root).resolve()


def memory_store(directory: Path) -> MemoryStore:
    return MemoryStore(directory / "sessions")


def load_runtime_skills(directory: Path, plugins: "PluginManager") -> SkillRegistry:
    skill_directory = directory / "skills"
    plugin_directory = directory / "plugins"
    sources: list[SkillSource] = []
    if is_path_within(skill_directory, directory):
        sources.append(DirectorySkillSource(skill_directory))
    if is_path_within(plugin_directory, directory):
        sources.extend(
            source for source in plugins.skill_sources()
            if is_path_within(source.plugin.root, plugin_directory)
        )
    return SkillLoader().load(sources)


def initialize_config_directory(directory: Path) -> tuple[Path, ...]:
    created = []
    directory.mkdir(parents=True, exist_ok=True)
    defaults = files("agent_runtime.defaults")
    for filename in EXECUTION_CONFIG_FILENAMES:
        path = directory / filename
        if path.exists():
            continue
        path.write_text(
            defaults.joinpath(filename).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        created.append(path)

    instructions_path = directory / "AGENTS.md"
    if not instructions_path.exists():
        instructions_path.touch()
        created.append(instructions_path)

    prompts_directory = directory / "prompts"
    prompts_directory.mkdir(exist_ok=True)
    packaged_prompts = defaults.joinpath("prompts_template")
    for filename in PROMPT_FILENAMES:
        path = prompts_directory / filename
        if path.exists():
            continue
        _copy_resource_file(packaged_prompts.joinpath(filename), path)
        created.append(path)

    skills_directory = directory / "skills"
    skills_directory.mkdir(exist_ok=True)
    (directory / "plugins").mkdir(exist_ok=True)
    packaged_skills = defaults.joinpath("skills")
    for child in sorted(packaged_skills.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or not child.joinpath("SKILL.md").is_file():
            continue
        target = skills_directory / child.name
        if target.exists():
            continue
        _copy_resource_directory(child, target)
        created.append(target)

    return tuple(created)


# Packaged directories are copied straight from the tree that holds them, which
# is the working tree for an editable install, so byte-code caches and Finder
# metadata can sit beside the real files. Neither belongs in installed defaults.
_SKIPPED_RESOURCE_NAMES = frozenset({"__pycache__", ".DS_Store"})


def _copy_resource_directory(source, destination: Path) -> None:
    destination.mkdir()
    for child in source.iterdir():
        if child.name in _SKIPPED_RESOURCE_NAMES:
            continue
        target = destination / child.name
        if child.is_dir():
            _copy_resource_directory(child, target)
            continue
        _copy_resource_file(child, target)


def _copy_resource_file(source, destination: Path) -> None:
    with source.open("rb") as source_file, destination.open("xb") as target_file:
        shutil.copyfileobj(source_file, target_file)


def load_prompt_templates(directory: Path) -> PromptTemplates:
    return PromptTemplates(
        system=(directory / "Soul.md").read_text(encoding="utf-8").strip(),
        subagent=(directory / "SubAgent.md").read_text(encoding="utf-8").strip(),
        consolidator=(
            (directory / "Consolidator.md").read_text(encoding="utf-8").strip()
        ),
        workspace_memory=(
            (directory / "WorkspaceMemory.md").read_text(encoding="utf-8").strip()
        ),
    )


def _read_config(
    path: Path,
) -> tuple[str, dict[str, object]]:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    main_agent = data.get("main_agent")
    if not isinstance(main_agent, dict):
        raise ValueError("config field 'main_agent' must be an object")
    provider = main_agent.get("provider")
    providers = data.get("providers")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError(
            "config field 'main_agent.provider' must be a non-empty string"
        )
    provider = provider.strip()
    if not isinstance(providers, dict):
        raise ValueError("config field 'providers' must be an object")
    if provider not in providers:
        raise ValueError(f"provider '{provider}' is not configured")
    return provider, providers


def _provider_entry(
    providers: dict[str, object],
    provider: str,
) -> dict[str, object]:
    selected = providers.get(provider)
    if not isinstance(selected, dict):
        raise ValueError(f"provider '{provider}' is not configured")
    return selected


def _model_name(selected: dict[str, object], provider: str) -> str:
    model = selected.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(
            f"provider '{provider}' field 'model' must be a non-empty string"
        )
    return model.strip()


def load_model_options(path: Path) -> tuple[str, dict[str, str]]:
    """Return the default provider name and every provider's model name.

    Keys are deliberately not resolved: listing the choices must not
    require a key for providers the user is not using.
    """
    default, providers = _read_config(path)
    return default, {
        name: _model_name(_provider_entry(providers, name), name)
        for name in providers
    }


def load_config(path: Path, provider: str | None = None) -> ModelConfig:
    default, providers = _read_config(path)
    provider = default if provider is None else provider
    selected = _provider_entry(providers, provider)
    model = _model_name(selected, provider)

    url = _optional_string(selected, "url", provider)
    key = _optional_string(selected, "key", provider)
    max_context_tokens = _optional_positive_integer(
        selected,
        "max_context_tokens",
        provider,
    )
    reasoning_effort = selected.get("reasoning_effort")
    if reasoning_effort is not None:
        reasoning_effort = normalize_semantic_effort(reasoning_effort)
    reasoning_parameters = selected.get("reasoning_parameters", {})
    if not isinstance(reasoning_parameters, dict):
        raise ValueError(
            f"provider '{provider}' field 'reasoning_parameters' must be an object"
        )
    reasoning_parameters = validate_reasoning_parameters(reasoning_parameters)
    api = selected.get("api")
    if api is not None and (not isinstance(api, str) or api not in OPENAI_APIS):
        raise ValueError("provider api must be responses or chat_completions")
    if key and key.startswith("${") and key.endswith("}"):
        key_env = key[2:-1]
        if not key_env:
            raise ValueError(
                f"provider '{provider}' field 'key' has an empty "
                "environment variable reference"
            )
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(
                f"environment variable '{key_env}' is required "
                f"for provider '{provider}'"
            )

    return ModelConfig(
        model=model,
        url=url,
        key=key,
        max_context_tokens=max_context_tokens,
        reasoning_effort=reasoning_effort,
        reasoning_parameters=reasoning_parameters,
        api=api,
    )


def _optional_string(
    data: dict[str, object],
    field: str,
    provider: str,
) -> str | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"provider '{provider}' field '{field}' must be a non-empty string"
        )
    return value.strip()


def _optional_positive_integer(
    data: dict[str, object],
    field: str,
    provider: str,
) -> int | None:
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"provider '{provider}' field '{field}' must be a positive integer"
        )
    return value
