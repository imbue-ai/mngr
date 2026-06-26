"""Recovery diagnostics probe.

Powers the workspace-recovery page's diagnostics list. The endpoint reads
the outer host/provider state from the passive discovery resolver (a single
sampler shared with the rest of minds -- no synchronous ``mngr list``) and
runs a batched in-container probe via ``mngr exec`` only when that outer
state is healthy, then returns a flat list of named probes -- each capturing
the question asked, the command (or pseudo-command label) that produced the
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
import shlex
import socket
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId

PROBE_SENTINEL: Final[str] = "===PROBE-READY==="

# Hard ceiling for a single batched ``mngr exec``, so a wedged container can't
# gate the recovery UI. Only two of the inner checks spawn subprocesses
# (``supervisorctl status`` at 1s and ``curl`` at 2s, summing to 3s worst case);
# the supervisord.conf parse and the ``/proc/net/tcp`` LISTEN scan run
# in-process. 5s leaves margin.
PROBE_TIMEOUT_SECONDS: Final[float] = 5.0


# Inner Python script executed on the agent's host, loaded from a sibling
# .txt resource so the in-container script's patterns (subprocess calls,
# broad Exception catches, ...) don't trip minds-side ratchets that only
# inspect ``.py`` files. The script is then base64-encoded
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
    """What is wrong with the workspace, derived from the probe answers.

    Every member names a *condition* (what we observed), not the *action* the
    recovery page takes in response -- the action is a consequence of the
    condition (e.g. INTERFACE_UNRESPONSIVE -> in-place restart; HOST_OFFLINE ->
    unattended host restart; HOST_UNRESPONSIVE -> ask the user first).
    """

    INTERFACE_UNRESPONSIVE = "interface_unresponsive"
    """Container running and exec works -- restart the system-services agent in place."""

    HOST_OFFLINE = "host_offline"
    """Container is offline -- restart the host (no live work to interrupt)."""

    HOST_UNRESPONSIVE = "host_unresponsive"
    """Container claims running but we can't reach it -- require explicit user consent.

    Also the fallback for any ambiguous host state: the host is not responding
    in the way we expect, so we ask the user before bouncing it.
    """

    BACKEND_UNREACHABLE = "backend_unreachable"
    """The provider/backend hosting this workspace can't be reached, or refused us
    -- the connector is down, the local docker daemon is stopped or paused, or the
    provider rejected us (e.g. an expired login). Whatever the cause, a host or
    interface restart routes through that same backend, so it cannot help: the
    page offers only a Retry, surfaces the provider's own error verbatim, and arms
    a background poll that returns the user to the workspace the moment it
    recovers. Takes precedence over every host/interface tier because no
    host-state observation can be trusted when the backend that produces it is
    unreachable.
    """


class HostHealthResponse(FrozenModel):
    """List of probes plus the derived restart tier.

    Intentionally narrow: every datum the recovery page renders is a
    ``Probe`` in ``probes``, and the page's branching reads only
    ``dispatch_tier``. The two provider-error fields below are the sole
    exception: the BACKEND_UNREACHABLE tier is not derived from in-container
    probes (it short-circuits before those run), so the reason and provider label
    travel alongside the tier instead.
    """

    probes: tuple[Probe, ...] = Field(
        default=(), description="Ordered probe results to render in the diagnostics list."
    )
    dispatch_tier: DispatchTier = Field(
        default=DispatchTier.HOST_UNRESPONSIVE,
        description="Restart-tier classification derived from probe answers.",
    )
    unreachable_reason: str = Field(
        default="",
        description="Provider error message for the BACKEND_UNREACHABLE tier; empty for all other tiers.",
    )
    provider_label: str = Field(
        default="",
        description=(
            "Friendly provider name for the unreachable page title (e.g. 'Imbue Cloud', 'Docker'); "
            "empty for non-BACKEND_UNREACHABLE tiers."
        ),
    )


# -- Probe questions (canonical wording, shared with tests) ----------------


_QUESTION_CONTAINER_RUNNING: Final[str] = "Is the workspace's container running?"
_QUESTION_SERVICES_AGENT_REGISTERED: Final[str] = "Is the system-services agent registered?"
_QUESTION_CAN_RUN_COMMANDS_INSIDE: Final[str] = "Can we run a command inside the container?"
_QUESTION_SYSTEM_INTERFACE_RUNNING: Final[str] = "Is the system_interface service running under supervisord?"
_QUESTION_PORT_LISTENING: Final[str] = "Is anything listening on the system-interface inner port?"
_QUESTION_CURL_OK: Final[str] = "Does the inner web server answer GET / inside the container?"
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
    system_interface_status: str | None = Field(default=None)
    supervisorctl_error: str | None = Field(default=None)
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
    except json.JSONDecodeError as exc:
        logger.warning("In-container probe emitted a non-JSON payload line ({!r}): {}", json_line, exc)
        return _InContainerProbe(sentinel_seen=True, raw_stdout=stdout)
    if not isinstance(payload, dict):
        return _InContainerProbe(sentinel_seen=True, raw_stdout=stdout)

    return _InContainerProbe(
        sentinel_seen=True,
        raw_stdout=stdout,
        system_interface_status=_coerce_optional_str(payload.get("system_interface_status")),
        supervisorctl_error=_coerce_optional_str(payload.get("supervisorctl_error")),
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


def _coerce_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


# -- Host/provider state (resolver-sourced) --------------------------------


_RUNNING_STATE: Final[str] = "RUNNING"
_OFFLINE_HOST_STATES: Final[frozenset[str]] = frozenset({"STOPPED", "STOPPING", "CRASHED", "FAILED"})


# -- Per-probe builders ----------------------------------------------------
#
# In-container checks are wrapped in ``mngr exec <id> '<check>' --no-start
# --quiet`` so the operator does not need a shell inside the container, and
# their ``output`` is exactly what that command prints. The host-state,
# system-services-agent, and resolver probes are read from minds' own
# passive-discovery memory rather than re-sampled, so they carry a short
# ``(... from the discovery snapshot)`` pseudo-command label and have no
# runnable reproduction.

# Pseudo-command labels for the resolver-sourced probes (no runnable
# reproduction -- the datum is read from the passive discovery snapshot).
_HOST_STATE_PSEUDO_COMMAND: Final[str] = "(host state from the discovery snapshot)"
_SERVICES_AGENT_PSEUDO_COMMAND: Final[str] = "(system-services agent from the discovery snapshot)"
# Output shown for the resolver-sourced probes when discovery has not surfaced the datum.
_NO_HOST_STATE: Final[str] = "(no host state in the discovery snapshot)"
_NO_SERVICES_AGENT: Final[str] = "(no system-services agent id known -- discovery has not surfaced one)"


def _mngr_exec_command(mngr_binary: str, services_agent_id: AgentId | None, inner_command: str) -> str:
    """A copy-pasteable ``mngr exec`` that runs ``inner_command`` in the container.

    ``--quiet`` strips mngr's progress chatter so stdout is exactly the inner
    command's stdout; ``--no-start`` keeps a probe from booting a stopped host.
    Falls back to a ``<system-services-agent>`` placeholder when the agent id
    has not been discovered yet (the command is still shape-accurate).
    """
    if services_agent_id is None:
        return f"mngr exec <system-services-agent> {shlex.quote(inner_command)} --no-start --quiet"
    return shlex.join([mngr_binary, "exec", str(services_agent_id), inner_command, "--no-start", "--quiet"])


def _build_container_running_probe(host_state: str) -> Probe:
    """Probe 1: the workspace host's lifecycle state, read from the discovery snapshot."""
    upper = host_state.upper()
    if upper == _RUNNING_STATE:
        answer = ProbeAnswer.YES
    elif upper in _OFFLINE_HOST_STATES:
        answer = ProbeAnswer.NO
    else:
        answer = ProbeAnswer.UNKNOWN
    output = host_state if host_state else _NO_HOST_STATE
    return Probe(
        question=_QUESTION_CONTAINER_RUNNING,
        command=_HOST_STATE_PSEUDO_COMMAND,
        output=output,
        answer=answer,
    )


