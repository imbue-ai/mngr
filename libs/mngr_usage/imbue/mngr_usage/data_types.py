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

    auto_refresh: bool = Field(
        default=True,
        description="When True, mngr usage automatically refreshes stale cache entries by spawning a brief claude -p call.",
    )
    max_age_seconds: int = Field(
        default=300,
        description="Cache freshness threshold in seconds. When the oldest window's updated_at is older than this, mngr usage triggers a refresh.",
    )
    refresh_model: str = Field(
        default="haiku",
        description="Claude model alias used for the refresh probe. Cheaper models like haiku reduce refresh cost.",
    )

    def merge_with(self, override: PluginConfig) -> UsagePluginConfig:
        """Merge with override config (FrozenModel-style)."""
        if not isinstance(override, UsagePluginConfig):
            return self
        return UsagePluginConfig(
            enabled=override.enabled if override.enabled is not None else self.enabled,
            auto_refresh=override.auto_refresh if override.auto_refresh is not None else self.auto_refresh,
            max_age_seconds=override.max_age_seconds if override.max_age_seconds is not None else self.max_age_seconds,
            refresh_model=override.refresh_model if override.refresh_model is not None else self.refresh_model,
        )


class WindowSnapshot(FrozenModel):
    """A single rate-limit window's cached state.

    Per-window "last write wins". The statusline writer fills used_percentage and resets_at;
    the SDK-event writer fills status, resets_at, and is_using_overage. Missing fields are None.
    """

    used_percentage: float | None = Field(default=None)
    resets_at: int | None = Field(default=None, description="Unix timestamp when this window resets")
    status: str | None = Field(default=None, description="Status from SDK rate_limit_event (e.g., 'rejected')")
    is_using_overage: bool | None = Field(default=None)
    source: str | None = Field(default=None, description="Which writer last wrote this entry")
    updated_at: int | None = Field(default=None, description="Unix timestamp when this entry was last written")


class CacheDoc(FrozenModel):
    """The on-disk cache document. See claude_rate_limits_writer.sh for the canonical schema."""

    schema_version: int = Field(default=CACHE_SCHEMA_VERSION)
    windows: dict[str, WindowSnapshot] = Field(default_factory=dict)
