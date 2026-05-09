"""Claude rate-limit data writer for `mngr usage`.

Single responsibility: install a per-agent statusline shim into Claude
agents so each render appends a rate-limit event to
``$MNGR_AGENT_STATE_DIR/events/claude/rate_limits/events.jsonl``.

Discovery is by convention -- ``mngr usage`` walks all
``events/<source>/rate_limits/events.jsonl`` files itself, mirroring how
``mngr transcript`` finds ``common_transcript`` events. We don't implement a
reader hookspec; we just write to the conventional path and let the generic
CLI find the data.

Provisioning runs from a single ``on_before_provisioning`` hookimpl on
mngr core, so this plugin doesn't depend on any Claude-specific hookspec.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude_usage import resources as _resources

_RATE_LIMITS_WRITER_SCRIPT = "claude_rate_limits_writer.sh"
_STATUSLINE_SHIM_SCRIPT = "claude_statusline.sh"
_USER_STATUSLINE_CMD_FILE = "user_statusline_cmd"


def _agent_state_dir(agent: AgentInterface, host: OnlineHostInterface) -> Path:
    """Mirror BaseAgent._get_agent_dir(): the per-agent state directory on this host."""
    return host.host_dir / "agents" / str(agent.id)


def _read_existing_settings(path: Path) -> dict[str, Any]:
    """Return the JSON contents of ``path`` as a dict, or empty if missing/malformed.

    A malformed settings.local.json is logged at warning level and treated as
    empty rather than raising -- we don't want a typo in the user's settings to
    break agent provisioning.
    """
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        logger.warning("Could not parse {} as JSON ({}); treating as empty.", path, e)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _capture_existing_statusline_command(work_dir: Path, our_shim_path: str) -> str:
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
        settings = _read_existing_settings(claude_dir / filename)
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


def _write_user_statusline_cmd(commands_dir: Path, command: str) -> None:
    """Write the captured user command to the sidecar file the shim reads.

    If the sidecar already exists with the same contents, skip the write (avoid
    needless mtime churn on re-provisioning).

    An empty ``command`` means "no user command was found in settings this
    run". On re-provisioning that's the expected state for users whose
    original statusline lived in ``settings.local.json``: our first provision
    captured it into the sidecar and overwrote settings.local.json with our
    shim, so subsequent runs find only our shim there and nothing in
    settings.json. To avoid silently dropping a previously-captured user
    command, we preserve any existing non-empty sidecar when called with an
    empty ``command``. (A user who genuinely wants to clear their wrapped
    statusline can delete the sidecar manually -- it's still a strictly
    better failure mode than silent loss.)
    """
    sidecar = commands_dir / _USER_STATUSLINE_CMD_FILE
    if not command and sidecar.is_file() and sidecar.read_text():
        return
    if sidecar.is_file() and sidecar.read_text() == command:
        return
    sidecar.write_text(command)


def _copy_resource_script(commands_dir: Path, filename: str) -> None:
    """Copy a packaged resource script into ``commands_dir`` and chmod +x.

    Uses ``importlib.resources.as_file`` so it works whether the package is
    installed as source files or zipped (we only ever ship as source today,
    but the pattern is correct).
    """
    dst = commands_dir / filename
    src_traversable = importlib.resources.files(_resources).joinpath(filename)
    with importlib.resources.as_file(src_traversable) as src_path:
        shutil.copyfile(src_path, dst)
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_settings_local_statusline(work_dir: Path, statusline_command: str) -> None:
    """Set the agent's ``statusLine.command`` in ``<work_dir>/.claude/settings.local.json``.

    Merges with whatever else is in that file (other plugins or the user may
    have written hooks, MCP servers, etc.). Atomic via temp + os.replace so a
    partial write can't corrupt the file.
    """
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.local.json"
    settings = _read_existing_settings(settings_path)
    settings["statusLine"] = {"type": "command", "command": statusline_command}
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2))
    os.replace(tmp, settings_path)


def _provision_statusline_shim(state_dir: Path, work_dir: Path) -> None:
    """Install shim, writer, sidecar, and settings.local.json statusLine.

    Factored out of the hookimpl so tests can exercise the full file-side
    behavior without needing a fully-constructed ``ClaudeAgent``.
    """
    commands_dir = state_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    shim_path = str(commands_dir / _STATUSLINE_SHIM_SCRIPT)
    user_cmd = _capture_existing_statusline_command(work_dir, our_shim_path=shim_path)
    _write_user_statusline_cmd(commands_dir, user_cmd)

    _copy_resource_script(commands_dir, _STATUSLINE_SHIM_SCRIPT)
    _copy_resource_script(commands_dir, _RATE_LIMITS_WRITER_SCRIPT)

    _install_settings_local_statusline(work_dir, shim_path)


@hookimpl
def on_before_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Provision the rate-limit statusline shim for Claude agents on the local host.

    Steps:
    1. Capture the user's pre-existing ``statusLine.command`` (if any) into
       ``<state_dir>/commands/user_statusline_cmd`` so the shim can chain.
    2. Copy the shim and the writer into ``<state_dir>/commands/``.
    3. Set ``<work_dir>/.claude/settings.local.json``'s ``statusLine.command``
       to point at our shim (local-tier wins over project-tier in Claude Code's
       precedence stack).

    Skips non-Claude agents and remote hosts: ``mngr usage`` walks the local
    host_dir for events files, so a remote-only events file would never be
    visible. The ``isinstance`` check covers ``claude``, ``headless_claude``,
    and user-defined agent types whose ``parent_type`` chain reaches
    ``claude`` (e.g. config-defined templates like ``write-plus``).
    """
    if not isinstance(agent, ClaudeAgent):
        return
    if not host.is_local:
        return
    _provision_statusline_shim(_agent_state_dir(agent, host), agent.work_dir)