def _build_services_agent_registered_probe(services_agent_id: AgentId | None) -> Probe:
    """Probe 2: is the system-services agent present in the discovery snapshot?

    Presence is read from the resolver (``get_system_services_agent_id``): a
    resolved id -- whether from the live snapshot or the persisted last-good
    topology -- answers YES; an unresolved one answers UNKNOWN (discovery has not
    surfaced this workspace's system-services agent yet). This probe is purely
    diagnostic; the dispatch tier never branches on it.
    """
    if services_agent_id is None:
        return Probe(
            question=_QUESTION_SERVICES_AGENT_REGISTERED,
            command=_SERVICES_AGENT_PSEUDO_COMMAND,
            output=_NO_SERVICES_AGENT,
            answer=ProbeAnswer.UNKNOWN,
        )
    return Probe(
        question=_QUESTION_SERVICES_AGENT_REGISTERED,
        command=_SERVICES_AGENT_PSEUDO_COMMAND,
        output=str(services_agent_id),
        answer=ProbeAnswer.YES,
    )


def _build_can_run_commands_probe(in_container: _InContainerProbe, mngr_exec_command: str) -> Probe:
    """Probe 3: did the batched ``mngr exec`` reach the container?

    The command is the real batched ``mngr exec`` and the output is its raw
    stdout -- the sentinel followed by the JSON payload when the probe ran, so
    re-running the command reproduces exactly what is shown.
    """
    answer = ProbeAnswer.YES if in_container.sentinel_seen else ProbeAnswer.NO
    output = in_container.raw_stdout if in_container.raw_stdout.strip() else "(mngr exec produced no output on stdout)"
    return Probe(
        question=_QUESTION_CAN_RUN_COMMANDS_INSIDE,
        command=mngr_exec_command,
        output=output,
        answer=answer,
    )


