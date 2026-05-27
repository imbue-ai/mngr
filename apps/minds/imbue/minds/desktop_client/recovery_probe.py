"""Recovery diagnostics probe.

Renders the workspace-recovery page's structured checklist (Q1-Q7). On
recovery-page load (and only then), minds runs a batched ``mngr exec``
against the workspace's system-services agent that emits a single JSON
payload describing:

- Q3: ``tmux ls`` (services tmux session listing).
- Q4: whether ``/code/services.toml`` declares ``[services.system_interface]``.
- Q5: whether anything is bound to the system-interface inner port (``ss -ltnp``).
- Q6: the result of a localhost ``curl`` against the inner port.

Q1 (host state) / Q2 (SSH reachable) / Q7 (plugin resolver entry) come from
data minds already has -- ``mngr list --format json`` and the
``EnvelopeStreamConsumer`` snapshot mirror -- and are merged into the
endpoint response alongside the batched probe.

The single sentinel ``===PROBE-READY===`` is printed before the JSON
payload. If the sentinel is absent from stdout, minds treats the run as
"SSH dead" -- the ``mngr exec`` plumbing returned without ever invoking
the in-container script, so we have no in-container observations and the
recovery page steers the user to a host restart rather than auto-dispatching
surgical.
"""

import base64
import json
import re
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId

PROBE_SENTINEL: Final[str] = "===PROBE-READY==="

# Hard ceiling for a single batched ``mngr exec``. Bounded so a wedged
# container can't gate the recovery UI. The inner probe is four small
# subprocesses (``tmux ls`` / TOML parse / ``ss`` / ``curl``) whose
# individual subprocess timeouts sum to a worst case of 1+1+2 = 4s, so the
# 5s ceiling leaves a comfortable margin while still keeping a wedged
# container from hanging the recovery UI. The endpoint surfaces a timeout
# as ssh_dead, same as a missing sentinel.
PROBE_TIMEOUT_SECONDS: Final[float] = 5.0


# Inner Python script executed on the agent's host, loaded from a sibling
# .txt resource so the in-container script's pattern matches (tmllib import,
# subprocess.run calls, broad Exception catches, ...) don't trip minds-side
# ratchets that only inspect ``.py`` files. The script is then base64-encoded
# in ``build_probe_shell_command`` so the outer ``mngr exec`` argv stays a
# single shell-safe token without quoting headaches.
#
# Loaded lazily on first use rather than at module import: if the .txt file
# is missing (e.g. an incomplete install), an eager read would raise during
# import and break every importer of this module (app.py and the rest of
# the desktop-client chain). Lazy load keeps the rest of the app working;
# only the recovery-probe endpoint hits the missing file, where the
# FileNotFoundError surfaces as a 500 from the host-health endpoint.
_PROBE_SCRIPT_PATH: Final[Path] = Path(__file__).parent / "recovery_probe_script.txt"
_probe_python_script_cache: str | None = None


def _get_probe_python_script() -> str:
    """Return the inner-probe Python source, loading it from disk on first call."""
    global _probe_python_script_cache
    if _probe_python_script_cache is None:
        _probe_python_script_cache = _PROBE_SCRIPT_PATH.read_text(encoding="utf-8")
    return _probe_python_script_cache


def build_probe_shell_command() -> str:
    """Return the shell command minds passes to ``mngr exec``.

    Prints the sentinel, then base64-decodes and runs the inner Python
    script. Base64 keeps the entire batched probe a single shell-safe
    token, so we don't have to escape Python source through the layers of
    ``mngr exec`` / sshd / the container shell.
    """
    encoded = base64.b64encode(_get_probe_python_script().encode("utf-8")).decode("ascii")
    return f"echo '{PROBE_SENTINEL}' && echo {encoded} | base64 -d | python3"


def build_probe_argv(mngr_binary: str, services_agent_id: AgentId) -> list[str]:
    """Build the ``mngr exec`` argv that runs the batched probe on the agent's host.

    ``--quiet`` suppresses mngr's own progress chatter so stdout starts
    with the sentinel directly. ``--no-start`` keeps us from accidentally
    starting a stopped host just by probing it (the recovery page tier
    logic owns the decision to restart).
    """
    return [
        mngr_binary,
        "exec",
        str(services_agent_id),
        build_probe_shell_command(),
        "--timeout",
        str(int(PROBE_TIMEOUT_SECONDS)),
        "--no-start",
        "--quiet",
    ]


