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
    assert _answer(response, "answer locally") == ProbeAnswer.YES


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
    assert _answer(response, "answer locally") == ProbeAnswer.NO


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


def test_dispatch_tier_surgical_when_container_running_and_exec_works() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout(
            {"services_toml_declares_system_interface": True, "inner_port": 8000, "curl_status": "502"}
        ),
        plugin_resolver_services={},
    )
    assert response.dispatch_tier == DispatchTier.SURGICAL


def test_dispatch_tier_host_when_container_is_offline() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="STOPPED"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert response.dispatch_tier == DispatchTier.HOST


def test_dispatch_tier_manual_when_container_running_but_exec_dead() -> None:
    """SSH-dead path: host claims RUNNING but mngr exec failed -- require user consent."""
    response = build_host_health_response(
        list_json=_list_json(host_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert response.dispatch_tier == DispatchTier.MANUAL


def test_dispatch_tier_misconfigured_beats_other_signals() -> None:
    """A missing [services.system_interface] block dominates: no restart will help."""
    response = build_host_health_response(
        list_json=_list_json(host_state="RUNNING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=_probe_stdout({"services_toml_declares_system_interface": False}),
        plugin_resolver_services={"system_interface": "http://127.0.0.1:9100"},
    )
    assert response.dispatch_tier == DispatchTier.MISCONFIGURED


def test_dispatch_tier_manual_for_ambiguous_host_state() -> None:
    response = build_host_health_response(
        list_json=_list_json(host_state="STARTING"),
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        in_container_stdout=None,
        plugin_resolver_services={},
    )
    assert response.dispatch_tier == DispatchTier.MANUAL


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


def _python_c_body(command: str) -> str | None:
    """Return the ``-c`` body of a ``python3 -c "..."`` command, or None."""
    tokens = shlex.split(command)
    if len(tokens) >= 3 and tokens[0] == "python3" and tokens[1] == "-c":
        return tokens[2]
    return None


def test_python_probe_commands_are_well_formed_and_runnable() -> None:
    """Every ``python3 -c`` probe command must shlex-split and compile as Python.

    Guards against quote-nesting bugs like the original services.toml
    command (single quotes nested inside a single-quoted ``-c`` body), which
    rendered an un-runnable, un-parseable string.
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
    python_commands = [(p.question, _python_c_body(p.command)) for p in response.probes]
    checked = [(q, body) for q, body in python_commands if body is not None]
    assert checked, "expected at least one python3 -c probe command"
    for question, body in checked:
        # compile() validates syntax without executing; a botched quote nest
        # yields a body that fails here.
        compile(body, f"<probe:{question}>", "exec")


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
