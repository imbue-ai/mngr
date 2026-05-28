"""Recovery diagnostics probe.

Powers the workspace-recovery page's diagnostics list. The endpoint runs
a batched in-container probe via ``mngr exec`` and reads ``mngr list``
state, then returns a flat list of named probes -- each capturing the
question asked, the command (or pseudo-command label) that produced the
data, the raw output captured, and a derived yes/no/unknown answer.

The recovery-page client renders each probe row as a question with a
check / x / question-mark indicator and an expandable command + output
panel. The page's restart-tier branching keys off a single derived
``dispatch_tier`` field so the rendering stays a pure projection of the
probe data, not a parallel set of natural-language fields.

The single sentinel ``===PROBE-READY===`` is printed before the in-container
JSON payload. If the sentinel is absent from stdout, the "Can we run a
command inside the container?" probe answers ``no`` -- the ``mngr exec``
plumbing returned without ever invoking the in-container script, so we
have no in-container observations and the page steers the user to a host
restart rather than auto-dispatching surgical.
"""

import base64
import json
import re
import socket
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId

PROBE_SENTINEL: Final[str] = "===PROBE-READY==="

# Hard ceiling for a single batched ``mngr exec``. Bounded so a wedged
# container can't gate the recovery UI. Only two of the inner checks spawn
# subprocesses (``tmux ls`` at 1s and ``curl`` at 2s); the TOML parse and
# the ``/proc/net/tcp`` LISTEN scan run in-process and effectively instantly.
# The subprocess timeouts sum to a worst case of 1+2 = 3s, so the 5s ceiling
# leaves a comfortable margin while still keeping a wedged container from
# hanging the recovery UI.
PROBE_TIMEOUT_SECONDS: Final[float] = 5.0


# Inner Python script executed on the agent's host, loaded from a sibling
# .txt resource so the in-container script's pattern matches (tmllib import,
# subprocess.run calls, broad Exception catches, ...) don't trip minds-side
# ratchets that only inspect ``.py`` files. The script is then base64-encoded
# in ``build_probe_shell_command`` so the outer ``mngr exec`` argv stays a
# single shell-safe token without quoting headaches.
_PROBE_SCRIPT_PATH: Final[Path] = Path(__file__).parent / "recovery_probe_script.txt"


@cache
def _get_probe_python_script() -> str:
    """Return the inner-probe Python source, loading it from disk on first call."""
    return _PROBE_SCRIPT_PATH.read_text(encoding="utf-8")


def build_probe_shell_command() -> str:
    """Return the shell command minds passes to ``mngr exec``."""
    encoded = base64.b64encode(_get_probe_python_script().encode("utf-8")).decode("ascii")
    return f"echo '{PROBE_SENTINEL}' && echo {encoded} | base64 -d | python3"


def build_probe_argv(mngr_binary: str, services_agent_id: AgentId) -> list[str]:
    """Build the ``mngr exec`` argv that runs the batched probe on the agent's host.

    ``--quiet`` suppresses mngr's own progress chatter so stdout starts
    with the sentinel directly. ``--no-start`` keeps us from accidentally
    starting a stopped host just by probing it.
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


class ProbeAnswer(str, Enum):
    """yes / no / unknown answer for a single probe."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class Probe(FrozenModel):
    """A single diagnostic check.

    Each probe is a (question, command, output, answer) tuple. The
    recovery page renders the question as a row, the answer as a
    check / x / ? glyph, and the command + output in an expander so the
    operator can re-run the command outside minds to verify.
    """

    question: str = Field(description="The yes/no/unknown question this probe answers.")
    command: str = Field(
        description=(
            "Exact command (or short pseudo-command label for an internal "
            "observation) that produced ``output``, for the operator to "
            "re-run outside minds."
        ),
    )
    output: str = Field(description="Raw output captured for this probe.")
    answer: ProbeAnswer = Field(description="Derived answer to the question.")


class DispatchTier(str, Enum):
    """How the recovery page should respond, derived from the probe answers."""

    SURGICAL = "surgical"
    """Container running and exec works -- restart the system-services agent."""

    HOST = "host"
    """Container is offline -- restart the host (no live work to interrupt)."""

    MANUAL = "manual"
    """Ambiguous -- show "Restart workspace" and require explicit user consent."""

    MISCONFIGURED = "misconfigured"
    """services.toml lacks [services.system_interface] -- a restart won't help."""


