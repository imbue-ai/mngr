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
from imbue.mngr.primitives import AgentId

_AGENT_ID: AgentId = AgentId("agent-" + "0" * 31 + "1")
_SERVICES_AGENT_ID: AgentId = AgentId("agent-" + "0" * 31 + "2")


def _probe_stdout(payload: dict[str, object]) -> str:
    """Build a probe-stdout string with the sentinel and a JSON payload line."""
    return PROBE_SENTINEL + "\n" + json.dumps(payload) + "\n"


def _list_json(host_state: str = "RUNNING", services_state: str | None = "RUNNING") -> str:
    agents: list[dict[str, object]] = [{"id": str(_AGENT_ID), "host": {"state": host_state}}]
    if services_state is not None:
        agents.append({"id": str(_SERVICES_AGENT_ID), "state": services_state})
    return json.dumps({"agents": agents, "errors": []})


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


# --- build_probe_argv -----------------------------------------------------


def test_build_probe_argv_targets_services_agent_with_timeout_and_no_start() -> None:
    argv = build_probe_argv("/usr/local/bin/mngr", _SERVICES_AGENT_ID)
    assert argv[:3] == ["/usr/local/bin/mngr", "exec", str(_SERVICES_AGENT_ID)]
    assert "--no-start" in argv
    assert "--timeout" in argv
    assert "--quiet" in argv


# --- per-probe answers ----------------------------------------------------


def test_container_running_probe_says_yes_when_host_state_is_running() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout({"services_toml_declares_system_interface": True}),
        plugin_resolver_services={},
    )
    assert _answer(response, "container running") == ProbeAnswer.YES


def test_container_running_probe_says_no_when_host_state_is_stopped() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="STOPPED"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert _answer(response, "container running") == ProbeAnswer.NO


def test_container_running_probe_is_unknown_for_ambiguous_host_state() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="STARTING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert _answer(response, "container running") == ProbeAnswer.UNKNOWN


def test_services_agent_registered_probe_yes_when_row_present() -> None:
    response = build_host_health_response(
        list_json=_list_json(services_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout({"services_toml_declares_system_interface": True}),
        plugin_resolver_services={},
    )
    assert _answer(response, "system-services agent registered") == ProbeAnswer.YES


def test_services_agent_registered_probe_no_when_row_absent() -> None:
    response = build_host_health_response(
        list_json=_list_json(services_state=None),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert _answer(response, "system-services agent registered") == ProbeAnswer.NO


def test_services_agent_registered_probe_unknown_when_id_not_known() -> None:
    response = build_host_health_response(
        list_json=_list_json(services_state=None),
        agent_id=_AGENT_ID,
        services_agent_id=None,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert _answer(response, "system-services agent registered") == ProbeAnswer.UNKNOWN


def test_host_state_probes_surface_mngr_list_failure_when_row_missing() -> None:
    """A failed ``mngr list`` (no usable row) surfaces its reason on the host-state probes.

    The reason is shown in place of a bare "no row" so the user can tell the
    listing failed (e.g. a provider was unreachable) rather than concluding the
    host / agent is genuinely absent; both probes answer UNKNOWN since the
    listing told us nothing about this workspace.
    """
    response = build_host_health_response(
        list_json=None,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
        mngr_list_error="timed out after 120s",
    )
    container_probe = _probe_for(response, "container running")
    assert container_probe.answer == ProbeAnswer.UNKNOWN
    assert "mngr list failed: timed out after 120s" in container_probe.output
    services_probe = _probe_for(response, "system-services agent registered")
    assert services_probe.answer == ProbeAnswer.UNKNOWN
    assert "mngr list failed: timed out after 120s" in services_probe.output


def test_host_state_probes_prefer_partial_list_data_over_failure_reason() -> None:
    """When ``mngr list`` returns this workspace's row despite a non-clean exit, show the data.

    ``--on-error continue`` can yield a usable row for our own host even when an
    unrelated provider failed, so a present row wins over the failure reason.
    """
    response = build_host_health_response(
        list_json=_list_json(host_state="RUNNING", services_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
        mngr_list_error="exited 1: provider 'other' unreachable",
    )
    container_probe = _probe_for(response, "container running")
    assert container_probe.answer == ProbeAnswer.YES
    assert container_probe.output == "RUNNING"
    assert "mngr list failed" not in container_probe.output


def test_can_run_commands_probe_no_when_sentinel_absent() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert _answer(response, "run a command") == ProbeAnswer.NO


def test_can_run_commands_probe_yes_when_sentinel_present() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout({"services_toml_declares_system_interface": True}),
        plugin_resolver_services={},
    )
    assert _answer(response, "run a command") == ProbeAnswer.YES


def test_services_toml_probe_no_when_declaration_missing() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout({"services_toml_declares_system_interface": False}),
        plugin_resolver_services={},
    )
    assert _answer(response, "services.toml") == ProbeAnswer.NO


def test_services_toml_probe_yes_when_declaration_present() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout({"services_toml_declares_system_interface": True}),
        plugin_resolver_services={},
    )
    assert _answer(response, "services.toml") == ProbeAnswer.YES


def test_services_toml_probe_unknown_when_probe_did_not_run() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert _answer(response, "services.toml") == ProbeAnswer.UNKNOWN


def test_curl_probe_yes_for_200() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout(
            {"services_toml_declares_system_interface": True, "inner_port": 8000, "curl_status": "200"}
        ),
        plugin_resolver_services={},
    )
    assert _answer(response, "GET /") == ProbeAnswer.YES


