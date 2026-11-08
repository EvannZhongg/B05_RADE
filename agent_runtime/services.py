"""Process resources shared by otherwise independent session runtimes."""

import json
from pathlib import Path

from .settings import EnvironmentReloader, SettingsStore


class RuntimeServices:
    def __init__(self, config_directory: Path) -> None:
        self.config_directory = config_directory.resolve()
        self.environment = EnvironmentReloader(self.config_directory / ".env")
        self.settings = SettingsStore(self.config_directory, self.environment)

    def warmup(self) -> None:
        """Load the default provider's code and vocabulary before accepting sessions."""
        from agent_core.providers import OpenAIProvider

        from .config import load_model_options

        self.environment.reload()
        path = self.config_directory / "provider_config.json"
        default, models = load_model_options(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        # Warming code must work before the user has configured credentials.
        OpenAIProvider.warm(models[default], document["providers"][default].get("api"))
