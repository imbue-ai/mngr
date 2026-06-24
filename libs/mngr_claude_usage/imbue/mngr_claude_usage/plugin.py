"""Claude usage data writer and reader for `mngr usage`.

Install a host-stable statusline shim that, when invoked
by Claude Code, appends one ``cost_snapshot`` event (carrying rate_limits +
cost + session_id) to ``$MNGR_AGENT_STATE_DIR/events/claude/usage/events.jsonl``
and chains to any user-defined ``statusLine.command``.

The shim itself lives at ``<host_dir>/commands/claude_statusline.sh`` (one
shared copy per host) so the ``statusLine.command`` entry written into the
work_dir's ``settings.local.json`` stays valid across the entire lifetime of
the host -- it does not point at any one agent's state dir. The per-agent
state still drives the actual event-emit destination (``$MNGR_AGENT_STATE_DIR``)
and the captured "user statusline" sidecar
(``$MNGR_AGENT_STATE_DIR/commands/user_statusline_cmd``); the shim reads both
from the environment at render time. If ``MNGR_AGENT_STATE_DIR`` is unset --
e.g. claude is invoked standalone, outside of an mngr agent -- the shim exits
0 silently.

Discovery is by convention -- ``mngr usage`` enumerates agents via
``list_agents`` and reads each agent's ``events/<source>/usage/events.jsonl``
via the events API (``discover_event_sources`` + ``read_event_content``), the
same mechanism ``mngr event`` uses. This module also provides the matching
``aggregate_usage_source`` reader hookimpl (like the other usage packages): it
claims the ``claude`` source and aggregates it with the process-cumulative
strategy, declining every other source.

Provisioning runs from a single ``on_before_provisioning`` hookimpl on mngr
core, so this plugin doesn't depend on any Claude-specific hookspec. All file
I/O goes through ``host.read_text_file`` / ``host.write_file`` so the
provisioner works for local and remote agents uniformly.
"""

from __future__ import annotations

import json
from pathlib import Path

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.common import get_agents_root_dir
from imbue.mngr.hosts.host import install_packaged_script_on_host
from imbue.mngr.hosts.host import read_json_dict_via_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_claude.plugin import ClaudeCoreAgent
from imbue.mngr_claude_usage import resources as _resources
from imbue.mngr_usage.api import aggregate_process_cumulative
from imbue.mngr_usage.data_types import UsageEvent
from imbue.mngr_usage.data_types import UsageSnapshot

# Source name the Claude writer emits under ($STATE_DIR/events/claude/usage/...);
# the reader strips "/usage", so this hookimpl claims exactly "claude".
_CLAUDE_USAGE_SOURCE_NAME = "claude"

_USAGE_WRITER_SCRIPT = "claude_usage_writer.sh"
_STATUSLINE_SHIM_SCRIPT = "claude_statusline.sh"
_USER_STATUSLINE_CMD_FILE = "user_statusline_cmd"

# Host-stable subdirectory of <host_dir> where mngr_claude_usage installs the
# shim and writer scripts. Single copy per host; shared by every claude agent
# on the host. Distinct from the per-agent ``<state_dir>/commands/`` dir where
# the runtime sidecar (``user_statusline_cmd``) lives.
_HOST_COMMANDS_SUBDIR = "commands"


def _host_commands_dir(host_dir: Path) -> Path:
    """Return the host-stable commands dir for the shim and writer scripts."""
    return host_dir / _HOST_COMMANDS_SUBDIR


def _stable_shim_path(host_dir: Path) -> Path:
    """Return the host-stable shim path that ``settings.local.json`` points at."""
    return _host_commands_dir(host_dir) / _STATUSLINE_SHIM_SCRIPT


def _is_mngr_owned_shim_path(command: str, host_dir: Path) -> bool:
    """True if ``command`` points to a mngr-installed statusline shim.

    Matches either the current host-stable location
    (``<host_dir>/commands/claude_statusline.sh``) or any legacy per-agent
    location (``<host_dir>/agents/<id>/commands/claude_statusline.sh``) left
    over by an older version of this plugin. Treating both as "ours" matters
    in two cases:

    1. Re-provisioning in a work_dir whose ``settings.local.json`` already
       points at the stable shim -- capturing our own path would form a
       trivial self-chain.
    2. Migration: a work_dir whose ``settings.local.json`` still points at a
       prior agent's per-agent shim (the old layout). Treating that as a user
       command would either chain to a destroyed agent's script (broken) or,
       if the agent's state dir still exists, form the infinite-recursion loop
       that motivated the move to a stable path. Skip the capture and let
       ``_install_settings_local_statusline`` overwrite with the stable path
       on this very provision pass.
    """
    candidate = Path(command.strip())
    if candidate.name != _STATUSLINE_SHIM_SCRIPT:
        return False
    if candidate.parent.name != "commands":
        return False
    if candidate == _stable_shim_path(host_dir):
        return True
    return candidate.parent.parent.parent == get_agents_root_dir(host_dir)


