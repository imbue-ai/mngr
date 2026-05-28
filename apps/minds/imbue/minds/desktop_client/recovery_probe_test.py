"""Unit tests for the recovery-diagnostics probe parsing + classification."""

import json

from imbue.minds.desktop_client.recovery_probe import PROBE_SENTINEL
from imbue.minds.desktop_client.recovery_probe import build_host_health_response
from imbue.minds.desktop_client.recovery_probe import build_probe_argv
from imbue.minds.desktop_client.recovery_probe import classify_host_state
from imbue.minds.desktop_client.recovery_probe import extract_agent_row
from imbue.minds.desktop_client.recovery_probe import extract_host_state
from imbue.minds.desktop_client.recovery_probe import extract_services_agent_state
from imbue.minds.desktop_client.recovery_probe import extract_ssh_connections
from imbue.minds.desktop_client.recovery_probe import parse_inner_port_from_command
from imbue.minds.desktop_client.recovery_probe import parse_probe_output
from imbue.mngr.primitives import AgentId

_AGENT_ID: AgentId = AgentId("agent-" + "0" * 31 + "1")
_SERVICES_AGENT_ID: AgentId = AgentId("agent-" + "0" * 31 + "2")
_OTHER_AGENT_ID: AgentId = AgentId("agent-" + "0" * 31 + "3")


def _probe_stdout(payload: dict[str, object]) -> str:
    """Build a probe-stdout string with the sentinel and a JSON payload line."""
    return PROBE_SENTINEL + "\n" + json.dumps(payload) + "\n"


# --- parse_probe_output ---------------------------------------------------


def test_parse_probe_output_returns_ssh_dead_when_stdout_is_none() -> None:
    record = parse_probe_output(None)
    assert record.ssh_dead is True
    assert record.services_toml_declares_system_interface is None


def test_parse_probe_output_returns_ssh_dead_when_sentinel_absent() -> None:
    record = parse_probe_output("nothing interesting here\n")
    assert record.ssh_dead is True
    # Original stdout is preserved for the debug menu.
    assert record.raw_stdout == "nothing interesting here\n"


def test_parse_probe_output_extracts_full_payload() -> None:
    payload: dict[str, object] = {
        "tmux_ls": "svc-system_interface: 1 window",
        "services_toml_declares_system_interface": True,
        "services_toml_path": "/code/services.toml",
        "inner_port": 8000,
        "port_listener": 'LISTEN 0 128 *:8000 *:* users:(("python3",pid=10,fd=3))',
        "curl_status": "200",
    }
    record = parse_probe_output(_probe_stdout(payload))
    assert record.ssh_dead is False
    assert record.services_toml_declares_system_interface is True
    assert record.inner_port == 8000
    assert record.port_listener is not None and "LISTEN" in record.port_listener
    assert record.curl_status == "200"


def test_parse_probe_output_handles_missing_services_toml() -> None:
    payload: dict[str, object] = {
        "tmux_ls": "",
        "services_toml_declares_system_interface": False,
        "inner_port": None,
    }
    record = parse_probe_output(_probe_stdout(payload))
    assert record.ssh_dead is False
    assert record.services_toml_declares_system_interface is False
    assert record.inner_port is None


def test_parse_probe_output_tolerates_malformed_json_after_sentinel() -> None:
    record = parse_probe_output(PROBE_SENTINEL + "\nnot json\n")
    assert record.ssh_dead is False
    assert record.services_toml_declares_system_interface is None


# --- inner-port regex -----------------------------------------------------


def test_parse_inner_port_matches_forward_port_command() -> None:
    cmd = "python3 scripts/forward_port.py --url http://localhost:8000 --name system_interface && system-interface"
    assert parse_inner_port_from_command(cmd) == 8000


def test_parse_inner_port_returns_none_when_command_lacks_url_flag() -> None:
    assert parse_inner_port_from_command("system-interface") is None


# --- classify_host_state --------------------------------------------------


def test_classify_host_state_running_is_reachable() -> None:
    assert classify_host_state("RUNNING") == (True, False)


def test_classify_host_state_stopped_states_are_offline() -> None:
    for state in ("STOPPED", "STOPPING", "CRASHED", "FAILED"):
        assert classify_host_state(state) == (False, True), state


def test_classify_host_state_ambiguous_is_neither() -> None:
    for state in ("STARTING", "PAUSED", "", "DESTROYED", "UNAUTHENTICATED"):
        assert classify_host_state(state) == (False, False), state


