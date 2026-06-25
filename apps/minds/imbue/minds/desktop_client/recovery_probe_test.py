"""Unit tests for the recovery-diagnostics probe builder + dispatch tier."""

import json
import shlex

from imbue.minds.desktop_client.recovery_probe import DispatchTier
from imbue.minds.desktop_client.recovery_probe import HostHealthResponse
from imbue.minds.desktop_client.recovery_probe import PROBE_SENTINEL
from imbue.minds.desktop_client.recovery_probe import Probe
from imbue.minds.desktop_client.recovery_probe import ProbeAnswer
from imbue.minds.desktop_client.recovery_probe import build_host_health_response
from imbue.minds.desktop_client.recovery_probe import build_probe_argv
from imbue.minds.desktop_client.recovery_probe import parse_inner_port_from_command
from imbue.minds.desktop_client.recovery_probe import parse_listening_sockets
from imbue.minds.desktop_client.recovery_probe import parse_supervisorctl_status_state
from imbue.mngr.primitives import AgentId

_SERVICES_AGENT_ID: AgentId = AgentId("agent-" + "0" * 31 + "2")


def _probe_stdout(payload: dict[str, object]) -> str:
    """Build a probe-stdout string with the sentinel and a JSON payload line."""
    return PROBE_SENTINEL + "\n" + json.dumps(payload) + "\n"


def _response(
    *,
    host_state: str = "RUNNING",
    services_agent_id: AgentId | None = _SERVICES_AGENT_ID,
    in_container_stdout: str | None = None,
    plugin_resolver_services: dict[str, str] | None = None,
    provider_error_message: str | None = None,
    provider_label: str = "",
    mngr_exec_command: str = "",
) -> HostHealthResponse:
    """Call ``build_host_health_response`` with resolver-sourced defaults.

    Defaults to a healthy RUNNING host so each test only has to vary the inputs
    it exercises.
    """
    return build_host_health_response(
        host_state=host_state,
        services_agent_id=services_agent_id,
        in_container_stdout=in_container_stdout,
        plugin_resolver_services=plugin_resolver_services or {},
        provider_error_message=provider_error_message,
        provider_label=provider_label,
        mngr_exec_command=mngr_exec_command,
    )


def _answer(response: HostHealthResponse, question_fragment: str) -> ProbeAnswer:
    for probe in response.probes:
        if question_fragment in probe.question:
            return probe.answer
    raise AssertionError(
        f"no probe matched fragment {question_fragment!r}; got {[p.question for p in response.probes]}"
    )


def _probe_for(response: HostHealthResponse, question_fragment: str) -> Probe:
    for probe in response.probes:
        if question_fragment in probe.question:
            return probe
    raise AssertionError(f"no probe matched fragment {question_fragment!r}")


# --- inner-port regex -----------------------------------------------------


def test_parse_inner_port_matches_forward_port_command() -> None:
    cmd = "python3 scripts/forward_port.py --url http://localhost:8000 --name system_interface && system-interface"
    assert parse_inner_port_from_command(cmd) == 8000


def test_parse_inner_port_returns_none_when_command_lacks_url_flag() -> None:
    assert parse_inner_port_from_command("system-interface") is None


# --- supervisorctl status parsing -----------------------------------------


def test_parse_supervisorctl_status_state_reads_running() -> None:
    line = "system_interface                 RUNNING   pid 42, uptime 0:10:00"
    assert parse_supervisorctl_status_state(line) == "RUNNING"


def test_parse_supervisorctl_status_state_reads_non_running_state() -> None:
    assert parse_supervisorctl_status_state("system_interface   FATAL     Exited too quickly") == "FATAL"


def test_parse_supervisorctl_status_state_none_on_connection_error() -> None:
    """A socket/connection error is not a process state -> None (UNKNOWN), not NO."""
    assert parse_supervisorctl_status_state("unix:///var/run/supervisor.sock refused connection") is None
    assert parse_supervisorctl_status_state("system_interface: ERROR (no such process)") is None


# --- build_probe_argv -----------------------------------------------------


def test_build_probe_argv_targets_services_agent_with_timeout_and_no_start() -> None:
    argv = build_probe_argv("/usr/local/bin/mngr", _SERVICES_AGENT_ID)
    assert argv[:3] == ["/usr/local/bin/mngr", "exec", str(_SERVICES_AGENT_ID)]
    assert "--no-start" in argv
    assert "--timeout" in argv
    assert "--quiet" in argv


