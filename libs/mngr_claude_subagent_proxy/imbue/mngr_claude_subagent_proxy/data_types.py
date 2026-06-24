"""Data types for the mngr_claude_subagent_proxy plugin."""

from __future__ import annotations

from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr.config.data_types import PluginConfig


class SubagentProxyMode(UpperCaseStrEnum):
    """Selects how the plugin handles a parent agent's Task tool calls.

    PROXY: route every Task call through a mngr-managed subagent via
    a Haiku dispatcher (default; the original behavior). Spawned
    subagents are observable through `mngr connect` / `mngr transcript`
    while running, and the parent's tool_result is the subagent's
    end-turn body.

    DENY: deny every Task call with a short permissionDecisionReason
    that points Claude at the ``mngr-proxy`` skill. The skill
    teaches an explicit two-command spawn-and-wait protocol Claude
    runs itself via the Bash tool (``mngr create`` then
    ``python -m imbue.mngr_claude_subagent_proxy.subagent_wait``) and
    treats subagent_wait's stdout as the Task tool's tool_result.
    Nothing is spawned by the deny hook itself, no per-Task
    wait-script is generated, no PostToolUse hook is installed, and
    no Stop-hook guarding or settings.json check runs. A SessionStart
    hook IS installed -- the same label-driven ``hooks/reap.py`` PROXY
    mode uses -- so terminal children spawned via the skill's protocol
    are reaped on the parent's next session start.

    Typed-subagent handling: when the parent calls Task with a
    specialized ``subagent_type`` (e.g. ``imbue-code-guardian:verify-and-fix``)
    whose agent definition resolves to an on-disk ``.md`` under
    ``<work_dir>/.claude/agents/``, ``~/.claude/agents/``, or
    ``~/.claude/plugins/marketplaces/*/plugins/<plugin>/agents/``, the
    deny reason includes a one-line pointer at that path so Claude
    knows to prepend its body (the spawned subagent's system prompt)
    to the prompt file before ``mngr create``. Built-in types (no
    on-disk file) and unresolved types fall through to the uniform
    short skill-pointer reason.

    Known limitation in this mode: tool restrictions declared in an
    agent definition's frontmatter (``tools: [Read, Grep]``, etc.) are
    NOT honored. The spawned mngr subagent inherits the user's full
    Claude config. The skill documents the limitation; a future
    extension can add ``--type`` variants with restricted permissions.
    """

    PROXY = auto()
    DENY = auto()


class SubagentProxyPluginConfig(PluginConfig):
    """Configuration for the mngr_claude_subagent_proxy plugin.

    This plugin is opt-in: it is DISABLED by default and only loads when a
    config layer explicitly sets ``[plugins.claude_subagent_proxy] enabled =
    true`` (enforced by ``OPT_IN_PLUGINS`` in mngr's config pre-reader). It is
    very experimental and breaks a lot of other tooling, so it must be turned
    on deliberately rather than relied upon by default.
    """

    mode: SubagentProxyMode = Field(
        default=SubagentProxyMode.PROXY,
        description="Whether to proxy Task calls through a mngr subagent (PROXY) "
        "or deny them with a short skill-pointer reason that directs Claude at "
        "the mngr-proxy skill (DENY).",
    )