# --- extract_* helpers ----------------------------------------------------


def test_extract_agent_row_finds_matching_id() -> None:
    list_json = json.dumps({"agents": [{"id": "agent-other"}, {"id": str(_AGENT_ID), "host": {"state": "RUNNING"}}]})
    row = extract_agent_row(list_json, _AGENT_ID)
    assert row is not None
    assert row["id"] == str(_AGENT_ID)


def test_extract_agent_row_returns_none_for_unknown_agent() -> None:
    list_json = json.dumps({"agents": [{"id": "agent-other"}]})
    assert extract_agent_row(list_json, _AGENT_ID) is None


def test_extract_host_state_reads_nested_field() -> None:
    row = {"id": str(_AGENT_ID), "host": {"state": "RUNNING"}}
    assert extract_host_state(row) == "RUNNING"


def test_extract_services_agent_state_returns_state_for_services_agent() -> None:
    list_json = json.dumps(
        {
            "agents": [
                {"id": str(_AGENT_ID), "state": "RUNNING"},
                {"id": str(_SERVICES_AGENT_ID), "state": "WAITING"},
            ]
        }
    )
    assert extract_services_agent_state(list_json, _SERVICES_AGENT_ID) == "WAITING"


def test_extract_services_agent_state_handles_none_agent_id() -> None:
    list_json = json.dumps({"agents": []})
    assert extract_services_agent_state(list_json, None) == ""


def test_extract_ssh_connections_includes_remote_hosts_only() -> None:
    list_json = json.dumps(
        {
            "agents": [
                {
                    "id": str(_AGENT_ID),
                    "host": {
                        "id": "host-1",
                        "ssh": {
                            "user": "root",
                            "host": "1.2.3.4",
                            "port": 22,
                            "key_path": "/tmp/key",
                            "command": "ssh -i /tmp/key -p 22 root@1.2.3.4",
                        },
                    },
                },
                # Local host: no ssh block.
                {"id": str(_OTHER_AGENT_ID), "host": {"id": "host-local"}},
            ]
        }
    )
    conns = extract_ssh_connections(list_json)
    assert len(conns) == 1
    assert conns[0].user == "root"
    assert conns[0].host == "1.2.3.4"
    assert conns[0].port == 22
    assert conns[0].host_id == "host-1"


def test_extract_ssh_connections_dedupes_by_host_id() -> None:
    """Two agents on the same remote host produce a single ssh entry."""
    common_ssh = {
        "user": "root",
        "host": "1.2.3.4",
        "port": 22,
        "key_path": "/tmp/key",
        "command": "ssh -i /tmp/key -p 22 root@1.2.3.4",
    }
    list_json = json.dumps(
        {
            "agents": [
                {"id": str(_AGENT_ID), "host": {"id": "host-1", "ssh": common_ssh}},
                {"id": str(_SERVICES_AGENT_ID), "host": {"id": "host-1", "ssh": common_ssh}},
            ]
        }
    )
    conns = extract_ssh_connections(list_json)
    assert len(conns) == 1


# --- build_probe_argv -----------------------------------------------------


def test_build_probe_argv_targets_services_agent_with_timeout_and_no_start() -> None:
    argv = build_probe_argv("/usr/local/bin/mngr", _SERVICES_AGENT_ID)
    assert argv[:3] == ["/usr/local/bin/mngr", "exec", str(_SERVICES_AGENT_ID)]
    # The probe must never auto-start a stopped host (that's the recovery
    # tier's decision) and must run under a hard timeout.
    assert "--no-start" in argv
    assert "--timeout" in argv
    assert "--quiet" in argv


# --- build_host_health_response ------------------------------------------


def test_build_host_health_response_misconfigured_when_services_toml_lacks_entry() -> None:
    list_json = json.dumps({"agents": [{"id": str(_AGENT_ID), "host": {"state": "RUNNING"}}]})
    probe = parse_probe_output(_probe_stdout({"services_toml_declares_system_interface": False, "inner_port": None}))
    response = build_host_health_response(
        list_json=list_json,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        probe=probe,
        plugin_resolver_services={},
    )
    assert response.is_misconfigured is True
    # Host is still RUNNING; only services.toml is wrong.
    assert response.reachable is True
    assert response.host_offline is False


