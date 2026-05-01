"""Data types for the mngr_subagent_proxy plugin."""

from __future__ import annotations

from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr.config.data_types import PluginConfig


class SubagentProxyMode(UpperCaseStrEnum):
    """Selects how the plugin handles a parent agent's Task tool calls.

    PROXY: route every Task call through an mngr-managed subagent via
    a Haiku dispatcher (default; the original behavior). Spawned
    subagents are observable through `mngr connect` / `mngr transcript`
    while running, and the parent's tool_result is the subagent's
    end-turn body.

    DENY: deny every Task call with a permissionDecisionReason that
    contains a copy-pasteable invocation of `mngr create` / `mngr
    transcript`. The calling agent (Claude) is expected to run those
    commands itself via Bash to spawn an mngr-managed subagent and
    capture its reply, then continue. Nothing is spawned automatically;
    no PostToolUse / SessionStart hooks are installed; no Stop-hook
    guarding or settings.json check runs.
    """

    PROXY = auto()
    DENY = auto()


class SubagentProxyPluginConfig(PluginConfig):
    """Configuration for the mngr_subagent_proxy plugin."""

    mode: SubagentProxyMode = Field(
        default=SubagentProxyMode.PROXY,
        description="Whether to proxy Task calls through an mngr subagent (PROXY) "
        "or deny them with copy-pasteable mngr commands (DENY).",
    )

    def merge_with(self, override: "PluginConfig") -> "SubagentProxyPluginConfig":
        """Merge this config with an override config.

        Scalar fields: override wins if not None. Matches the convention
        established by other plugin configs (see ``RecursivePluginConfig``).

        Accepts the base ``PluginConfig`` type for LSP-compatibility with
        the parent's signature; behavior for non-subclass overrides falls
        through to the base merge.
        """
        if not isinstance(override, SubagentProxyPluginConfig):
            return SubagentProxyPluginConfig(
                enabled=override.enabled if override.enabled is not None else self.enabled,
                mode=self.mode,
            )
        merged_enabled = override.enabled if override.enabled is not None else self.enabled
        merged_mode = override.mode if override.mode is not None else self.mode
        return SubagentProxyPluginConfig(enabled=merged_enabled, mode=merged_mode)