# --- per-probe answers ----------------------------------------------------


def test_container_running_probe_says_yes_when_host_state_is_running() -> None:
    response = _response(host_state="RUNNING", in_container_stdout=_probe_stdout({}))
    assert _answer(response, "container running") == ProbeAnswer.YES


def test_container_running_probe_says_no_when_host_state_is_stopped() -> None:
    response = _response(host_state="STOPPED")
    assert _answer(response, "container running") == ProbeAnswer.NO


def test_container_running_probe_is_unknown_for_ambiguous_host_state() -> None:
    response = _response(host_state="STARTING")
    assert _answer(response, "container running") == ProbeAnswer.UNKNOWN


def test_container_running_probe_is_unknown_when_host_state_absent() -> None:
    """No host in the discovery snapshot -> UNKNOWN, with an explanatory output."""
    response = _response(host_state="")
    probe = _probe_for(response, "container running")
    assert probe.answer == ProbeAnswer.UNKNOWN
    assert probe.output == "(no host state in the discovery snapshot)"


def test_services_agent_registered_probe_yes_when_id_resolved() -> None:
    response = _response(services_agent_id=_SERVICES_AGENT_ID)
    probe = _probe_for(response, "system-services agent registered")
    assert probe.answer == ProbeAnswer.YES
    assert probe.output == str(_SERVICES_AGENT_ID)


def test_services_agent_registered_probe_unknown_when_id_not_known() -> None:
    response = _response(services_agent_id=None)
    assert _answer(response, "system-services agent registered") == ProbeAnswer.UNKNOWN


def test_can_run_commands_probe_no_when_sentinel_absent() -> None:
    response = _response(in_container_stdout=None)
    assert _answer(response, "run a command") == ProbeAnswer.NO


def test_can_run_commands_probe_yes_when_sentinel_present() -> None:
    response = _response(in_container_stdout=_probe_stdout({"system_interface_status": "system_interface RUNNING"}))
    assert _answer(response, "run a command") == ProbeAnswer.YES


def test_system_interface_probe_yes_when_running() -> None:
    response = _response(
        in_container_stdout=_probe_stdout(
            {"system_interface_status": "system_interface   RUNNING   pid 42, uptime 0:10:00"}
        )
    )
    assert _answer(response, "running under supervisord") == ProbeAnswer.YES


def test_system_interface_probe_no_when_not_running() -> None:
    response = _response(
        in_container_stdout=_probe_stdout(
            {"system_interface_status": "system_interface   FATAL     Exited too quickly"}
        )
    )
    assert _answer(response, "running under supervisord") == ProbeAnswer.NO


def test_system_interface_probe_unknown_when_supervisorctl_errored() -> None:
    """A supervisorctl error (couldn't reach supervisord) is UNKNOWN, not NO."""
    response = _response(
        in_container_stdout=_probe_stdout({"supervisorctl_error": "FileNotFoundError('supervisorctl')"})
    )
    assert _answer(response, "running under supervisord") == ProbeAnswer.UNKNOWN


def test_system_interface_probe_unknown_when_status_unparseable() -> None:
    """A connection-error line carries no state word -> UNKNOWN, not NO."""
    response = _response(
        in_container_stdout=_probe_stdout(
            {"system_interface_status": "unix:///var/run/supervisor.sock refused connection"}
        )
    )
    assert _answer(response, "running under supervisord") == ProbeAnswer.UNKNOWN


def test_system_interface_probe_unknown_when_probe_did_not_run() -> None:
    response = _response(in_container_stdout=None)
    assert _answer(response, "running under supervisord") == ProbeAnswer.UNKNOWN


def test_curl_probe_yes_for_200() -> None:
    response = _response(in_container_stdout=_probe_stdout({"inner_port": 8000, "curl_status": "200"}))
    assert _answer(response, "GET /") == ProbeAnswer.YES


def test_curl_probe_no_for_non_200() -> None:
    response = _response(in_container_stdout=_probe_stdout({"inner_port": 8000, "curl_status": "502"}))
    assert _answer(response, "GET /") == ProbeAnswer.NO