def test_curl_probe_no_for_non_200() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout(
            {"services_toml_declares_system_interface": True, "inner_port": 8000, "curl_status": "502"}
        ),
        plugin_resolver_services={},
    )
    assert _answer(response, "GET /") == ProbeAnswer.NO


def test_port_listener_probe_yes_when_listener_present() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout(
            {
                "services_toml_declares_system_interface": True,
                "inner_port": 8000,
                "port_listener": "LISTEN 0.0.0.0:8000\nLISTEN ::1:8000",
            }
        ),
        plugin_resolver_services={},
    )
    assert _answer(response, "listening on the system-interface inner port") == ProbeAnswer.YES


def test_plugin_resolver_probe_yes_when_services_registered() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout({"services_toml_declares_system_interface": True}),
        plugin_resolver_services={"system_interface": "http://127.0.0.1:9100"},
    )
    probe = _probe_for(response, "registered with the plugin resolver")
    assert probe.answer == ProbeAnswer.YES
    assert "system_interface" in probe.output


def test_plugin_resolver_probe_no_when_no_services_registered() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout({"services_toml_declares_system_interface": True}),
        plugin_resolver_services={},
    )
    assert _answer(response, "registered with the plugin resolver") == ProbeAnswer.NO


# --- dispatch_tier classification ----------------------------------------


def test_dispatch_tier_interface_unresponsive_when_container_running_and_exec_works() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout(
            {"services_toml_declares_system_interface": True, "inner_port": 8000, "curl_status": "502"}
        ),
        plugin_resolver_services={},
    )
    assert response.dispatch_tier == DispatchTier.INTERFACE_UNRESPONSIVE


def test_dispatch_tier_host_offline_when_container_is_offline() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="STOPPED"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert response.dispatch_tier == DispatchTier.HOST_OFFLINE


