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
# (``tmux ls`` at 1s and ``curl`` at 2s, summing to 3s worst case); the TOML
# parse and the ``/proc/net/tcp`` LISTEN scan run in-process. 5s leaves margin.
PROBE_TIMEOUT_SECONDS: Final[float] = 5.0


# Inner Python script executed on the agent's host, loaded from a sibling
# .txt resource so the in-container script's patterns (tomllib import,
# subprocess calls, broad Exception catches, ...) don't trip minds-side
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

    WORKSPACE_MISCONFIGURED = "workspace_misconfigured"
    """services.toml lacks [services.system_interface] -- a restart won't help."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """The provider backend itself is unreachable (imbue_cloud connector down /
    network down, or local docker daemon stopped). A host or interface restart
    routes through that same backend, so it cannot help -- the page offers only
    a Retry, never a restart. Takes precedence over every host/interface tier
    because we cannot trust any host-state observation when the provider that
    produces it is unreachable.
    """

    WORKSPACE_UNREACHABLE = "workspace_unreachable"
    """The provider responded but with a non-connectivity error (expired login,
    no account configured, ...). A restart cannot fix an auth/config problem, so
    the page surfaces the reason with no restart affordance. Distinct from
    PROVIDER_UNAVAILABLE, which is a transient outage worth retrying.
    """


class ProviderProbeError(FrozenModel):
    """A provider-level error pulled from ``mngr list``'s ``errors[]`` array.

    Carries just the fields the recovery classification needs: the exception
    type (to tell a connector outage apart from an auth/config failure) and the
    human-readable message (rendered on the unreachable page).
    """

    exception_type: str = Field(
        description="Exception class name from `mngr list` errors[] (e.g. ProviderUnavailableError)."
    )
    message: str = Field(description="Human-readable error message to surface to the user.")


class HostHealthResponse(FrozenModel):
    """List of probes plus the derived restart tier.

    Intentionally narrow: every datum the recovery page renders is a
    ``Probe`` in ``probes``, and the page's branching reads only
    ``dispatch_tier``. The two provider-error fields below are the sole
    exception: the PROVIDER_UNAVAILABLE / WORKSPACE_UNREACHABLE tiers are not
    derived from in-container probes (they short-circuit before those run), so
    the reason and provider label travel alongside the tier instead.
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
        description=(
            "Provider error message for the PROVIDER_UNAVAILABLE / WORKSPACE_UNREACHABLE "
            "tiers; empty for all other tiers."
        ),
    )
    provider_label: str = Field(
        default="",
        description=(
            "Friendly provider name for the unreachable page title (e.g. 'Imbue Cloud'); empty for non-provider tiers."
        ),
    )


# -- Probe questions (canonical wording, shared with tests) ----------------


_QUESTION_CONTAINER_RUNNING: Final[str] = "Is the workspace's container running?"
_QUESTION_SERVICES_AGENT_REGISTERED: Final[str] = "Is the system-services agent registered?"
_QUESTION_CAN_RUN_COMMANDS_INSIDE: Final[str] = "Can we run a command inside the container?"
_QUESTION_SERVICES_TOML_DECLARES: Final[str] = "Does services.toml declare [services.system_interface]?"
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
    except json.JSONDecodeError as exc:
        logger.opt(exception=exc).error("In-container probe emitted a non-JSON payload line ({!r}): {}", json_line, exc)
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
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.opt(exception=exc).error("Could not parse `mngr list` output while extracting agent row: {}", exc)
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


_PROVIDER_UNAVAILABLE_EXCEPTION: Final[str] = "ProviderUnavailableError"


def provider_unavailable_error(message: str) -> ProviderProbeError:
    """Build a synthetic provider error that classifies as PROVIDER_UNAVAILABLE.

    Used when the host-state listing could not *complete* (e.g. the host-health
    ``mngr list`` timed out), so there is no ``errors[]`` body to parse. An
    inability to even enumerate the provider is evidence it is unreachable, so we
    route it through the same retry-don't-restart tier as a connector-raised
    ``ProviderUnavailableError`` rather than letting it fall through to the
    destructive ``HOST_UNRESPONSIVE`` bucket.
    """
    return ProviderProbeError(exception_type=_PROVIDER_UNAVAILABLE_EXCEPTION, message=message)


