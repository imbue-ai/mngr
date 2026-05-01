from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pluggy
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import MngrContext

hookspec = pluggy.HookspecMarker("mngr")


class ClaudeExtraSettingsContribution(FrozenModel):
    """A contribution from a plugin that wants to extend a Claude agent's per-agent settings.

    Plugins return one of these from claude_extra_per_agent_settings to:
    - install a statusLine.command into the agent's settings.json
    - add env vars to the settings.json env block (e.g. for forwarding to hooks)
    - request that resource scripts from the contributing plugin be provisioned
      to ``$MNGR_AGENT_STATE_DIR/commands/`` so the statusline command can call them
    """

    model_config = {"arbitrary_types_allowed": True}

    statusline_command: str | None = Field(
        default=None,
        description="Command string to install at settings.json's statusLine.command. "
        "If multiple plugins contribute one, the first non-None wins.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Env vars to merge into settings.json's env block. "
        "Later contributions overwrite earlier ones for the same key.",
    )
    resource_scripts: tuple[str, ...] = Field(
        default=(),
        description="File names of shell/python scripts to provision into the agent's commands dir, "
        "loaded from the contributing plugin's resource module.",
    )
    resource_module: types.ModuleType | None = Field(
        default=None,
        description="Module from which to load resource_scripts via importlib.resources. "
        "Required if resource_scripts is non-empty.",
    )


@hookspec
def claude_extra_per_agent_settings(
    mngr_ctx: MngrContext,
    source_settings: dict[str, Any],
    agent_state_dir: Path,
) -> ClaudeExtraSettingsContribution | None:
    """Contribute statusline / env / scripts to a Claude agent's per-agent config.

    Called once per agent during ``_setup_per_agent_config_dir``. Plugins may
    inspect ``source_settings`` (the user's ``~/.claude/settings.json`` already
    parsed into a dict) to capture e.g. an existing ``statusLine.command`` so it
    can be wrapped or forwarded.

    ``agent_state_dir`` is the path to ``$MNGR_AGENT_STATE_DIR`` for this agent;
    plugins should reference resources at ``$MNGR_AGENT_STATE_DIR/commands/<script>``
    in any commands they install. ``mngr_ctx`` gives access to the user's
    ``profile_dir`` for shared on-disk state.

    Return a ``ClaudeExtraSettingsContribution`` to contribute, or ``None`` to abstain.
    """
