from __future__ import annotations

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import PluginConfig


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

    The fields are intentionally generic: any writer that emits per-window
    usage percentages with reset timestamps fits, regardless of which API
    the percentages came from.

    ``label``, when present, is what the human renderer displays before the
    colon (e.g. ``"5h"``). When absent, the renderer falls back to the
    window's key as the label. Writers can use this to give compact display
    names while keeping keys identifier-safe so format templates work.

    ``status`` and ``is_using_overage`` are declared as optional fields
    defaulting to None; reserved for forward-compat without a schema bump.
    """

    used_percentage: float | None = Field(default=None)
    resets_at: int | None = Field(default=None, description="Unix timestamp when this window resets")
    window_seconds: int | None = Field(
        default=None,
        description="Window duration in seconds. When present (together with resets_at), enables "
        "the reader to derive elapsed_seconds / elapsed_percentage without baking per-window-class "
        "knowledge into mngr_usage. Writers emit this for fixed-duration windows (five_hour, "
        "seven_day, ...) and omit it for variable-duration windows (e.g. Claude's overage).",
    )
    label: str | None = Field(default=None, description="Human-display label; falls back to the window key.")
    status: str | None = Field(default=None)
    is_using_overage: bool | None = Field(default=None)


class UsageSnapshot(FrozenModel):
    """A complete usage snapshot derived from one writer's events file.

    Carries:
    - ``source_name``: free-form identifier for the writer (taken from the
      ``<source>`` segment of ``events/<source>/rate_limits/events.jsonl``).
      Used in the ``[source]`` header and as a tiebreaker when multiple
      writers contribute. Should not contain spaces.
    - ``windows``: per-window state. Keys are writer-chosen and treated as
      opaque by ``mngr usage``; render order is the writer's insertion order
      (preserved through the JSONL serialization). Per-window optional
      ``label`` controls the human display name (falls back to the key).
    - ``updated_at``: Unix timestamp the writer regards as the snapshot's
      freshness. The CLI uses this to pick the freshest snapshot when
      multiple writers contribute, and to compute the stale-warning age.
    """

    source_name: str = Field(description="Writer-chosen source identifier")
    windows: dict[str, WindowSnapshot] = Field(
        default_factory=dict,
        description="Per-window state, keyed by writer-chosen window names (insertion-order preserved).",
    )
    updated_at: int = Field(description="Unix timestamp this snapshot was last refreshed")