def test_dispatch_tier_host_unresponsive_when_container_running_but_exec_dead() -> None:
    """SSH-dead path: host claims RUNNING but mngr exec failed -- require user consent."""
    response = build_host_health_response(
        list_json=_list_json(host_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


def test_dispatch_tier_misconfigured_beats_other_signals() -> None:
    """A missing [services.system_interface] block dominates: no restart will help."""
    response = build_host_health_response(
        list_json=_list_json(host_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout({"services_toml_declares_system_interface": False}),
        plugin_resolver_services={"system_interface": "http://127.0.0.1:9100"},
    )
    assert response.dispatch_tier == DispatchTier.WORKSPACE_MISCONFIGURED


def test_dispatch_tier_host_unresponsive_for_ambiguous_host_state() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="STARTING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


# --- shape sanity --------------------------------------------------------


def test_every_probe_has_a_command_and_an_output() -> None:
    """No probe should render with an empty command label or empty output text."""
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
        mngr_list_command="/usr/local/bin/mngr list --format json --quiet",
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
    body. Returns None for commands that are not python reproductions (the jq
    and curl ones).
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

    Guards against quote-nesting bugs (the original services.toml command
    nested single quotes inside a single-quoted ``-c`` body) and against the
    ``mngr exec`` wrapper mangling the inner script. compile() validates the
    inner body's syntax without executing it.
    """
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout(
            {"services_toml_declares_system_interface": True, "inner_port": 8000, "curl_status": "200"}
        ),
        plugin_resolver_services={},
    )
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
# Each probe's command, run where minds ran it, must print exactly its output.
# These tests pin the command shape and assert the rendered output is the value
# the command would emit (not a derived prose description).


def _healthy_probe_stdout(**overrides: object) -> str:
    payload: dict[str, object] = {
        "services_toml_declares_system_interface": True,
        "inner_port": 8000,
        "curl_status": "200",
        "port_listener": "LISTEN 0.0.0.0:8000",
    }
    payload.update(overrides)
    return _probe_stdout(payload)


def test_container_running_command_derives_state_with_jq() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
        mngr_list_command="mngr list --format json --quiet --on-error continue",
    )
    probe = _probe_for(response, "container running")
    assert " | jq -r " in probe.command
    # the jq filter targets this agent
    assert str(_AGENT_ID) in probe.command
    # exactly what the jq pipeline prints
    assert probe.output == "RUNNING"


def test_services_agent_command_outputs_bare_state_without_prefix() -> None:
    response = build_host_health_response(
        list_json=_list_json(services_state="RUNNING_UNKNOWN_AGENT_TYPE"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
        mngr_list_command="mngr list --format json --quiet --on-error continue",
    )
    probe = _probe_for(response, "services agent")
    assert " | jq -r " in probe.command
    # no synthetic "state=" prefix
    assert probe.output == "RUNNING_UNKNOWN_AGENT_TYPE"


def test_can_run_commands_output_is_the_raw_exec_stdout() -> None:
    stdout = _probe_stdout({"services_toml_declares_system_interface": True})
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=stdout,
        plugin_resolver_services={},
        mngr_exec_command="mngr exec agent-x 'echo hi' --quiet",
    )
    probe = _probe_for(response, "run a command")
    assert probe.command == "mngr exec agent-x 'echo hi' --quiet"
    # the verbatim stdout the command produced
    assert probe.output == stdout


def test_services_toml_command_is_mngr_exec_and_output_is_declared() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_healthy_probe_stdout(),
        plugin_resolver_services={},
    )
    probe = _probe_for(response, "services.toml")
    assert probe.command.startswith(f"mngr exec {_SERVICES_AGENT_ID} ")
    assert "python3 -c" in probe.command
    assert probe.output == "declared"


def test_services_toml_output_is_missing_when_not_declared() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_healthy_probe_stdout(services_toml_declares_system_interface=False),
        plugin_resolver_services={},
    )
    assert _probe_for(response, "services.toml").output == "MISSING"


def test_curl_output_is_bare_status_code() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_healthy_probe_stdout(curl_status="200"),
        plugin_resolver_services={},
    )
    probe = _probe_for(response, "GET /")
    assert probe.command.startswith(f"mngr exec {_SERVICES_AGENT_ID} ")
    assert "curl" in probe.command
    # Probes the bare root, decoupled from any app-specific route.
    assert "http://localhost:8000/" in probe.command
    assert "/api/agents" not in probe.command
    # not "HTTP 200"
    assert probe.output == "200"


def test_port_listening_output_matches_listener_lines() -> None:
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_healthy_probe_stdout(port_listener="LISTEN 0.0.0.0:8000\nLISTEN ::1:8000"),
        plugin_resolver_services={},
    )
    probe = _probe_for(response, "listening on the system-interface inner port")
    assert probe.command.startswith(f"mngr exec {_SERVICES_AGENT_ID} ")
    assert "/proc/net/tcp" in probe.command
    assert probe.output == "LISTEN 0.0.0.0:8000\nLISTEN ::1:8000"


def test_port_listening_no_listener_output_matches_command_fallback() -> None:
    """The no-listener output must be byte-identical to what the command prints."""
    response = build_host_health_response(
        list_json=_list_json(),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_healthy_probe_stdout(port_listener=""),
        plugin_resolver_services={},
    )
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
