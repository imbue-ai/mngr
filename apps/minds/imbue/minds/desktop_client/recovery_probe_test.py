"""Unit tests for the recovery-diagnostics probe builder + dispatch tier."""

import json

from imbue.minds.desktop_client.recovery_probe import DispatchTier
from imbue.minds.desktop_client.recovery_probe import HostHealthResponse
from imbue.minds.desktop_client.recovery_probe import PROBE_SENTINEL
from imbue.minds.desktop_client.recovery_probe import Probe
from imbue.minds.desktop_client.recovery_probe import ProbeAnswer
from imbue.minds.desktop_client.recovery_probe import build_host_health_response
from imbue.minds.desktop_client.recovery_probe import build_probe_argv
from imbue.minds.desktop_client.recovery_probe import parse_inner_port_from_command
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
                "port_listener": 'LISTEN 0 128 *:8000 *:* users:(("python3",pid=10,fd=3))',
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