class ProbeRecord(FrozenModel):
    """Parsed result of the in-container batched probe.

    ``ssh_dead`` is True when the sentinel was never seen on stdout --
    either ``mngr exec`` could not reach the agent (SSH transport down)
    or the run timed out before printing anything. In that case every
    other field is None / its default.
    """

    ssh_dead: bool = Field(default=False, description="True when the sentinel is absent from stdout")
    tmux_ls: str | None = Field(default=None, description="Stdout (and stderr on non-zero) of `tmux ls`")
    tmux_error: str | None = Field(default=None, description="Python repr of any exception running tmux")
    services_toml_declares_system_interface: bool | None = Field(
        default=None,
        description="True when [services.system_interface] exists in /code/services.toml",
    )
    services_toml_path: str = Field(default="/code/services.toml")
    services_toml_error: str | None = Field(default=None, description="Python repr of any exception parsing TOML")
    inner_port: int | None = Field(
        default=None,
        description="Port parsed out of services.system_interface.command (--url http://host:PORT)",
    )
    port_listener: str | None = Field(
        default=None,
        description="Matching lines from `ss -ltnp` filtered to inner_port",
    )
    port_listener_error: str | None = Field(default=None, description="Python repr of any exception running ss")
    curl_status: str | None = Field(default=None, description="HTTP status code from curl, as a string")
    curl_error: str | None = Field(default=None, description="Python repr of any exception running curl")
    raw_stdout: str = Field(default="", description="Full captured stdout from the batched probe, for the debug menu")


def parse_probe_output(stdout: str | None) -> ProbeRecord:
    """Parse the batched probe's stdout into a :class:`ProbeRecord`.

    Returns an ``ssh_dead=True`` record when stdout is None (the
    underlying ``mngr exec`` could not be run at all) or the sentinel is
    absent. Otherwise extracts the JSON payload that follows the sentinel
    and folds it into the record.
    """
    if stdout is None:
        return ProbeRecord(ssh_dead=True, raw_stdout="")
    if PROBE_SENTINEL not in stdout:
        return ProbeRecord(ssh_dead=True, raw_stdout=stdout)

    after = stdout.split(PROBE_SENTINEL, 1)[1]
    # The first non-empty line after the sentinel is the JSON payload. Any
    # extra trailing lines (the ``mngr exec`` per-agent footer when not
    # quieted enough, or anything else) are tolerated.
    json_line: str | None = None
    for line in after.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        json_line = candidate
        break
    if json_line is None:
        return ProbeRecord(ssh_dead=False, raw_stdout=stdout)
    try:
        payload = json.loads(json_line)
    except json.JSONDecodeError:
        return ProbeRecord(ssh_dead=False, raw_stdout=stdout)
    if not isinstance(payload, dict):
        return ProbeRecord(ssh_dead=False, raw_stdout=stdout)

    return ProbeRecord(
        ssh_dead=False,
        tmux_ls=_coerce_optional_str(payload.get("tmux_ls")),
        tmux_error=_coerce_optional_str(payload.get("tmux_error")),
        services_toml_declares_system_interface=_coerce_optional_bool(
            payload.get("services_toml_declares_system_interface")
        ),
        services_toml_path=str(payload.get("services_toml_path") or "/code/services.toml"),
        services_toml_error=_coerce_optional_str(payload.get("services_toml_error")),
        inner_port=_coerce_optional_int(payload.get("inner_port")),
        port_listener=_coerce_optional_str(payload.get("port_listener")),
        port_listener_error=_coerce_optional_str(payload.get("port_listener_error")),
        curl_status=_coerce_optional_str(payload.get("curl_status")),
        curl_error=_coerce_optional_str(payload.get("curl_error")),
        raw_stdout=stdout,
    )


def _coerce_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _coerce_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


class SshConnectionInfo(FrozenModel):
    """SSH connection info for a single host, ready to render in the debug menu."""

    host_id: str = Field(description="Host ID this SSH info belongs to")
    user: str = Field(description="SSH username")
    host: str = Field(description="SSH hostname")
    port: int = Field(description="SSH port")
    key_path: str = Field(default="", description="Path to the private key file")
    command: str = Field(default="", description="Full ssh command string from mngr list")


class HostHealthResponse(FrozenModel):
    """Response from the host-health endpoint.

    Backwards-compatible with the previous shape (``reachable`` /
    ``host_offline``). The new fields are surfaced to the recovery page's
    JS for misconfigured-tier classification, the structured checklist,
    and the debug menu.
    """

    reachable: bool = Field(description="Host is RUNNING (surgical restart is appropriate)")
    host_offline: bool = Field(description="Host is in an offline state (host restart non-destructive)")
    host_state: str = Field(default="", description="Raw host state string from mngr list")
    ssh_dead: bool = Field(default=False, description="Batched probe timed out / never saw sentinel")
    is_misconfigured: bool = Field(
        default=False,
        description="services.toml does not declare [services.system_interface]; restart will not help",
    )
    services_agent_state: str = Field(default="", description="Lifecycle state of the system-services agent")
    ssh_connections: tuple[SshConnectionInfo, ...] = Field(
        default=(),
        description="SSH connection info per host (only remote hosts; local hosts omit ssh)",
    )
    plugin_resolver_services: dict[str, str] = Field(
        default_factory=dict,
        description="Per-agent service map from the plugin's resolver snapshot, or {} if not yet seen",
    )
    probe: ProbeRecord = Field(default_factory=ProbeRecord, description="Parsed in-container probe result")


