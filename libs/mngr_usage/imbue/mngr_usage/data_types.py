from __future__ import annotations

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import PluginConfig

WINDOW_KEYS: tuple[str, ...] = ("five_hour", "seven_day", "overage")


class UsagePluginConfig(PluginConfig):
    """Configuration for the usage plugin."""

    max_age_seconds: int = Field(
        default=300,
        description="Snapshot freshness threshold in seconds. When the snapshot's "
        "updated_at is older than this, `mngr usage` prints a stale-cache warning. "
        "Reader-only -- this plugin doesn't capture data, it asks data-providing "
        "plugins (e.g. mngr_claude_usage) for the latest snapshot via the "
        "`current_usage_snapshot` hook.",
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
    """A single rate-limit window's snapshot state.

    Each provider's hookimpl populates these from whatever data source it
    owns. The fields are intentionally generic enough to cover Claude.ai's
    rate_limits payload (the only provider today) and any future provider
    that emits per-window usage percentages with reset timestamps.

    ``status`` and ``is_using_overage`` are declared as optional fields
    defaulting to None; no current provider emits them, but they're
    reserved for forward-compat without a schema bump.
    """

    used_percentage: float | None = Field(default=None)
    resets_at: int | None = Field(default=None, description="Unix timestamp when this window resets")
    status: str | None = Field(default=None)
    is_using_overage: bool | None = Field(default=None)


class UsageSnapshot(FrozenModel):
    """A complete usage snapshot returned by a ``current_usage_snapshot`` hookimpl.

    Carries:
    - ``source_name``: free-form identifier for the originating provider
      (e.g. ``"claude"``). Used in the human output and as a tiebreaker
      across hookimpls. Should not contain spaces.
    - ``windows``: per-window state, keyed by names from ``WINDOW_KEYS`` for
      the standard cases. Providers may include other window names too;
      ``mngr usage`` renders unknown ones with the literal key as the label.
    - ``updated_at``: Unix timestamp the provider regards as the snapshot's
      freshness. The CLI uses this to pick the freshest snapshot when
      multiple providers contribute, and to compute the stale-warning age.
    """

    source_name: str = Field(description="Provider name, e.g. 'claude'")
    windows: dict[str, WindowSnapshot] = Field(
        default_factory=dict,
        description="Per-window state, keyed by window name (five_hour / seven_day / overage / ...).",
    )
    updated_at: int = Field(description="Unix timestamp this snapshot was last refreshed")