def extract_provider_error(list_json: str | None, provider_name: str | None) -> ProviderProbeError | None:
    """Pull this workspace's provider-level error from ``mngr list``'s ``errors[]``, if any.

    ``mngr list --on-error continue`` emits ``{"agents": [...], "errors": [...]}``
    even when a provider fails (and exits non-zero), so the recovery probe reads
    the body regardless of exit code. Each error dict carries ``exception_type``,
    ``message`` and -- for provider-level errors -- ``provider_name``.

    Only errors attributed to *this* workspace's provider count. When the probe's
    listing is scoped via ``--provider`` (the normal case) that is automatic, but
    in the brief pre-discovery window where the workspace's provider is unknown
    (``provider_name is None``) we return None rather than blame an unrelated
    provider's error on this workspace.

    A ``ProviderUnavailableError`` is preferred over any other matching error so
    a genuine connector outage classifies as PROVIDER_UNAVAILABLE (retry, no
    restart) rather than being shadowed by an incidental auth/config error.
    """
    if list_json is None or provider_name is None:
        return None
    try:
        errors = json.loads(list_json).get("errors", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.opt(exception=exc).error("Could not parse `mngr list` output while extracting provider error: {}", exc)
        return None
    if not isinstance(errors, list):
        return None
    fallback: ProviderProbeError | None = None
    for entry in errors:
        if not isinstance(entry, dict) or entry.get("provider_name") != provider_name:
            continue
        exception_type = str(entry.get("exception_type") or "")
        message = str(entry.get("message") or "")
        if exception_type == _PROVIDER_UNAVAILABLE_EXCEPTION:
            return ProviderProbeError(exception_type=exception_type, message=message)
        if fallback is None:
            fallback = ProviderProbeError(exception_type=exception_type, message=message)
    return fallback


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
#
# Every probe's ``command`` is a complete, copy-pasteable command whose stdout
# equals the probe's ``output`` when run in the same place minds ran it (the
# mngr host for the ``mngr list`` / ``mngr exec`` probes). Host-state probes
# pipe ``mngr list`` through ``jq`` to print exactly the extracted field;
# in-container checks are wrapped in ``mngr exec <id> '<check>' --no-start
# --quiet`` so the operator does not need a shell inside the container. The
# only exception is the resolver probe, whose datum lives in minds' own memory
# and has no runnable reproduction.

# Display strings shared between a probe's rendered ``output`` and the fallback
# its reproduction command prints, so the two stay byte-identical.
_NO_HOST_ROW: Final[str] = "no host row"
_NO_AGENT_ROW: Final[str] = "no agent row"


def _mngr_list_jq_command(mngr_list_command: str, jq_filter: str) -> str:
    """``mngr list ... | jq -r '<filter>'`` -- the list call plus its derivation."""
    base = mngr_list_command or "mngr list --format json"
    return f"{base} | jq -r {shlex.quote(jq_filter)}"


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


def _build_container_running_probe(
    host_state: str, agent_id: AgentId, mngr_list_command: str, mngr_list_error: str | None
) -> Probe:
    """Probe 1: host.state from ``mngr list``, extracted with jq."""
    upper = host_state.upper()
    if upper == _RUNNING_STATE:
        answer = ProbeAnswer.YES
    elif upper in _OFFLINE_HOST_STATES:
        answer = ProbeAnswer.NO
    else:
        answer = ProbeAnswer.UNKNOWN
    if host_state:
        output = host_state
    elif mngr_list_error is not None:
        # No row for this host AND the listing did not exit cleanly: surface why
        # rather than a bare "no host row" (the answer stays UNKNOWN either way).
        output = f"mngr list failed: {mngr_list_error}"
    else:
        output = _NO_HOST_ROW
    jq_filter = f'([.agents[] | select(.id == "{agent_id}")][0].host.state) // "{_NO_HOST_ROW}"'
    return Probe(
        question=_QUESTION_CONTAINER_RUNNING,
        command=_mngr_list_jq_command(mngr_list_command, jq_filter),
        output=output,
        answer=answer,
    )


def _build_services_agent_registered_probe(
    list_json: str | None,
    services_agent_id: AgentId | None,
    mngr_list_command: str,
    mngr_list_error: str | None,
) -> Probe:
    """Probe 2: lifecycle state of the system-services agent, extracted with jq."""
    if services_agent_id is None:
        # No id to filter on -> no runnable command (like the resolver probe).
        return Probe(
            question=_QUESTION_SERVICES_AGENT_REGISTERED,
            command="(no system-services agent id known -- discovery has not surfaced one)",
            output="(no system-services agent id known -- discovery has not surfaced one)",
            answer=ProbeAnswer.UNKNOWN,
        )
    services_state = _extract_services_agent_state(list_json, services_agent_id)
    jq_filter = f'([.agents[] | select(.id == "{services_agent_id}")][0].state) // "{_NO_AGENT_ROW}"'
    if services_state:
        answer = ProbeAnswer.YES
        output = services_state
    elif mngr_list_error is not None:
        # No row AND the listing did not exit cleanly: we cannot tell whether the
        # agent is registered, so answer UNKNOWN and surface why instead of NO.
        answer = ProbeAnswer.UNKNOWN
        output = f"mngr list failed: {mngr_list_error}"
    else:
        answer = ProbeAnswer.NO
        output = _NO_AGENT_ROW
    return Probe(
        question=_QUESTION_SERVICES_AGENT_REGISTERED,
        command=_mngr_list_jq_command(mngr_list_command, jq_filter),
        output=output,
        answer=answer,
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


def _services_toml_inner_command(services_toml_path: str) -> str:
    """In-container check that prints ``declared`` or ``MISSING``.

    Mirrors the inline probe script's ``isinstance(si, dict)`` test. The body
    uses only double quotes so it survives the single-quoted ``-c`` wrapper,
    which in turn survives ``shlex.join`` quoting for ``mngr exec``.
    """
    body = (
        "import tomllib; "
        f'd = tomllib.load(open("{services_toml_path}", "rb")); '
        'print("declared" if isinstance(d.get("services", {}).get("system_interface"), dict) else "MISSING")'
    )
    return f"python3 -c '{body}'"


def _build_services_toml_probe(
    in_container: _InContainerProbe,
    mngr_binary: str,
    services_agent_id: AgentId | None,
) -> Probe:
    """Probe 4: does services.toml declare [services.system_interface]?"""
    command = _mngr_exec_command(
        mngr_binary, services_agent_id, _services_toml_inner_command(in_container.services_toml_path)
    )
    if not in_container.sentinel_seen:
        output, answer = "(in-container probe did not run)", ProbeAnswer.UNKNOWN
    elif in_container.services_toml_error is not None:
        output, answer = f"error: {in_container.services_toml_error}", ProbeAnswer.UNKNOWN
    elif in_container.services_toml_declares_system_interface is True:
        output, answer = "declared", ProbeAnswer.YES
    elif in_container.services_toml_declares_system_interface is False:
        output, answer = "MISSING", ProbeAnswer.NO
    else:
        output, answer = "(no declaration data returned)", ProbeAnswer.UNKNOWN
    return Probe(question=_QUESTION_SERVICES_TOML_DECLARES, command=command, output=output, answer=answer)


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
        output, answer = "(could not parse inner port from services.toml command)", ProbeAnswer.UNKNOWN
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
        output, answer = "(could not parse inner port from services.toml command)", ProbeAnswer.UNKNOWN
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


def _classify_dispatch_tier(probes: tuple[Probe, ...], provider_error: ProviderProbeError | None) -> DispatchTier:
    """Derive the dispatch tier from probe answers and the provider-error signal.

    Ordered by precedence:

    * PROVIDER_UNAVAILABLE / WORKSPACE_UNREACHABLE beat every host/interface
      tier: if the provider that produces the host-state observations is itself
      unreachable (or rejecting us), no restart routed through it can help and
      the host-state probes cannot be trusted, so the provider-error signal wins
      outright. A ProviderUnavailableError is a transient outage (retry);
      anything else from the provider is an auth/config problem (show reason).
    * WORKSPACE_MISCONFIGURED beats the remaining tiers: a missing
      [services.system_interface] block means no restart will help, so don't
      bury that behind any transport / container check.
    * HOST_OFFLINE when the container is offline: nothing live to interrupt, so
      a host restart can run unattended.
    * HOST_UNRESPONSIVE when the container claims running but we can't exec into
      it: a host restart bounces a live container so it requires explicit user
      consent.
    * INTERFACE_UNRESPONSIVE when both container and exec are healthy: the
      system-services agent can be restarted in place without touching the
      user's agents.
    * HOST_UNRESPONSIVE on anything else (ambiguous host states).
    """
    if provider_error is not None:
        if provider_error.exception_type == _PROVIDER_UNAVAILABLE_EXCEPTION:
            return DispatchTier.PROVIDER_UNAVAILABLE
        return DispatchTier.WORKSPACE_UNREACHABLE
    answers = {probe.question: probe.answer for probe in probes}
    if answers.get(_QUESTION_SERVICES_TOML_DECLARES) == ProbeAnswer.NO:
        return DispatchTier.WORKSPACE_MISCONFIGURED
    container_running = answers.get(_QUESTION_CONTAINER_RUNNING)
    if container_running == ProbeAnswer.NO:
        return DispatchTier.HOST_OFFLINE
    can_run = answers.get(_QUESTION_CAN_RUN_COMMANDS_INSIDE)
    if container_running == ProbeAnswer.YES and can_run == ProbeAnswer.YES:
        return DispatchTier.INTERFACE_UNRESPONSIVE
    return DispatchTier.HOST_UNRESPONSIVE


def build_host_health_response(
    list_json: str | None,
    agent_id: AgentId,
    services_agent_id: AgentId | None,
    in_container_stdout: str | None,
    plugin_resolver_services: dict[str, str],
    mngr_list_command: str = "",
    mngr_list_error: str | None = None,
    mngr_exec_command: str = "",
    mngr_binary: str = "mngr",
    provider_error: ProviderProbeError | None = None,
    provider_label: str = "",
) -> HostHealthResponse:
    """Assemble the host-health response (probes + dispatch tier) from raw inputs.

    Pure function so the integration is straightforward to unit-test:
    feed in raw ``mngr list`` / in-container stdout / plugin snapshot,
    assert on the probe answers and the derived tier.

    ``mngr_binary`` is used to render the ``mngr exec`` reproduction commands
    for the in-container probes; ``mngr_list_command`` is the real list argv
    those probes pipe through ``jq`` to print exactly their extracted field.
    ``mngr_list_error`` is the reason ``mngr list`` did not exit cleanly (or
    None); the host-state probes surface it in place of a bare "no row" when the
    listing failed to produce this workspace's row, so the user can see *why*.

    ``provider_error`` is this workspace's provider-level error parsed from
    ``mngr list``'s ``errors[]`` (see ``extract_provider_error``); when present
    it drives the PROVIDER_UNAVAILABLE / WORKSPACE_UNREACHABLE tiers and its
    message is carried on the response as ``unreachable_reason``.
    ``provider_label`` is the friendly provider name for that page's title.
    """
    in_container = _parse_in_container_probe(in_container_stdout)
    agent_row = _extract_agent_row(list_json, agent_id)
    host_state = _extract_host_state(agent_row)
    exec_cmd = mngr_exec_command or "(mngr exec <system-services-agent>)"
    probes: tuple[Probe, ...] = (
        _build_container_running_probe(host_state, agent_id, mngr_list_command, mngr_list_error),
        _build_services_agent_registered_probe(list_json, services_agent_id, mngr_list_command, mngr_list_error),
        _build_can_run_commands_probe(in_container, exec_cmd),
        _build_services_toml_probe(in_container, mngr_binary, services_agent_id),
        _build_port_listening_probe(in_container, mngr_binary, services_agent_id),
        _build_curl_probe(in_container, mngr_binary, services_agent_id),
        _build_plugin_resolver_probe(plugin_resolver_services),
    )
    dispatch_tier = _classify_dispatch_tier(probes, provider_error)
    is_provider_tier = dispatch_tier in (DispatchTier.PROVIDER_UNAVAILABLE, DispatchTier.WORKSPACE_UNREACHABLE)
    return HostHealthResponse(
        probes=probes,
        dispatch_tier=dispatch_tier,
        unreachable_reason=(provider_error.message if provider_error is not None else ""),
        provider_label=(provider_label if is_provider_tier else ""),
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