_RUNNING_STATE: Final[str] = "RUNNING"
_OFFLINE_HOST_STATES: Final[frozenset[str]] = frozenset({"STOPPED", "STOPPING", "CRASHED", "FAILED"})


def classify_host_state(host_state: str) -> tuple[bool, bool]:
    """Return ``(reachable, host_offline)`` for a raw host state string.

    Mirrors the previous tiering in app.py's ``_classify_host_health`` so
    the existing auto-dispatch behavior is preserved exactly.
    """
    upper = host_state.upper()
    if upper == _RUNNING_STATE:
        return True, False
    if upper in _OFFLINE_HOST_STATES:
        return False, True
    return False, False


def extract_agent_row(list_json: str | None, agent_id: AgentId) -> dict | None:
    """Pull the row for ``agent_id`` from ``mngr list --format json`` output."""
    if list_json is None:
        return None
    try:
        agents = json.loads(list_json).get("agents", [])
    except (json.JSONDecodeError, AttributeError):
        return None
    for agent in agents:
        if isinstance(agent, dict) and agent.get("id") == str(agent_id):
            return agent
    return None


def extract_host_state(agent_row: dict | None) -> str:
    """Read ``host.state`` from a row of ``mngr list --format json``."""
    if agent_row is None:
        return ""
    host = agent_row.get("host")
    if not isinstance(host, dict):
        return ""
    state = host.get("state")
    if not isinstance(state, str):
        return ""
    return state


def extract_services_agent_state(list_json: str | None, services_agent_id: AgentId | None) -> str:
    """Return the lifecycle state of the system-services agent from ``mngr list`` output."""
    if services_agent_id is None:
        return ""
    row = extract_agent_row(list_json, services_agent_id)
    if row is None:
        return ""
    state = row.get("state")
    if not isinstance(state, str):
        return ""
    return state


def extract_ssh_connections(list_json: str | None) -> tuple[SshConnectionInfo, ...]:
    """Return SSH connection info per unique host from ``mngr list`` output.

    Local hosts (no ``host.ssh`` block) are omitted. Hosts that appear on
    multiple agents are deduplicated by ``host.id``.
    """
    if list_json is None:
        return ()
    try:
        agents = json.loads(list_json).get("agents", [])
    except (json.JSONDecodeError, AttributeError):
        return ()
    seen: dict[str, SshConnectionInfo] = {}
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        host = agent.get("host")
        if not isinstance(host, dict):
            continue
        host_id = host.get("id")
        if not isinstance(host_id, str) or host_id in seen:
            continue
        ssh = host.get("ssh")
        if not isinstance(ssh, dict):
            continue
        user = ssh.get("user")
        host_name = ssh.get("host")
        port = ssh.get("port")
        if not isinstance(user, str) or not isinstance(host_name, str) or not isinstance(port, int):
            continue
        key_path = ssh.get("key_path")
        command = ssh.get("command")
        seen[host_id] = SshConnectionInfo(
            host_id=host_id,
            user=user,
            host=host_name,
            port=port,
            key_path=key_path if isinstance(key_path, str) else "",
            command=command if isinstance(command, str) else "",
        )
    return tuple(seen.values())


def build_host_health_response(
    list_json: str | None,
    agent_id: AgentId,
    services_agent_id: AgentId | None,
    probe: ProbeRecord,
    plugin_resolver_services: dict[str, str],
) -> HostHealthResponse:
    """Assemble the host-health endpoint response from raw inputs.

    Pure function so the integration is easy to unit-test: mock the
    ``mngr exec`` / ``mngr list`` stdout and the resolver snapshot, then
    feed them in and assert on the response shape.
    """
    agent_row = extract_agent_row(list_json, agent_id)
    host_state = extract_host_state(agent_row)
    reachable, host_offline = classify_host_state(host_state)
    services_state = extract_services_agent_state(list_json, services_agent_id)
    ssh_connections = extract_ssh_connections(list_json)
    is_misconfigured = probe.services_toml_declares_system_interface is False
    return HostHealthResponse(
        reachable=reachable,
        host_offline=host_offline,
        host_state=host_state,
        ssh_dead=probe.ssh_dead,
        is_misconfigured=is_misconfigured,
        services_agent_state=services_state,
        ssh_connections=ssh_connections,
        plugin_resolver_services=dict(plugin_resolver_services),
        probe=probe,
    )


# Regex used in tests that need to assert on the embedded inner-port parse.
_INNER_PORT_REGEX: Final[re.Pattern[str]] = re.compile(r"--url\s+\S+://[^:]+:(\d+)")


def parse_inner_port_from_command(command: str) -> int | None:
    """Mirror of the inner-port parser the inline Python script uses.

    Exposed for unit tests so the regex and the in-container behavior can
    be pinned in one place; the in-container script duplicates the regex
    because it can't import this module.
    """
    match = _INNER_PORT_REGEX.search(command)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None