def _capture_existing_statusline_command(host: OnlineHostInterface, work_dir: Path) -> str:
    """Capture the user's pre-existing ``statusLine.command`` so the shim can chain to it.

    Reads ``<work_dir>/.claude/settings.local.json`` first (local tier wins in
    Claude Code's precedence stack), then ``<work_dir>/.claude/settings.json``.
    Returns ``""`` if there's nothing to wrap.

    Skips any path that looks like a mngr-owned shim (the current host-stable
    path or any legacy per-agent path) so we never chain back into ourselves.
    See :func:`_is_mngr_owned_shim_path` for the rationale.
    """
    claude_dir = work_dir / ".claude"
    for filename in ("settings.local.json", "settings.json"):
        settings = read_json_dict_via_host(host, claude_dir / filename)
        statusline = settings.get("statusLine")
        if not isinstance(statusline, dict):
            continue
        command = statusline.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        if _is_mngr_owned_shim_path(command, host.host_dir):
            continue
        return command
    return ""


def _write_user_statusline_cmd(host: OnlineHostInterface, commands_dir: Path, command: str) -> None:
    """Write the captured user command to the per-agent sidecar the shim reads.

    The sidecar lives at ``$MNGR_AGENT_STATE_DIR/commands/user_statusline_cmd``
    (per-agent, unversioned) so the shim can dereference it from the env at
    render time. Empty ``command`` means the most recent capture pass found
    nothing -- preserve any existing non-empty sidecar so a no-op re-provision
    (settings.local.json now holds our shim, not the original user command)
    does not silently drop the previously captured command. A user who
    genuinely wants to clear the wrapped statusline can delete the sidecar
    manually.
    """
    sidecar = commands_dir / _USER_STATUSLINE_CMD_FILE
    if not command:
        try:
            existing = host.read_text_file(sidecar)
        except FileNotFoundError:
            existing = ""
        if existing:
            return
    host.write_file(sidecar, command.encode())


def _install_settings_local_statusline(host: OnlineHostInterface, work_dir: Path, statusline_command: str) -> None:
    """Set ``statusLine.command`` in ``<work_dir>/.claude/settings.local.json`` on the host.

    Merges with whatever else is in that file (other plugins or the user may
    have written hooks, MCP servers, etc.). Atomic via ``host.write_file``'s
    ``is_atomic=True`` so a partial write can't corrupt the file.
    """
    settings_path = work_dir / ".claude" / "settings.local.json"
    settings = read_json_dict_via_host(host, settings_path)
    settings["statusLine"] = {"type": "command", "command": statusline_command}
    host.write_file(settings_path, (json.dumps(settings, indent=2) + "\n").encode(), is_atomic=True)


def _provision_statusline_shim(host: OnlineHostInterface, state_dir: Path, work_dir: Path) -> None:
    """Install shim+writer at the host-stable commands dir, sidecar in the per-agent
    state_dir, and point ``settings.local.json`` at the stable shim path.

    All file writes go through the ``host`` so this works uniformly for local and
    remote agents. Idempotent: re-running on the same host overwrites the shim and
    writer scripts in place (cheap), and ``_install_settings_local_statusline`` is
    a no-op if the entry already points at the stable shim.
    """
    host_commands_dir = _host_commands_dir(host.host_dir)
    shim_path = str(_stable_shim_path(host.host_dir))

    install_packaged_script_on_host(
        host,
        module=_resources,
        filename=_STATUSLINE_SHIM_SCRIPT,
        dest=host_commands_dir / _STATUSLINE_SHIM_SCRIPT,
    )
    install_packaged_script_on_host(
        host,
        module=_resources,
        filename=_USAGE_WRITER_SCRIPT,
        dest=host_commands_dir / _USAGE_WRITER_SCRIPT,
    )

    sidecar_dir = state_dir / "commands"
    user_cmd = _capture_existing_statusline_command(host, work_dir)
    _write_user_statusline_cmd(host, sidecar_dir, user_cmd)

    _install_settings_local_statusline(host, work_dir, shim_path)


@hookimpl
def on_before_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Provision the usage statusline shim for Claude agents on any host.

    Steps:
    1. Install the shim and the writer into ``<host_dir>/commands/`` (host-stable;
       a single copy shared by every claude agent on this host).
    2. Capture the user's pre-existing ``statusLine.command`` (if any) into
       ``<state_dir>/commands/user_statusline_cmd`` (per-agent sidecar that
       the shim reads at render time via ``$MNGR_AGENT_STATE_DIR``).
    3. Set ``<work_dir>/.claude/settings.local.json``'s ``statusLine.command``
       to point at the stable shim (local-tier wins over project-tier in Claude
       Code's precedence stack).

    All writes go through ``host.write_file`` so the provisioner works for local
    and remote agents the same way. Skips non-Claude agents only; the
    ``isinstance`` check is against ``ClaudeCoreAgent`` (the shared base of every
    Claude agent), so it covers ``claude``, ``headless_claude``, and user-defined
    agent types whose ``parent_type`` chain reaches ``claude`` (e.g.
    config-defined templates like a custom ``coder``).
    """
    if not isinstance(agent, ClaudeCoreAgent):
        return
    _provision_statusline_shim(
        host,
        get_agent_state_dir_path(host.host_dir, agent.id),
        agent.work_dir,
    )


@hookimpl
def aggregate_usage_source(
    source_name: str,
    agents_events: dict[str, list[UsageEvent]],
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate the Claude usage source; decline every other source.

    Claude Code reports cost cumulatively across a process's lifetime (a
    ``/clear`` rotates ``session_id`` without resetting cost), so this is the
    process-cumulative strategy. Returning None for non-Claude sources lets the
    firstresult hook fall through to another plugin (or the dispatcher fallback).
    """
    if source_name != _CLAUDE_USAGE_SOURCE_NAME:
        return None
    return aggregate_process_cumulative(source_name, agents_events, since_seconds=since_seconds, now=now)