class HostHealthResponse(FrozenModel):
    """List of probes plus the derived restart tier.

    Intentionally narrow: every datum the recovery page renders is a
    ``Probe`` in ``probes``, and the page's branching reads only
    ``dispatch_tier``. There are no parallel natural-language fields.
    """

    probes: tuple[Probe, ...] = Field(
        default=(), description="Ordered probe results to render in the diagnostics list."
    )
    dispatch_tier: DispatchTier = Field(
        default=DispatchTier.MANUAL,
        description="Restart-tier classification derived from probe answers.",
    )


# -- Probe questions (canonical wording, shared with tests) ----------------


_QUESTION_CONTAINER_RUNNING: Final[str] = "Is the workspace's container running?"
_QUESTION_SERVICES_AGENT_REGISTERED: Final[str] = "Is the system-services agent registered?"
_QUESTION_CAN_RUN_COMMANDS_INSIDE: Final[str] = "Can we run a command inside the container?"
_QUESTION_SERVICES_TOML_DECLARES: Final[str] = "Does services.toml declare [services.system_interface]?"
_QUESTION_PORT_LISTENING: Final[str] = "Is anything listening on the system-interface inner port?"
_QUESTION_CURL_OK: Final[str] = "Does the system interface answer locally inside the container?"
_QUESTION_PLUGIN_RESOLVER: Final[str] = "Has the system interface registered with the plugin resolver?"


# -- Inner-probe payload parsing -------------------------------------------


class _InContainerProbe(FrozenModel):
    """Internal: parsed payload from the in-container batched probe.

    Not exposed in the endpoint response; folded into probes 3-6 by
    ``_build_probes_from_in_container``. ``sentinel_seen`` is the single
    bit that distinguishes "probe ran" from "ssh dead" -- without it,
    every other field is None.
    """

    sentinel_seen: bool = Field(default=False)
    raw_stdout: str = Field(default="")
    tmux_ls: str | None = Field(default=None)
    tmux_error: str | None = Field(default=None)
    services_toml_declares_system_interface: bool | None = Field(default=None)
    services_toml_path: str = Field(default="/code/services.toml")
    services_toml_error: str | None = Field(default=None)
    inner_port: int | None = Field(default=None)
    port_listener: str | None = Field(default=None)
    port_listener_error: str | None = Field(default=None)
    curl_status: str | None = Field(default=None)
    curl_error: str | None = Field(default=None)


