from enum import auto
from typing import Any

from pydantic import Field
from pydantic import field_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr.config.data_types import PluginConfig


class ContextCompactionMode(UpperCaseStrEnum):
    """How context compaction should be triggered for conversational agents."""

    DISABLED = auto()
    ON_NEXT_PROMPT = auto()
    PROACTIVE_TIMER = auto()


class AutoCompactPluginConfig(PluginConfig):
    """Configuration for automatic context compaction."""

    mode: ContextCompactionMode = Field(
        default=ContextCompactionMode.DISABLED,
        description="Compaction mode: disabled, on_next_prompt, or proactive_timer.",
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.upper()
        return v

    cache_ttl_minutes: int | None = Field(
        default=None,
        ge=1,
        description="Override for model cache TTL in minutes. If omitted, uses the agent's reported TTL.",
    )
    epsilon_offset_minutes: int = Field(
        default=3,
        ge=0,
        description="How many minutes before cache expiry to trigger compaction.",
    )
    min_context_tokens: int = Field(
        default=100_000,
        ge=0,
        description="Minimum context size in tokens required to trigger compaction. Set to 0 to disable gating.",
    )

    def get_trigger_delay_seconds(self, cache_ttl_minutes: int) -> float:
        """Calculate delay in seconds before triggering compaction for a given cache TTL."""
        return max(0.0, float((cache_ttl_minutes - self.epsilon_offset_minutes) * 60))
