"""Claude usage data writer for `mngr usage`.

Single responsibility: install a per-agent statusline shim into Claude
agents so each render appends one ``cost_snapshot`` event (carrying
rate_limits + cost + session_id) to
``$MNGR_AGENT_STATE_DIR/events/claude/usage/events.jsonl``.

Discovery is by convention -- ``mngr usage`` enumerates agents via
``list_agents`` and reads each agent's ``events/<source>/usage/
events.jsonl`` via the events API (``discover_event_sources`` +
``read_event_content``), the same mechanism ``mngr event`` uses. We don't
implement a reader hookspec; we just write to the conventional path and let
the generic CLI find the data uniformly for local and remote agents.

Provisioning runs from a single ``on_before_provisioning`` hookimpl on
mngr core, so this plugin doesn't depend on any Claude-specific hookspec.
All file I/O goes through ``host.read_text_file`` / ``host.write_file``
so the provisioner works for local and remote agents uniformly.
"""

from __future__ import annotations

import json
from pathlib import Path

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.hosts.host import install_packaged_script_on_host
from imbue.mngr.hosts.host import read_json_dict_via_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude_usage import resources as _resources

_USAGE_WRITER_SCRIPT = "claude_usage_writer.sh"
_STATUSLINE_SHIM_SCRIPT = "claude_statusline.sh"
_USER_STATUSLINE_CMD_FILE = "user_statusline_cmd"


def _capture_existing_statusline_command(host: OnlineHostInterface, work_dir: Path, our_shim_path: str) -> str:
    """Capture the user's pre-existing ``statusLine.command`` so the shim can chain to it.

    Reads ``<work_dir>/.claude/settings.local.json`` first (local tier wins in
    Claude Code's precedence stack), then ``<work_dir>/.claude/settings.json``.
    Returns ``""`` if there's nothing to wrap.

    Skips ``our_shim_path`` -- on re-provisioning, settings.local.json's
    ``statusLine.command`` is OUR shim, and capturing it would form a recursive
    wrap.
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
        if command.strip() == our_shim_path.strip():
            continue
        return command
    return ""


def _write_user_statusline_cmd(host: OnlineHostInterface, commands_dir: Path, command: str) -> None:
    """Write the captured user command to the sidecar file the shim reads.

    Empty ``command`` means the most recent capture pass found nothing; on
    re-provisioning that's the expected state for users whose original
    statusline lived in ``settings.local.json`` (our first provision
    captured it into the sidecar and overwrote settings.local.json with our
    shim, so subsequent runs find only our shim there). To avoid silently
    dropping the previously-captured user command, we preserve any existing
    non-empty sidecar when called with an empty ``command``. (A user who
    genuinely wants to clear their wrapped statusline can delete the
    sidecar manually -- it's still a strictly better failure mode than
    silent loss.)
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
    """Install shim, writer, sidecar, and settings.local.json statusLine.

    All file writes go through the ``host`` so this works uniformly for local
    and remote agents.
    """
    commands_dir = state_dir / "commands"
    shim_path = str(commands_dir / _STATUSLINE_SHIM_SCRIPT)
    user_cmd = _capture_existing_statusline_command(host, work_dir, our_shim_path=shim_path)
    _write_user_statusline_cmd(host, commands_dir, user_cmd)
    install_packaged_script_on_host(
        host, module=_resources, filename=_STATUSLINE_SHIM_SCRIPT, dest=commands_dir / _STATUSLINE_SHIM_SCRIPT
    )
    install_packaged_script_on_host(
        host,
        module=_resources,
        filename=_USAGE_WRITER_SCRIPT,
        dest=commands_dir / _USAGE_WRITER_SCRIPT,
    )
    _install_settings_local_statusline(host, work_dir, shim_path)


@hookimpl
def on_before_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Provision the usage statusline shim for Claude agents on any host.

    Steps:
    1. Capture the user's pre-existing ``statusLine.command`` (if any) into
       ``<state_dir>/commands/user_statusline_cmd`` so the shim can chain.
    2. Install the shim and the writer into ``<state_dir>/commands/``.
    3. Set ``<work_dir>/.claude/settings.local.json``'s ``statusLine.command``
       to point at our shim (local-tier wins over project-tier in Claude
       Code's precedence stack).

    All writes go through ``host.write_file`` so the provisioner works for
    local and remote agents the same way. Skips non-Claude agents only; the
    ``isinstance`` check covers ``claude``, ``headless_claude``, and
    user-defined agent types whose ``parent_type`` chain reaches ``claude``
    (e.g. config-defined templates like ``write-plus``).
    """
    if not isinstance(agent, ClaudeAgent):
        return
    _provision_statusline_shim(
        host,
        get_agent_state_dir_path(host.host_dir, agent.id),
        agent.work_dir,
    )