def test_port_listener_probe_yes_when_listener_present() -> None:
    response = _response(
        in_container_stdout=_probe_stdout(
            {
                "inner_port": 8000,
                "port_listener": "LISTEN 0.0.0.0:8000\nLISTEN ::1:8000",
            }
        )
    )
    assert _answer(response, "listening on the system-interface inner port") == ProbeAnswer.YES


def test_plugin_resolver_probe_yes_when_services_registered() -> None:
    response = _response(
        in_container_stdout=_probe_stdout({"system_interface_status": "system_interface RUNNING"}),
        plugin_resolver_services={"system_interface": "http://127.0.0.1:9100"},
    )
    probe = _probe_for(response, "registered with the plugin resolver")
    assert probe.answer == ProbeAnswer.YES
    assert "system_interface" in probe.output


def test_plugin_resolver_probe_no_when_no_services_registered() -> None:
    response = _response(in_container_stdout=_probe_stdout({"system_interface_status": "system_interface RUNNING"}))
    assert _answer(response, "registered with the plugin resolver") == ProbeAnswer.NO


# --- dispatch_tier classification ----------------------------------------


def test_dispatch_tier_interface_unresponsive_when_container_running_and_exec_works() -> None:
    response = _response(
        host_state="RUNNING",
        in_container_stdout=_probe_stdout({"inner_port": 8000, "curl_status": "502"}),
    )
    assert response.dispatch_tier == DispatchTier.INTERFACE_UNRESPONSIVE


def test_dispatch_tier_interface_unresponsive_when_system_interface_not_running() -> None:
    """A not-RUNNING system_interface (container up, exec works) is still INTERFACE_UNRESPONSIVE.

    The supervisorctl probe is diagnostic detail, not its own tier: a surgical
    restart bounces supervisord (and the system_interface with it), which is the
    correct recovery, so the dispatch tier must not branch on probe 4.
    """
    response = _response(
        host_state="RUNNING",
        in_container_stdout=_probe_stdout(
            {"system_interface_status": "system_interface   FATAL     Exited too quickly", "inner_port": 8000}
        ),
    )
    assert response.dispatch_tier == DispatchTier.INTERFACE_UNRESPONSIVE


def test_dispatch_tier_host_offline_when_container_is_offline() -> None:
    response = _response(host_state="STOPPED")
    assert response.dispatch_tier == DispatchTier.HOST_OFFLINE


def test_dispatch_tier_host_unresponsive_when_container_running_but_exec_dead() -> None:
    """SSH-dead path: host claims RUNNING but exec failed -> consent-gated host restart.

    The recovery page is only reached once discovery is fresh (the redirect is
    gated on freshness upstream), so the RUNNING claim is trustworthy here and
    HOST_UNRESPONSIVE is returned unconditionally.
    """
    response = _response(host_state="RUNNING", in_container_stdout=None)
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


def test_dispatch_tier_host_unresponsive_for_ambiguous_host_state() -> None:
    response = _response(host_state="STARTING", in_container_stdout=None)
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


# --- provider reachability tiers -----------------------------------------


def test_dispatch_tier_backend_unreachable_beats_host_state() -> None:
    """A provider error classifies as BACKEND_UNREACHABLE regardless of host state.

    The provider that produces the host-state observations is itself unreachable,
    so its error wins over any (now-untrustworthy) host probe.
    """
    response = _response(
        host_state="RUNNING",
        provider_error_message="Docker Desktop is manually paused.",
        provider_label="Docker",
    )
    assert response.dispatch_tier == DispatchTier.BACKEND_UNREACHABLE
    assert response.unreachable_reason == "Docker Desktop is manually paused."
    assert response.provider_label == "Docker"


def test_dispatch_tier_backend_unreachable_for_any_provider_error_kind() -> None:
    """Any provider error -- connectivity outage or auth/config rejection -- is the
    same BACKEND_UNREACHABLE tier; we no longer sub-classify by error kind because
    the user-facing impact (show the error, retry, wait) is identical."""
    response = _response(
        host_state="RUNNING",
        provider_error_message="Your login expired.",
        provider_label="Imbue Cloud",
    )
    assert response.dispatch_tier == DispatchTier.BACKEND_UNREACHABLE
    assert response.unreachable_reason == "Your login expired."
    assert response.provider_label == "Imbue Cloud"