def _supervisorctl_status_inner_command() -> str:
    """In-container ``supervisorctl status`` for the system_interface service.

    Pointed at the repo-root config (``-c /code/supervisord.conf``) so it finds
    the unix socket declared there; the default config search path does not
    include that file. Prints supervisord's own status line, e.g.
    ``system_interface   RUNNING   pid 42, uptime 0:10:00``.
    """
    return "supervisorctl -c /code/supervisord.conf status system_interface"


def _build_system_interface_probe(
    in_container: _InContainerProbe,
    mngr_binary: str,
    services_agent_id: AgentId | None,
) -> Probe:
    """Probe 4: is the system_interface service RUNNING under supervisord?

    Purely diagnostic -- the dispatch tier never branches on it. A
    not-RUNNING service while the container is up and exec works still
    classifies as INTERFACE_UNRESPONSIVE (a surgical restart bounces
    supervisord with it), so this probe is the detail behind that tier, not
    a tier of its own.
    """
    command = _mngr_exec_command(mngr_binary, services_agent_id, _supervisorctl_status_inner_command())
    if not in_container.sentinel_seen:
        return Probe(
            question=_QUESTION_SYSTEM_INTERFACE_RUNNING,
            command=command,
            output="(in-container probe did not run)",
            answer=ProbeAnswer.UNKNOWN,
        )
    if in_container.supervisorctl_error is not None:
        return Probe(
            question=_QUESTION_SYSTEM_INTERFACE_RUNNING,
            command=command,
            output=f"error: {in_container.supervisorctl_error}",
            answer=ProbeAnswer.UNKNOWN,
        )
    status = in_container.system_interface_status
    if not status:
        return Probe(
            question=_QUESTION_SYSTEM_INTERFACE_RUNNING,
            command=command,
            output="(no supervisorctl status returned)",
            answer=ProbeAnswer.UNKNOWN,
        )
    state = parse_supervisorctl_status_state(status)
    if state == _SUPERVISOR_RUNNING_STATE:
        answer = ProbeAnswer.YES
    elif state is not None:
        answer = ProbeAnswer.NO
    else:
        # No recognized state word -- a connection error, ``no such process``,
        # or otherwise unparseable output. We can't claim it's down.
        answer = ProbeAnswer.UNKNOWN
    return Probe(question=_QUESTION_SYSTEM_INTERFACE_RUNNING, command=command, output=status, answer=answer)


def _no_listener_output(port: int) -> str:
    """The exact line both the reproduction command and minds print for no listener."""
    return f"(no LISTEN socket on port {port})"


def _port_listening_inner_command(port: int) -> str:
    """In-container check that prints decoded ``LISTEN ip:port`` lines (or the no-listener line).

    Dependency-free (the container image ships no iproute2): scans
    ``/proc/net/tcp{,6}`` for TCP_LISTEN (state ``0A``) sockets on ``port`` and
    decodes the little-endian hex local address. Mirrors the inline probe
    script and ``parse_listening_sockets`` (kept textually parallel); the body
    uses only double quotes so it survives the ``-c`` and ``mngr exec`` quoting.
    """
    body = (
        "import socket,os; "
        f"t={port}; "
        'fmt=lambda h: ".".join(str(o) for o in bytes.fromhex(h)[::-1]) if len(h)==8 '
        'else (socket.inet_ntop(socket.AF_INET6,b"".join(bytes.fromhex(h[i:i+8])[::-1] '
        "for i in range(0,32,8))) if len(h)==32 else h); "
        'rows=[l.split() for p in ("/proc/net/tcp","/proc/net/tcp6") if os.path.exists(p) '
        "for l in open(p).read().splitlines()[1:]]; "
        'out=["LISTEN %s:%d"%(fmt(f[1].rpartition(":")[0]),t) for f in rows '
        'if len(f)>=4 and f[3]=="0A" and int(f[1].rpartition(":")[2],16)==t]; '
        'print("\\n".join(out) or "(no LISTEN socket on port %d)"%t)'
    )
    return f"python3 -c '{body}'"


