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
        "updated_at is older than this, `mngr usage` prints a stale-snapshot warning. "
        "Reader-only -- this plugin doesn't capture data, it walks events files "
        "produced by writer plugins (one event per provisioned agent's rate-limit "
        "render) and renders the freshest.",
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

    The fields are intentionally generic: any provider that emits per-window
    usage percentages with reset timestamps fits, regardless of which API
    the percentages came from.

    ``status`` and ``is_using_overage`` are declared as optional fields
    defaulting to None; reserved for forward-compat without a schema bump.
    """

    used_percentage: float | None = Field(default=None)
    resets_at: int | None = Field(default=None, description="Unix timestamp when this window resets")
    status: str | None = Field(default=None)
    is_using_overage: bool | None = Field(default=None)


class UsageSnapshot(FrozenModel):
    """A complete usage snapshot derived from one writer's events file.

    Carries:
    - ``source_name``: free-form identifier for the writer (taken from the
      ``<source>`` segment of ``events/<source>/rate_limits/events.jsonl``).
      Used in the ``[source]`` header and as a tiebreaker when multiple
      writers contribute. Should not contain spaces.
    - ``windows``: per-window state, keyed by names from ``WINDOW_KEYS`` for
      the standard cases. Writers may include other window names too;
      ``mngr usage`` renders unknown ones with the literal key as the label.
    - ``updated_at``: Unix timestamp the writer regards as the snapshot's
      freshness. The CLI uses this to pick the freshest snapshot when
      multiple writers contribute, and to compute the stale-warning age.
    """

    source_name: str = Field(description="Writer-chosen source identifier")
    windows: dict[str, WindowSnapshot] = Field(
        default_factory=dict,
        description="Per-window state, keyed by window name (five_hour / seven_day / overage / ...).",
    )
    updated_at: int = Field(description="Unix timestamp this snapshot was last refreshed")
