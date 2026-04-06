from functools import cached_property
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class DuplicateStaticBasenameError(ValueError):
    pass


class Config(BaseSettings):
    model_config = {"frozen": False}

    claude_web_chat_javascript_plugins: list[str] | None = None
    claude_web_chat_static_paths: list[str] | None = None
    claude_web_chat_host: str = "127.0.0.1"
    claude_web_chat_port: int = 8000

    @field_validator("claude_web_chat_javascript_plugins", "claude_web_chat_static_paths", mode="before")
    @classmethod
    def split_comma_separated(cls, value: object) -> list[str] | None:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        return None

    @cached_property
    def javascript_plugin_basenames(self) -> list[str]:
        if not self.claude_web_chat_javascript_plugins:
            return []
        return [Path(plugin_path).name for plugin_path in self.claude_web_chat_javascript_plugins]

    @cached_property
    def static_file_basename_to_path(self) -> dict[str, str]:
        all_paths = [
            *(self.claude_web_chat_javascript_plugins or []),
            *(self.claude_web_chat_static_paths or []),
        ]
        if not all_paths:
            return {}
        return {Path(file_path).name: file_path for file_path in all_paths}


def load_config() -> Config:
    return Config()