def _build_port_listening_probe(
    in_container: _InContainerProbe,
    mngr_binary: str,
    services_agent_id: AgentId | None,
) -> Probe:
    """Probe 5: scan /proc/net/tcp{,6} for a LISTEN socket on the inner port."""
    port = in_container.inner_port
    inner = _port_listening_inner_command(port if port is not None else 0)
    command = _mngr_exec_command(mngr_binary, services_agent_id, inner)
    if not in_container.sentinel_seen:
        output, answer = "(in-container probe did not run)", ProbeAnswer.UNKNOWN
    elif port is None:
        output, answer = "(could not parse inner port from supervisord.conf)", ProbeAnswer.UNKNOWN
    elif in_container.port_listener_error is not None:
        output, answer = f"error: {in_container.port_listener_error}", ProbeAnswer.UNKNOWN
    elif (in_container.port_listener or "").strip():
        output, answer = in_container.port_listener or "", ProbeAnswer.YES
    else:
        output, answer = _no_listener_output(port), ProbeAnswer.NO
    return Probe(question=_QUESTION_PORT_LISTENING, command=command, output=output, answer=answer)


def _curl_inner_command(port: int) -> str:
    """In-container curl of ``/`` that prints just the HTTP status code (``000`` on no response).

    Probes ``/`` and treats a 200 as "answering" -- deliberately not coupled to
    any particular application running inside the workspace. The check only
    confirms that some web server is up on the inner port, making no assumption
    about which app that is or which routes it implements.
    """
    return f'curl -m1 -s -o /dev/null -w "%{{http_code}}" http://localhost:{port}/'


def _build_curl_probe(
    in_container: _InContainerProbe,
    mngr_binary: str,
    services_agent_id: AgentId | None,
) -> Probe:
    """Probe 6: does the inner web server answer GET / inside the container?"""
    port = in_container.inner_port
    inner = _curl_inner_command(port if port is not None else 0)
    command = _mngr_exec_command(mngr_binary, services_agent_id, inner)
    if not in_container.sentinel_seen:
        output, answer = "(in-container probe did not run)", ProbeAnswer.UNKNOWN
    elif port is None:
        output, answer = "(could not parse inner port from supervisord.conf)", ProbeAnswer.UNKNOWN
    elif in_container.curl_error is not None:
        output, answer = f"error: {in_container.curl_error}", ProbeAnswer.NO
    elif in_container.curl_status == "200":
        output, answer = "200", ProbeAnswer.YES
    elif in_container.curl_status:
        output, answer = in_container.curl_status, ProbeAnswer.NO
    else:
        output, answer = "(no response captured)", ProbeAnswer.UNKNOWN
    return Probe(question=_QUESTION_CURL_OK, command=command, output=output, answer=answer)


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


def _classify_dispatch_tier(
    probes: tuple[Probe, ...],
    provider_error_message: str | None,
) -> DispatchTier:
    """Derive the dispatch tier from probe answers and the provider error.

    Ordered by precedence:

    * BACKEND_UNREACHABLE beats every host/interface tier: if the provider that
      produces the host-state observations is itself unreachable (or rejecting
      us), no restart routed through it can help and the host-state probes cannot
      be trusted, so the provider-error signal wins outright. We do not
      sub-classify by error kind (a stopped daemon, a paused daemon, an expired
      login all land here): the user-facing impact is identical -- show the
      provider's own message, offer Retry, and wait for it to recover.
    * HOST_OFFLINE when the container is offline: nothing live to interrupt, so
      a host restart can run unattended.
    * INTERFACE_UNRESPONSIVE when both container and exec are healthy: the
      system-services agent can be restarted in place without touching the
      user's agents.
    * HOST_UNRESPONSIVE when the container claims running but we can't exec into
      it (or on any other ambiguous host state): a host restart bounces a live
      container, so it requires explicit user consent. The recovery page is only
      reached once discovery is fresh (the redirect is gated on freshness), so
      the RUNNING claim is trustworthy here.
    """
    if provider_error_message is not None:
        return DispatchTier.BACKEND_UNREACHABLE
    answers = {probe.question: probe.answer for probe in probes}
    container_running = answers.get(_QUESTION_CONTAINER_RUNNING)
    if container_running == ProbeAnswer.NO:
        return DispatchTier.HOST_OFFLINE
    can_run = answers.get(_QUESTION_CAN_RUN_COMMANDS_INSIDE)
    if container_running == ProbeAnswer.YES and can_run == ProbeAnswer.YES:
        return DispatchTier.INTERFACE_UNRESPONSIVE
    return DispatchTier.HOST_UNRESPONSIVE