def test_build_host_health_response_ssh_dead_when_probe_missing_sentinel() -> None:
    list_json = json.dumps({"agents": [{"id": str(_AGENT_ID), "host": {"state": "RUNNING"}}]})
    probe = parse_probe_output(None)
    response = build_host_health_response(
        list_json=list_json,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        probe=probe,
        plugin_resolver_services={},
    )
    assert response.ssh_dead is True
    # is_misconfigured stays False on ssh_dead -- the probe never ran, so
    # we have no evidence about services.toml either way.
    assert response.is_misconfigured is False


def test_build_host_health_response_carries_plugin_resolver_services() -> None:
    list_json = json.dumps({"agents": [{"id": str(_AGENT_ID), "host": {"state": "RUNNING"}}]})
    probe = parse_probe_output(_probe_stdout({"services_toml_declares_system_interface": True}))
    response = build_host_health_response(
        list_json=list_json,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        probe=probe,
        plugin_resolver_services={"system_interface": "http://127.0.0.1:9100"},
    )
    assert response.plugin_resolver_services == {"system_interface": "http://127.0.0.1:9100"}


def test_build_host_health_response_offline_host_drives_host_restart_tier() -> None:
    list_json = json.dumps({"agents": [{"id": str(_AGENT_ID), "host": {"state": "STOPPED"}}]})
    # ssh_dead because the host is down.
    probe = parse_probe_output(None)
    response = build_host_health_response(
        list_json=list_json,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        probe=probe,
        plugin_resolver_services={},
    )
    assert response.reachable is False
    assert response.host_offline is True


# --- raw mngr list capture -----------------------------------------------


def test_build_host_health_response_passes_mngr_list_capture_through_verbatim() -> None:
    """The diagnostics menu renders the raw command + stdout + stderr + exit
    code, so build_host_health_response must pass each through untouched."""
    list_json = json.dumps({"agents": [], "errors": []})
    response = build_host_health_response(
        list_json=list_json,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        probe=parse_probe_output(None),
        plugin_resolver_services={},
        mngr_list_command="/usr/local/bin/mngr list --format json --quiet",
        mngr_list_stdout=list_json,
        mngr_list_stderr="WARNING: Vultr API key not configured, skipping VPS discovery\n",
        mngr_list_exit_code=0,
    )
    assert response.mngr_list_command == "/usr/local/bin/mngr list --format json --quiet"
    assert response.mngr_list_stdout == list_json
    assert "Vultr API key" in response.mngr_list_stderr
    assert response.mngr_list_exit_code == 0


def test_build_host_health_response_carries_subprocess_failure_state() -> None:
    """When the subprocess could not be spawned at all, exit_code is None and
    the streams are empty -- but mngr_list_error carries the exec failure."""
    response = build_host_health_response(
        list_json=None,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        probe=parse_probe_output(None),
        plugin_resolver_services={},
        mngr_list_command="/usr/local/bin/mngr list --format json",
        mngr_list_stdout="",
        mngr_list_stderr="",
        mngr_list_exit_code=None,
        mngr_list_error="[Errno 2] No such file or directory: 'mngr'",
    )
    assert response.mngr_list_exit_code is None
    assert response.mngr_list_stdout == ""
    assert response.mngr_list_stderr == ""
    assert response.mngr_list_error is not None
    assert "No such file" in response.mngr_list_error


def test_build_host_health_response_plugin_resolver_has_services_reflects_presence() -> None:
    list_json = json.dumps({"agents": [{"id": str(_AGENT_ID), "host": {"id": "host-abc", "state": "RUNNING"}}]})
    response_empty = build_host_health_response(
        list_json=list_json,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        probe=parse_probe_output(None),
        plugin_resolver_services={},
    )
    assert response_empty.plugin_resolver_has_services is False

    response_present = build_host_health_response(
        list_json=list_json,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        probe=parse_probe_output(None),
        plugin_resolver_services={"system_interface": "http://127.0.0.1:9100"},
    )
    assert response_present.plugin_resolver_has_services is True


def test_build_host_health_response_carries_mngr_list_error_for_blast_radius() -> None:
    """When mngr list errored on a *different* host, surface that as
    mngr_list_error so the recovery page can tell the user the issue is
    elsewhere."""
    response = build_host_health_response(
        list_json=None,
        agent_id=_AGENT_ID,
        services_agent_id=_SERVICES_AGENT_ID,
        probe=parse_probe_output(None),
        plugin_resolver_services={},
        mngr_list_error="provider=docker: HostConnectionError: SSH error (Error reading SSH protocol banner)",
    )
    assert response.mngr_list_error is not None
    assert "docker" in response.mngr_list_error
    assert "SSH" in response.mngr_list_error
