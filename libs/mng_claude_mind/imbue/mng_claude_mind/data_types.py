from __future__ import annotations

from pydantic import Field

from imbue.mng_llm.data_types import LlmSettings
from imbue.mng_mind.data_types import ConversationId as ConversationId
from imbue.mng_mind.data_types import MessageEvent as MessageEvent
from imbue.mng_mind.data_types import MessageRole as MessageRole
from imbue.mng_mind.data_types import MindEvent as MindEvent
from imbue.mng_mind.data_types import SOURCE_COMMON_TRANSCRIPT as SOURCE_COMMON_TRANSCRIPT
from imbue.mng_mind.data_types import SOURCE_DELIVERY_FAILURES as SOURCE_DELIVERY_FAILURES
from imbue.mng_mind.data_types import SOURCE_MESSAGES as SOURCE_MESSAGES
from imbue.mng_mind.data_types import SOURCE_MNG_AGENTS as SOURCE_MNG_AGENTS
from imbue.mng_mind.data_types import SOURCE_MONITOR as SOURCE_MONITOR
from imbue.mng_mind.data_types import SOURCE_SCHEDULED as SOURCE_SCHEDULED
from imbue.mng_mind.data_types import SOURCE_STOP as SOURCE_STOP
from imbue.mng_mind.data_types import WatcherSettings as WatcherSettings


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