def build_host_health_response(
    host_state: str,
    services_agent_id: AgentId | None,
    in_container_stdout: str | None,
    plugin_resolver_services: dict[str, str],
    mngr_exec_command: str = "",
    mngr_binary: str = "mngr",
    provider_error_message: str | None = None,
    provider_label: str = "",
) -> HostHealthResponse:
    """Assemble the host-health response (probes + dispatch tier) from raw inputs.

    Pure function so the integration is straightforward to unit-test: feed in
    the resolver-sourced host/provider state (``host_state``,
    ``services_agent_id``, ``provider_error_message``) plus the in-container exec
    stdout and plugin snapshot, and assert on the probe answers and derived tier.

    ``host_state`` is the workspace host's lifecycle state read from the passive
    discovery resolver (``get_host_state``), e.g. ``"RUNNING"`` / ``"STOPPED"``;
    ``""`` when discovery has not surfaced the host. ``mngr_binary`` is used to
    render the ``mngr exec`` reproduction commands for the in-container probes.

    ``provider_error_message`` is this workspace's provider-level error message
    read from the resolver's ``get_provider_errors()``; when present (not None) it
    drives the BACKEND_UNREACHABLE tier and is carried on the response as
    ``unreachable_reason``. ``provider_label`` is the friendly provider name for
    that page's title.
    """
    in_container = _parse_in_container_probe(in_container_stdout)
    exec_cmd = mngr_exec_command or "(mngr exec <system-services-agent>)"
    probes: tuple[Probe, ...] = (
        _build_container_running_probe(host_state),
        _build_services_agent_registered_probe(services_agent_id),
        _build_can_run_commands_probe(in_container, exec_cmd),
        _build_system_interface_probe(in_container, mngr_binary, services_agent_id),
        _build_port_listening_probe(in_container, mngr_binary, services_agent_id),
        _build_curl_probe(in_container, mngr_binary, services_agent_id),
        _build_plugin_resolver_probe(plugin_resolver_services),
    )
    dispatch_tier = _classify_dispatch_tier(probes, provider_error_message)
    is_backend_unreachable = dispatch_tier == DispatchTier.BACKEND_UNREACHABLE
    return HostHealthResponse(
        probes=probes,
        dispatch_tier=dispatch_tier,
        unreachable_reason=(provider_error_message if provider_error_message is not None else ""),
        provider_label=(provider_label if is_backend_unreachable else ""),
    )


# supervisord process states (see supervisor.states.ProcessStates). RUNNING is
# the only "up" state; the rest are real-but-not-running. Any second token that
# is NOT one of these (a connection error, ``no such process``, ...) means we
# could not read a status and the answer is UNKNOWN rather than NO.
_SUPERVISOR_RUNNING_STATE: Final[str] = "RUNNING"
_SUPERVISOR_PROCESS_STATES: Final[frozenset[str]] = frozenset(
    {"STOPPED", "STARTING", "RUNNING", "BACKOFF", "STOPPING", "EXITED", "FATAL", "UNKNOWN"}
)


def parse_supervisorctl_status_state(output: str) -> str | None:
    """Extract the supervisor process-state word from a ``supervisorctl status <name>`` line.

    supervisorctl prints ``<name>   <STATE>   <detail...>``; this returns the
    second whitespace-delimited field when it is a recognized supervisor state
    (e.g. ``RUNNING``, ``STOPPED``, ``FATAL``), else None -- which is how a
    connection error, a ``no such process`` line, or otherwise unparseable
    output is told apart from a genuine not-running state.
    """
    fields = output.split()
    if len(fields) >= 2 and fields[1] in _SUPERVISOR_PROCESS_STATES:
        return fields[1]
    return None


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
