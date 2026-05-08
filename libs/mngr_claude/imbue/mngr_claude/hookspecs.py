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
    """A contribution from a plugin that wants to extend a Claude agent's per-agent setup.

    Plugins return one of these from ``claude_extra_per_agent_settings`` to:

    - install a ``statusLine.command`` for the agent. The command is written to
      ``<work_dir>/.claude/settings.local.json`` (the highest user-controllable
      precedence tier in Claude Code's settings stack), so it wins over any
      project-level ``.claude/settings.json``. The hookimpl is expected to read
      ``source_settings.statusLine.command`` (the project-level command, if
      any) out of the hook's ``source_settings`` argument and weave it into
      its own command (e.g. capture it into an env var for a wrapping shim
      to chain to), so we wrap rather than replace.
    - export env vars into the agent's *process* environment (via
      ``ClaudeAgent.modify_env_vars``). These are visible to Claude Code and
      any subprocess it spawns, including the statusline command and hooks.
      (We deliberately do *not* use the ``env`` block of settings.json here:
      it does not reliably propagate to Claude's process, and on this branch
      we found the values went unseen in practice.)
    - request resource scripts from the contributing plugin be provisioned
      to ``$MNGR_AGENT_STATE_DIR/commands/`` so the statusline command (or
      anything else) can reference them by absolute path.
    """

    model_config = {"arbitrary_types_allowed": True}

    statusline_command: str | None = Field(
        default=None,
        description="Command string to install at the agent's statusLine.command, "
        "written into <work_dir>/.claude/settings.local.json. "
        "If multiple plugins contribute one, later contributions overwrite earlier ones.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Env vars to merge into the agent's process environment. "
        "Later contributions overwrite earlier ones for the same key. "
        "These are exported via ClaudeAgent.modify_env_vars, NOT via settings.json's env block.",
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
    work_dir: Path,
    is_local: bool,
) -> ClaudeExtraSettingsContribution | None:
    """Contribute statusline / env / scripts to a Claude agent's per-agent config.

    Called once per agent during provisioning. Plugins may inspect
    ``source_settings`` -- the parsed contents of the project-level
    ``<work_dir>/.claude/settings.json`` (the file that would otherwise win
    Claude Code's settings precedence at the project tier) -- to capture e.g.
    an existing ``statusLine.command`` so it can be wrapped or forwarded.

    ``work_dir`` is the agent's working directory. The hookimpl's
    ``statusline_command`` is installed at
    ``<work_dir>/.claude/settings.local.json`` so it overrides the project's
    ``.claude/settings.json`` (a higher tier in Claude Code's precedence
    stack). ``agent_state_dir`` is ``$MNGR_AGENT_STATE_DIR``; plugins should
    reference resources at ``$MNGR_AGENT_STATE_DIR/commands/<script>``.
    ``mngr_ctx`` gives access to the user's ``profile_dir`` for shared
    on-disk state.

    ``is_local`` is True when the agent runs on the local host (i.e. shares
    the user's filesystem). Plugins should typically return ``None`` for
    remote hosts, since paths under the local user's profile_dir won't exist
    in the remote agent's environment.

    Return a ``ClaudeExtraSettingsContribution`` to contribute, or ``None`` to abstain.
    """
