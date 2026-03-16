from __future__ import annotations

from pydantic import Field

from imbue.mng_llm.data_types import LlmSettings
from imbue.mng_mind.data_types import WatcherSettings


class ClaudeMindSettings(LlmSettings):
    """Top-level settings loaded from minds.toml.

    Extends LlmSettings (chat, provisioning) with mind-specific sections
    (agent_type, watchers). All fields have defaults, so an empty or missing
    settings file produces a valid settings object with the standard defaults.
    """

    agent_type: str | None = Field(
        default=None,
        description="Agent type for this mind (e.g. 'elena-code', 'claude-mind'). "
        "Read during agent creation to determine the --type passed to mng create. "
        "Falls back to 'claude-mind' when not set.",
    )
    watchers: WatcherSettings = Field(
        default_factory=WatcherSettings,
        description="Watcher settings ([watchers] section).",
    )