# --- shape sanity --------------------------------------------------------


def test_every_probe_has_a_command_and_an_output() -> None:
    """No probe should render with an empty command label or empty output text."""
    response = _response(
        host_state="RUNNING",
        in_container_stdout=None,
        mngr_exec_command="/usr/local/bin/mngr exec ... probe-script",
    )
    assert response.probes
    for probe in response.probes:
        assert probe.command, f"probe {probe.question!r} rendered with no command"
        assert probe.output, f"probe {probe.question!r} rendered with no output"


def _inner_python_body(command: str) -> str | None:
    """Extract a ``python3 -c`` body from a probe command, unwrapping ``mngr exec``.

    The in-container probe commands render as
    ``mngr exec <id> 'python3 -c '\\''<body>'\\''' --no-start --quiet``; this
    peels off the ``mngr exec`` wrapper and the inner ``-c`` to return the
    body. Returns None for commands that are not python reproductions (the curl
    one and the resolver-sourced pseudo-command labels).
    """
    tokens = shlex.split(command)
    if len(tokens) >= 4 and tokens[0].endswith("mngr") and tokens[1] == "exec":
        inner_tokens = shlex.split(tokens[3])
    else:
        inner_tokens = tokens
    if len(inner_tokens) >= 3 and inner_tokens[0] == "python3" and inner_tokens[1] == "-c":
        return inner_tokens[2]
    return None


def test_python_probe_commands_are_well_formed_and_runnable() -> None:
    """Every ``python3 -c`` probe command must unwrap and compile as Python.

    Guards against quote-nesting bugs (the inner-port LISTEN scan nests single
    quotes inside a single-quoted ``-c`` body) and against the ``mngr exec``
    wrapper mangling the inner script. compile() validates the inner body's
    syntax without executing it.
    """
    response = _response(in_container_stdout=_probe_stdout({"inner_port": 8000, "curl_status": "200"}))
    checked: list[tuple[str, str]] = []
    for p in response.probes:
        body = _inner_python_body(p.command)
        if body is not None:
            checked.append((p.question, body))
    assert checked, "expected at least one python3 -c probe command"
    for question, body in checked:
        compile(body, f"<probe:{question}>", "exec")


# --- command / output alignment -------------------------------------------
#
# Each probe's output is exactly what its command (run where minds ran it) would
# print. The host-state and system-services-agent probes are read from the passive
# discovery snapshot, so they carry a pseudo-command label and their output is the raw resolver datum;
# the in-container probes carry a runnable ``mngr exec`` reproduction.


def _healthy_probe_stdout(**overrides: object) -> str:
    payload: dict[str, object] = {
        "system_interface_status": "system_interface   RUNNING   pid 42, uptime 0:10:00",
        "inner_port": 8000,
        "curl_status": "200",
        "port_listener": "LISTEN 0.0.0.0:8000",
    }
    payload.update(overrides)
    return _probe_stdout(payload)


def test_container_running_probe_reads_state_from_discovery_snapshot() -> None:
    probe = _probe_for(_response(host_state="RUNNING"), "container running")
    # No runnable reproduction -- the datum is read from the passive snapshot.
    assert probe.command == "(host state from the discovery snapshot)"
    assert probe.output == "RUNNING"


def test_services_agent_probe_outputs_the_resolved_agent_id() -> None:
    probe = _probe_for(_response(services_agent_id=_SERVICES_AGENT_ID), "system-services agent")
    assert probe.command == "(system-services agent from the discovery snapshot)"
    assert probe.output == str(_SERVICES_AGENT_ID)


def test_can_run_commands_output_is_the_raw_exec_stdout() -> None:
    stdout = _probe_stdout({"system_interface_status": "system_interface RUNNING"})
    response = _response(in_container_stdout=stdout, mngr_exec_command="mngr exec agent-x 'echo hi' --quiet")
    probe = _probe_for(response, "run a command")
    assert probe.command == "mngr exec agent-x 'echo hi' --quiet"
    # the verbatim stdout the command produced
    assert probe.output == stdout


def test_system_interface_command_is_supervisorctl_and_output_is_status_line() -> None:
    status = "system_interface   RUNNING   pid 42, uptime 0:10:00"
    response = _response(in_container_stdout=_healthy_probe_stdout(system_interface_status=status))
    probe = _probe_for(response, "running under supervisord")
    assert probe.command.startswith(f"mngr exec {_SERVICES_AGENT_ID} ")
    assert "supervisorctl -c /code/supervisord.conf status system_interface" in probe.command
    # The output is supervisord's own status line, verbatim.
    assert probe.output == status


