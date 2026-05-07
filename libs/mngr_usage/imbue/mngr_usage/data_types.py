from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import PluginConfig

CACHE_SCHEMA_VERSION: Literal[1] = 1
WINDOW_KEYS: tuple[str, ...] = ("five_hour", "seven_day", "overage")
CACHE_RELATIVE_PATH: Path = Path("usage") / "claude_rate_limits.json"


class UsagePluginConfig(PluginConfig):
    """Configuration for the usage plugin."""

    max_age_seconds: int = Field(
        default=300,
        description="Cache freshness threshold in seconds. When the oldest window's updated_at "
        "is older than this, `mngr usage` prints a stale-cache warning. (Reader-only -- "
        "the plugin never writes to the cache itself; that's the per-agent statusline shim.)",
    )

    def merge_with(self, override: PluginConfig) -> UsagePluginConfig:
        """Merge with override config (FrozenModel-style)."""
        if not isinstance(override, UsagePluginConfig):
            return self
        return UsagePluginConfig(
            enabled=override.enabled if override.enabled is not None else self.enabled,
            max_age_seconds=override.max_age_seconds if override.max_age_seconds is not None else self.max_age_seconds,
        )


class WindowSnapshot(FrozenModel):
    """A single rate-limit window's cached state.

    Populated by the per-agent statusline shim from the JSON Claude Code feeds
    to its statusline command on every render. Status and is_using_overage
    fields are reserved for forward compatibility -- they're not currently
    emitted by any writer but the schema tolerates them so older cache files
    deserialize cleanly.
    """

    used_percentage: float | None = Field(default=None)
    resets_at: int | None = Field(default=None, description="Unix timestamp when this window resets")
    status: str | None = Field(default=None)
    is_using_overage: bool | None = Field(default=None)
    source: str | None = Field(default=None, description="Which writer last wrote this entry")
    updated_at: int | None = Field(default=None, description="Unix timestamp when this entry was last written")


class CacheDoc(FrozenModel):
    """The on-disk cache document. See claude_rate_limits_writer.sh for the canonical schema."""

    schema_version: int = Field(default=CACHE_SCHEMA_VERSION)
    windows: dict[str, WindowSnapshot] = Field(default_factory=dict)