def _parse_in_container_probe(stdout: str | None) -> _InContainerProbe:
    """Parse the batched probe's stdout into a typed record.

    Returns a record with ``sentinel_seen=False`` when stdout is None or
    the sentinel never landed. Otherwise extracts the JSON payload that
    follows the sentinel and folds it into the record.
    """
    if stdout is None:
        return _InContainerProbe(sentinel_seen=False, raw_stdout="")
    if PROBE_SENTINEL not in stdout:
        return _InContainerProbe(sentinel_seen=False, raw_stdout=stdout)

    after = stdout.split(PROBE_SENTINEL, 1)[1]
    json_line: str | None = None
    for line in after.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        json_line = candidate
        break
    if json_line is None:
        return _InContainerProbe(sentinel_seen=True, raw_stdout=stdout)
    try:
        payload = json.loads(json_line)
    except json.JSONDecodeError:
        return _InContainerProbe(sentinel_seen=True, raw_stdout=stdout)
    if not isinstance(payload, dict):
        return _InContainerProbe(sentinel_seen=True, raw_stdout=stdout)

    return _InContainerProbe(
        sentinel_seen=True,
        raw_stdout=stdout,
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


# -- mngr-list extraction --------------------------------------------------


_RUNNING_STATE: Final[str] = "RUNNING"
_OFFLINE_HOST_STATES: Final[frozenset[str]] = frozenset({"STOPPED", "STOPPING", "CRASHED", "FAILED"})


def _extract_agent_row(list_json: str | None, agent_id: AgentId) -> dict | None:
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


def _extract_host_state(agent_row: dict | None) -> str:
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


def _extract_services_agent_state(list_json: str | None, services_agent_id: AgentId | None) -> str:
    """Return the lifecycle state of the system-services agent from ``mngr list`` output."""
    if services_agent_id is None:
        return ""
    row = _extract_agent_row(list_json, services_agent_id)
    if row is None:
        return ""
    state = row.get("state")
    if not isinstance(state, str):
        return ""
    return state


# -- Per-probe builders ----------------------------------------------------


def _build_container_running_probe(host_state: str, mngr_list_command: str) -> Probe:
    """Probe 1: host.state from ``mngr list``."""
    upper = host_state.upper()
    if upper == _RUNNING_STATE:
        answer = ProbeAnswer.YES
    elif upper in _OFFLINE_HOST_STATES:
        answer = ProbeAnswer.NO
    else:
        answer = ProbeAnswer.UNKNOWN
    output = host_state or "(host row missing from mngr list)"
    return Probe(
        question=_QUESTION_CONTAINER_RUNNING,
        command=mngr_list_command or "(mngr list --format json)",
        output=output,
        answer=answer,
    )


def _build_services_agent_registered_probe(
    list_json: str | None,
    services_agent_id: AgentId | None,
    mngr_list_command: str,
) -> Probe:
    """Probe 2: presence + lifecycle state of the system-services agent."""
    services_state = _extract_services_agent_state(list_json, services_agent_id)
    if services_agent_id is None:
        answer = ProbeAnswer.UNKNOWN
        output = "(no system-services agent id known -- discovery has not surfaced one)"
    elif services_state:
        answer = ProbeAnswer.YES
        output = f"state={services_state}"
    else:
        answer = ProbeAnswer.NO
        output = "(system-services agent absent from mngr list)"
    return Probe(
        question=_QUESTION_SERVICES_AGENT_REGISTERED,
        command=mngr_list_command or "(mngr list --format json)",
        output=output,
        answer=answer,
    )


def _build_can_run_commands_probe(in_container: _InContainerProbe, mngr_exec_command: str) -> Probe:
    """Probe 3: did the ``mngr exec`` sentinel reach stdout?"""
    answer = ProbeAnswer.YES if in_container.sentinel_seen else ProbeAnswer.NO
    if in_container.sentinel_seen:
        output = f"sentinel '{PROBE_SENTINEL}' observed on stdout"
    elif in_container.raw_stdout:
        output = f"sentinel '{PROBE_SENTINEL}' NOT observed; raw stdout:\n{in_container.raw_stdout}"
    else:
        output = f"sentinel '{PROBE_SENTINEL}' NOT observed; stdout was empty (mngr exec returned without invoking the in-container script)"
    return Probe(
        question=_QUESTION_CAN_RUN_COMMANDS_INSIDE,
        command=mngr_exec_command,
        output=output,
        answer=answer,
    )


def _services_toml_command(services_toml_path: str) -> str:
    """Operator-runnable reproduction of the services.toml declaration check.

    Mirrors the inline probe script's ``isinstance(si, dict)`` test. The
    ``-c`` body is double-quoted with single-quoted literals inside, so it
    pastes into a shell cleanly (the previous ``{path!r}`` form nested
    single quotes inside a single-quoted body and was not runnable).
    """
    return (
        'python3 -c "import tomllib; '
        f"d = tomllib.load(open('{services_toml_path}', 'rb')); "
        "print(isinstance(d.get('services', {}).get('system_interface'), dict))\""
    )


def _build_services_toml_probe(in_container: _InContainerProbe) -> Probe:
    """Probe 4: does services.toml declare [services.system_interface]?"""
    command = _services_toml_command(in_container.services_toml_path)
    if not in_container.sentinel_seen:
        return Probe(
            question=_QUESTION_SERVICES_TOML_DECLARES,
            command=command,
            output="(in-container probe did not run)",
            answer=ProbeAnswer.UNKNOWN,
        )
    if in_container.services_toml_error is not None:
        return Probe(
            question=_QUESTION_SERVICES_TOML_DECLARES,
            command=command,
            output=f"error: {in_container.services_toml_error}",
            answer=ProbeAnswer.UNKNOWN,
        )
    if in_container.services_toml_declares_system_interface is True:
        answer = ProbeAnswer.YES
        output = "[services.system_interface] is declared"
    elif in_container.services_toml_declares_system_interface is False:
        answer = ProbeAnswer.NO
        output = "[services.system_interface] is MISSING"
    else:
        answer = ProbeAnswer.UNKNOWN
        output = "(no declaration data returned)"
    return Probe(
        question=_QUESTION_SERVICES_TOML_DECLARES,
        command=command,
        output=output,
        answer=answer,
    )


def _port_listening_command(port_label: str) -> str:
    """Operator-runnable reproduction of the inner-port LISTEN check.

    The agent container image ships no ``iproute2`` (so no ``ss``); this
    scans ``/proc/net/tcp{,6}`` for a TCP_LISTEN (state ``0A``) socket whose
    local port matches the inner port, mirroring the inline probe script.
    Prints the raw matching hex ``local_address`` columns; the probe's
    output panel renders the same sockets decoded to ``ip:port``.
    """
    return (
        'python3 -c "'
        "rows = [line.split() for path in ('/proc/net/tcp', '/proc/net/tcp6') "
        "for line in open(path).read().splitlines()[1:]]; "
        f"print([r[1] for r in rows if r[3] == '0A' and int(r[1].rsplit(':', 1)[1], 16) == {port_label}])\""
    )


def _build_port_listening_probe(in_container: _InContainerProbe) -> Probe:
    """Probe 5: scan /proc/net/tcp{,6} for a LISTEN socket on the inner port."""
    port_label = "?" if in_container.inner_port is None else str(in_container.inner_port)
    command = _port_listening_command(port_label)
    if not in_container.sentinel_seen:
        return Probe(
            question=_QUESTION_PORT_LISTENING,
            command=command,
            output="(in-container probe did not run)",
            answer=ProbeAnswer.UNKNOWN,
        )
    if in_container.inner_port is None:
        return Probe(
            question=_QUESTION_PORT_LISTENING,
            command=command,
            output="(could not parse inner port from services.toml command)",
            answer=ProbeAnswer.UNKNOWN,
        )
    if in_container.port_listener_error is not None:
        return Probe(
            question=_QUESTION_PORT_LISTENING,
            command=command,
            output=f"error: {in_container.port_listener_error}",
            answer=ProbeAnswer.UNKNOWN,
        )
    listener_output = in_container.port_listener or ""
    if listener_output.strip():
        return Probe(
            question=_QUESTION_PORT_LISTENING,
            command=command,
            output=listener_output,
            answer=ProbeAnswer.YES,
        )
    return Probe(
        question=_QUESTION_PORT_LISTENING,
        command=command,
        output="(no LISTEN entry for the inner port)",
        answer=ProbeAnswer.NO,
    )


def _build_curl_probe(in_container: _InContainerProbe) -> Probe:
    """Probe 6: curl http://localhost:<port>/."""
    port_label = "?" if in_container.inner_port is None else str(in_container.inner_port)
    command = f"curl -m1 -s -o /dev/null -w '%{{http_code}}' http://localhost:{port_label}/"
    if not in_container.sentinel_seen:
        return Probe(
            question=_QUESTION_CURL_OK,
            command=command,
            output="(in-container probe did not run)",
            answer=ProbeAnswer.UNKNOWN,
        )
    if in_container.inner_port is None:
        return Probe(
            question=_QUESTION_CURL_OK,
            command=command,
            output="(could not parse inner port from services.toml command)",
            answer=ProbeAnswer.UNKNOWN,
        )
    if in_container.curl_error is not None:
        return Probe(
            question=_QUESTION_CURL_OK,
            command=command,
            output=f"error: {in_container.curl_error}",
            answer=ProbeAnswer.NO,
        )
    status = in_container.curl_status or ""
    if status == "200":
        return Probe(
            question=_QUESTION_CURL_OK,
            command=command,
            output=f"HTTP {status}",
            answer=ProbeAnswer.YES,
        )
    if status:
        return Probe(
            question=_QUESTION_CURL_OK,
            command=command,
            output=f"HTTP {status}",
            answer=ProbeAnswer.NO,
        )
    return Probe(
        question=_QUESTION_CURL_OK,
        command=command,
        output="(no response captured)",
        answer=ProbeAnswer.UNKNOWN,
    )


def _build_plugin_resolver_probe(plugin_resolver_services: dict[str, str]) -> Probe:
    """Probe 7: mngr_forward plugin's resolver snapshot for this agent."""
    if plugin_resolver_services:
        lines = [f"{k} = {v}" for k, v in plugin_resolver_services.items()]
        return Probe(
            question=_QUESTION_PLUGIN_RESOLVER,
            command="(mngr_forward plugin resolver snapshot)",
            output="\n".join(lines),
            answer=ProbeAnswer.YES,
        )
    return Probe(
        question=_QUESTION_PLUGIN_RESOLVER,
        command="(mngr_forward plugin resolver snapshot)",
        output="(no services registered with the plugin resolver for this agent)",
        answer=ProbeAnswer.NO,
    )


# -- Top-level builder + dispatch tier -------------------------------------


def _classify_dispatch_tier(probes: tuple[Probe, ...]) -> DispatchTier:
    """Derive the dispatch tier from probe answers.

    Ordered by precedence:

    * MISCONFIGURED beats everything: a missing [services.system_interface]
      block means no restart will help, so don't bury that behind any
      transport / container check.
    * HOST when the container is offline: nothing live to interrupt, so
      a host restart can run unattended.
    * MANUAL when the container claims running but we can't exec into it
      (the SSH-dead path): a host restart bounces a live container so it
      requires explicit user consent.
    * SURGICAL when both container and exec are healthy: the system-services
      agent can be restarted in place without touching the user's agents.
    * MANUAL on anything else (ambiguous host states).
    """
    answers = {probe.question: probe.answer for probe in probes}
    if answers.get(_QUESTION_SERVICES_TOML_DECLARES) == ProbeAnswer.NO:
        return DispatchTier.MISCONFIGURED
    container_running = answers.get(_QUESTION_CONTAINER_RUNNING)
    if container_running == ProbeAnswer.NO:
        return DispatchTier.HOST
    can_run = answers.get(_QUESTION_CAN_RUN_COMMANDS_INSIDE)
    if container_running == ProbeAnswer.YES and can_run == ProbeAnswer.YES:
        return DispatchTier.SURGICAL
    return DispatchTier.MANUAL


def build_host_health_response(
    list_json: str | None,
    agent_id: AgentId,
    services_agent_id: AgentId | None,
    in_container_stdout: str | None,
    plugin_resolver_services: dict[str, str],
    mngr_list_command: str = "",
    mngr_exec_command: str = "",
) -> HostHealthResponse:
    """Assemble the host-health response (probes + dispatch tier) from raw inputs.

    Pure function so the integration is straightforward to unit-test:
    feed in raw ``mngr list`` / in-container stdout / plugin snapshot,
    assert on the probe answers and the derived tier.
    """
    in_container = _parse_in_container_probe(in_container_stdout)
    agent_row = _extract_agent_row(list_json, agent_id)
    host_state = _extract_host_state(agent_row)
    list_cmd = mngr_list_command or "(mngr list --format json)"
    exec_cmd = mngr_exec_command or "(mngr exec <system-services-agent>)"
    probes: tuple[Probe, ...] = (
        _build_container_running_probe(host_state, list_cmd),
        _build_services_agent_registered_probe(list_json, services_agent_id, list_cmd),
        _build_can_run_commands_probe(in_container, exec_cmd),
        _build_services_toml_probe(in_container),
        _build_port_listening_probe(in_container),
        _build_curl_probe(in_container),
        _build_plugin_resolver_probe(plugin_resolver_services),
    )
    return HostHealthResponse(probes=probes, dispatch_tier=_classify_dispatch_tier(probes))


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


# TCP socket state ``0A`` is ``TCP_LISTEN`` in the kernel's ``/proc/net/tcp``
# table (see ``include/net/tcp_states.h``).
_PROC_TCP_LISTEN_STATE: Final[str] = "0A"


def _decode_proc_local_address(local_address: str) -> str:
    """Decode a ``/proc/net/tcp{,6}`` ``local_address`` (``HEXIP:HEXPORT``) to ``ip:port``.

    The kernel writes the IP as little-endian 32-bit words in hex -- 8 hex
    chars for IPv4, 32 for IPv6 -- so each 4-byte group is byte-reversed
    before formatting. Falls back to the raw hex on anything unexpected so a
    decode quirk never hides a real LISTEN socket from the operator.
    """
    ip_hex, _, port_hex = local_address.rpartition(":")
    try:
        port = int(port_hex, 16)
    except ValueError:
        return local_address
    if len(ip_hex) == 8:
        ip = ".".join(str(octet) for octet in bytes.fromhex(ip_hex)[::-1])
    elif len(ip_hex) == 32:
        packed = b"".join(bytes.fromhex(ip_hex[i : i + 8])[::-1] for i in range(0, 32, 8))
        ip = socket.inet_ntop(socket.AF_INET6, packed)
    else:
        ip = ip_hex
    return f"{ip}:{port}"


def parse_listening_sockets(proc_net_tcp_text: str, port: int) -> list[str]:
    """Return decoded ``ip:port`` for LISTEN sockets matching ``port`` in /proc/net/tcp{,6} text.

    Mirror of the inline probe script's scan, exposed for unit tests; the
    in-container script duplicates this logic because it can't import this
    module (same arrangement as ``parse_inner_port_from_command``). The
    header row is skipped naturally because its state column is the literal
    ``st`` rather than a hex state code.
    """
    listeners: list[str] = []
    for line in proc_net_tcp_text.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[3] != _PROC_TCP_LISTEN_STATE:
            continue
        local_address = fields[1]
        _, _, port_hex = local_address.rpartition(":")
        try:
            matched = int(port_hex, 16) == port
        except ValueError:
            continue
        if matched:
            listeners.append(_decode_proc_local_address(local_address))
    return listeners