def test_system_interface_output_is_status_line_when_not_running() -> None:
    status = "system_interface   FATAL     Exited too quickly"
    response = _response(in_container_stdout=_healthy_probe_stdout(system_interface_status=status))
    probe = _probe_for(response, "running under supervisord")
    assert probe.answer == ProbeAnswer.NO
    assert probe.output == status


def test_curl_output_is_bare_status_code() -> None:
    response = _response(in_container_stdout=_healthy_probe_stdout(curl_status="200"))
    probe = _probe_for(response, "GET /")
    assert probe.command.startswith(f"mngr exec {_SERVICES_AGENT_ID} ")
    assert "curl" in probe.command
    # Probes the bare root, decoupled from any app-specific route.
    assert "http://localhost:8000/" in probe.command
    assert "/api/agents" not in probe.command
    # not "HTTP 200"
    assert probe.output == "200"


def test_port_listening_output_matches_listener_lines() -> None:
    response = _response(
        in_container_stdout=_healthy_probe_stdout(port_listener="LISTEN 0.0.0.0:8000\nLISTEN ::1:8000")
    )
    probe = _probe_for(response, "listening on the system-interface inner port")
    assert probe.command.startswith(f"mngr exec {_SERVICES_AGENT_ID} ")
    assert "/proc/net/tcp" in probe.command
    assert probe.output == "LISTEN 0.0.0.0:8000\nLISTEN ::1:8000"


def test_port_listening_no_listener_output_matches_command_fallback() -> None:
    """The no-listener output must be byte-identical to what the command prints."""
    response = _response(in_container_stdout=_healthy_probe_stdout(port_listener=""))
    probe = _probe_for(response, "listening on the system-interface inner port")
    assert probe.answer == ProbeAnswer.NO
    assert probe.output == "(no LISTEN socket on port 8000)"
    # The command's own fallback (printed when no socket matches) is the same string.
    assert '"(no LISTEN socket on port %d)"' in probe.command


# --- /proc/net/tcp LISTEN parsing -----------------------------------------

# A representative /proc/net/tcp sample. Columns: ``sl local_address
# rem_address st ...``; state ``0A`` is LISTEN, ``01`` is ESTABLISHED. The
# local port is hex: 0x1F90 == 8080, 0x0016 == 22.
_PROC_NET_TCP_SAMPLE = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 100 1
   1: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 200 1
   2: 0100007F:1F90 0100007F:C722 01 00000000:00000000 00:00000000 00000000  1000        0 300 1
"""

# IPv6 /proc/net/tcp6 sample: ::1 (loopback) listening on 0x1F90 == 8080.
_PROC_NET_TCP6_SAMPLE = """\
  sl  local_address                         remote_address                        st ...
   0: 00000000000000000000000001000000:1F90 00000000000000000000000000000000:0000 0A 0 0 0
"""


def test_parse_listening_sockets_matches_listen_state_and_port() -> None:
    listeners = parse_listening_sockets(_PROC_NET_TCP_SAMPLE, 8080)
    # Row 0 is LISTEN on 127.0.0.1:8080; row 2 has the same port but is
    # ESTABLISHED (state 01) so it must be excluded.
    assert listeners == ["127.0.0.1:8080"]


def test_parse_listening_sockets_decodes_all_interfaces_and_ignores_other_ports() -> None:
    assert parse_listening_sockets(_PROC_NET_TCP_SAMPLE, 22) == ["0.0.0.0:22"]
    assert parse_listening_sockets(_PROC_NET_TCP_SAMPLE, 9999) == []


def test_parse_listening_sockets_decodes_ipv6() -> None:
    assert parse_listening_sockets(_PROC_NET_TCP6_SAMPLE, 8080) == ["::1:8080"]


def test_parse_listening_sockets_skips_header_and_blank_lines() -> None:
    # Header row (state column is the literal "st") and blank lines must not
    # be misread as sockets.
    assert parse_listening_sockets("\n\n" + _PROC_NET_TCP_SAMPLE + "\n", 80) == []
