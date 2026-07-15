"""Unit tests for the recovery-diagnostics probe builder + dispatch tier."""

import json
import shlex

import pytest

from imbue.minds.desktop_client.recovery_probe import DispatchTier
from imbue.minds.desktop_client.recovery_probe import HOST_ACCESS_REJECTED_REASON
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
    probe_timed_out: bool = False,
    probe_exec_attempted: bool = False,
    classification_is_trustworthy: bool = True,
) -> HostHealthResponse:
    """Call ``build_host_health_response`` with resolver-sourced defaults.

    Defaults to a healthy RUNNING host with a completed probe and a trustworthy
    (post-onset) snapshot, so each test only has to vary the inputs it exercises.
    """
    return build_host_health_response(
        host_state=host_state,
        services_agent_id=services_agent_id,
        in_container_stdout=in_container_stdout,
        plugin_resolver_services=plugin_resolver_services or {},
        provider_error_message=provider_error_message,
        provider_label=provider_label,
        mngr_exec_command=mngr_exec_command,
        probe_timed_out=probe_timed_out,
        probe_exec_attempted=probe_exec_attempted,
        classification_is_trustworthy=classification_is_trustworthy,
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


def test_build_probe_argv_passes_agent_id_to_the_inner_script() -> None:
    """The inner script's agent-process scan needs the agent id as its argv[1]."""
    argv = build_probe_argv("/usr/local/bin/mngr", _SERVICES_AGENT_ID)
    shell_command = argv[3]
    assert shell_command.endswith(f"| python3 - {_SERVICES_AGENT_ID}")


# --- per-probe answers ----------------------------------------------------


def test_container_running_probe_says_yes_when_host_state_is_running() -> None:
    response = _response(host_state="RUNNING", in_container_stdout=_probe_stdout({}))
    assert _answer(response, "container running") == ProbeAnswer.YES


def test_container_running_probe_says_no_when_host_state_is_stopped() -> None:
    response = _response(host_state="STOPPED")
    assert _answer(response, "container running") == ProbeAnswer.NO


def test_container_running_probe_says_yes_when_host_state_is_unauthenticated() -> None:
    """UNAUTHENTICATED means the container was observed running but inner SSH is dead.

    Both producers (docker's connection-error fallback hook and imbue_cloud's
    listing path; PR #2247) emit it only after observing a running container, so
    the "is the container running?" answer is an observed YES.
    """
    response = _response(host_state="UNAUTHENTICATED")
    assert _answer(response, "container running") == ProbeAnswer.YES


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


def test_services_agent_running_probe_yes_when_agent_processes_found() -> None:
    """Live MNGR_AGENT_ID-tagged processes mean the system-services agent is running."""
    response = _response(in_container_stdout=_probe_stdout({"agent_processes": "42 supervisord\n43 claude"}))
    probe = _probe_for(response, "system-services agent running")
    assert probe.answer == ProbeAnswer.YES
    assert probe.output == "42 supervisord\n43 claude"


def test_services_agent_running_probe_no_when_no_agent_processes_survive() -> None:
    """An empty scan means ``mngr stop system-services`` (or a crash) took the agent down.

    This is the row that names the cause when the supervisord probes can only
    report connection errors: supervisord died *because* the agent it runs under
    was stopped.
    """
    response = _response(in_container_stdout=_probe_stdout({"agent_processes": ""}))
    probe = _probe_for(response, "system-services agent running")
    assert probe.answer == ProbeAnswer.NO
    assert probe.output == f"(no live process carries MNGR_AGENT_ID={_SERVICES_AGENT_ID})"


def test_services_agent_running_probe_unknown_when_scan_skipped_or_errored() -> None:
    """No scan result (old script, no agent id) or a scan error cannot claim NO."""
    skipped = _response(in_container_stdout=_probe_stdout({}))
    assert _answer(skipped, "system-services agent running") == ProbeAnswer.UNKNOWN
    errored = _response(in_container_stdout=_probe_stdout({"agent_processes_error": "PermissionError(...)"}))
    assert _answer(errored, "system-services agent running") == ProbeAnswer.UNKNOWN


def test_services_agent_running_probe_unknown_when_probe_did_not_run() -> None:
    response = _response(in_container_stdout=None)
    assert _answer(response, "system-services agent running") == ProbeAnswer.UNKNOWN


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


def test_dispatch_tier_host_unresponsive_when_exec_works_but_interface_not_answering() -> None:
    """Exec reached the container but GET / is not answering 200 -> consent-gated verdict.

    There is no in-place (surgical) restart tier anymore: an interface that is
    not answering, with supervisord not reporting a self-heal in progress, gets
    the consent-gated "Workspace unresponsive" page. The page's liveness poll
    still returns the user home the moment the interface self-heals, so no
    restart fires without a click.
    """
    response = _response(
        host_state="RUNNING",
        in_container_stdout=_probe_stdout({"inner_port": 8000, "curl_status": "502"}),
    )
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


@pytest.mark.parametrize(
    "status_line",
    [
        "system_interface   RUNNING   pid 42, uptime 0:10:00",
        "system_interface   FATAL     Exited too quickly",
        "system_interface   EXITED    Jul 14 09:00 AM",
        "system_interface   STOPPED   Not started",
        "unix:///var/run/supervisor.sock refused connection",
    ],
)
def test_dispatch_tier_host_unresponsive_for_settled_or_unparseable_supervisord_state(status_line: str) -> None:
    """A settled (or unreadable) supervisord state carries no self-heal promise.

    Exec works but the interface is not answering: unless supervisord positively
    reports a start in progress, the verdict is the consent-gated
    HOST_UNRESPONSIVE -- including a status line we cannot parse.
    """
    response = _response(
        host_state="RUNNING",
        in_container_stdout=_probe_stdout(
            {"system_interface_status": status_line, "inner_port": 8000, "curl_status": "502"}
        ),
    )
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


@pytest.mark.parametrize(
    "status_line",
    [
        "system_interface   STARTING",
        "system_interface   BACKOFF   Exited too quickly (process log may have details)",
    ],
)
def test_dispatch_tier_indeterminate_while_supervisord_self_heals(status_line: str) -> None:
    """supervisord STARTING/BACKOFF means it is already fixing the interface -> keep checking.

    Restart-worthy conclusions must wait for the evidence to settle: a service in
    its startsecs window (STARTING) or supervisord's own retry loop (BACKOFF) is
    mid-self-heal, so the classifier keeps checking rather than rendering the
    consent-gated verdict.
    """
    response = _response(
        host_state="RUNNING",
        in_container_stdout=_probe_stdout(
            {"system_interface_status": status_line, "inner_port": 8000, "curl_status": "502"}
        ),
    )
    assert response.dispatch_tier == DispatchTier.INDETERMINATE


def test_no_interface_unresponsive_tier_exists() -> None:
    """The surgical-restart tier is gone: no DispatchTier member carries its value."""
    assert "interface_unresponsive" not in {tier.value for tier in DispatchTier}


def test_dispatch_tier_healthy_when_interface_answers_http_200() -> None:
    """Container up, exec works, and GET / answers 200: nothing to recover.

    The live in-container HTTP 200 is direct proof the interface is responding, so
    the classifier must report HEALTHY (the recovery page returns the user to the
    workspace) rather than a by-elimination unresponsive verdict -- this is
    the fix for a healthy workspace being misclassified (and needlessly
    restarted) just because container+exec were up.
    """
    response = _response(
        host_state="RUNNING",
        in_container_stdout=_probe_stdout({"inner_port": 8000, "curl_status": "200"}),
    )
    assert _answer(response, "GET /") == ProbeAnswer.YES
    assert response.dispatch_tier == DispatchTier.HEALTHY


@pytest.mark.parametrize("host_state", ["STOPPED", "CRASHED"])
def test_dispatch_tier_host_offline_when_container_observed_stopped_or_crashed(host_state: str) -> None:
    """A trusted observed-not-running state (settled, non-FAILED) auto-restarts unattended.

    In-app stops close their workspace windows first, so an open window observing
    STOPPED implies an out-of-app stop; reviving it is intended (this is also the
    path that revives workspaces after a laptop reboot).
    """
    response = _response(host_state=host_state)
    assert response.dispatch_tier == DispatchTier.HOST_OFFLINE


def test_dispatch_tier_indeterminate_while_host_is_stopping() -> None:
    """STOPPING is transitional: keep checking; the restart fires off the settled STOPPED."""
    response = _response(host_state="STOPPING", in_container_stdout=None)
    assert response.dispatch_tier == DispatchTier.INDETERMINATE


def test_dispatch_tier_host_unresponsive_for_failed_host_state() -> None:
    """FAILED is consent-gated, not auto-restarted.

    A failed-to-create host is observed not running, but an unattended
    ``mngr start`` on it mostly re-fails -- so it renders the consent-gated
    "Workspace unresponsive" page instead of HOST_OFFLINE's unattended restart.
    """
    response = _response(host_state="FAILED", in_container_stdout=None)
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


def test_dispatch_tier_backend_unreachable_for_unreachable_host_state_with_canned_reason() -> None:
    """UNREACHABLE (host rejected this machine's access) is terminal: retry/report only.

    A restart routes through the same rejected credential, so the page must not
    offer one. Discovery carries no per-host failure detail, so the response
    carries the canned access-rejected reason and the provider label.
    """
    response = _response(host_state="UNREACHABLE", in_container_stdout=None, provider_label="Imbue Cloud")
    assert response.dispatch_tier == DispatchTier.BACKEND_UNREACHABLE
    assert response.unreachable_reason == HOST_ACCESS_REJECTED_REASON
    assert response.provider_label == "Imbue Cloud"


def test_dispatch_tier_unreachable_host_state_is_subject_to_the_freshness_gate() -> None:
    """A stale UNREACHABLE reading is a negative verdict like any other -> INDETERMINATE first."""
    response = _response(host_state="UNREACHABLE", in_container_stdout=None, classification_is_trustworthy=False)
    assert response.dispatch_tier == DispatchTier.INDETERMINATE


def test_dispatch_tier_host_unresponsive_when_container_running_but_exec_dead() -> None:
    """SSH-dead path: host claims RUNNING but the exec cleanly failed -> consent-gated host restart.

    A clean exit with no sentinel (ssh dead) is a real negative signal, distinct
    from a timeout; with a trustworthy (post-onset) snapshot backing the RUNNING
    claim, this classifies as HOST_UNRESPONSIVE and asks the user before bouncing
    a live container.
    """
    response = _response(host_state="RUNNING", in_container_stdout=None)
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


def test_dispatch_tier_host_unresponsive_for_unauthenticated_host_state() -> None:
    """Dead inner sshd (`pkill sshd` in the container) -> consent-gated host restart.

    The provider observed the container running but could not get inside, so it
    reported UNAUTHENTICATED. The consent-gated host restart is the engineered
    recovery for this state (PR #2247): its stop step is not skipped, so the
    stop/start relaunches the inner sshd. This must NOT classify INDETERMINATE --
    a dead sshd never self-heals, so "keep checking" would strand the user.
    """
    response = _response(host_state="UNAUTHENTICATED", in_container_stdout=None)
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


def test_dispatch_tier_indeterminate_for_ambiguous_host_state() -> None:
    """A transitional host state is not an observation of the container -> keep checking.

    Only an observed RUNNING claim earns the consent-gated HOST_UNRESPONSIVE
    verdict; a state that answers neither "running" nor "offline" is non-evidence,
    so no verdict (and no restart affordance) is rendered off it.
    """
    response = _response(host_state="STARTING", in_container_stdout=None)
    assert response.dispatch_tier == DispatchTier.INDETERMINATE


def test_dispatch_tier_indeterminate_for_unknown_host_state() -> None:
    """An UNKNOWN host state (host unobservable during discovery) -> keep checking.

    The imbue_cloud provider surfaces UNKNOWN when a leased host's outer SSH is
    unreachable: the *path* to the host is broken, which says nothing about the
    container. Rendering a host-offline/unresponsive verdict (with its restart
    affordance) off that would offer a restart that is doomed for exactly the
    same reason the host is unreachable -- so it must classify INDETERMINATE.
    """
    response = _response(host_state="UNKNOWN", in_container_stdout=None)
    assert response.dispatch_tier == DispatchTier.INDETERMINATE


def test_dispatch_tier_indeterminate_when_probe_timed_out() -> None:
    """A timed-out in-container probe observed nothing, so no verdict is rendered.

    This is the macOS-sleep case: the probe was killed by its own timeout (the
    laptop suspended across it), which is absence of evidence -- not proof the
    workspace is down. It must classify as INDETERMINATE ("keep checking"), not
    the HOST_UNRESPONSIVE verdict a clean-exit ssh-dead probe earns.
    """
    response = _response(host_state="RUNNING", in_container_stdout=None, probe_timed_out=True)
    assert response.dispatch_tier == DispatchTier.INDETERMINATE


def test_dispatch_tier_indeterminate_when_snapshot_is_stale() -> None:
    """A negative verdict off a pre-outage snapshot is untrustworthy -> INDETERMINATE.

    Without direct in-container evidence, the host state comes from a discovery
    snapshot that predates the outage onset (still reading the stale value), so no
    host-state-derived verdict can be trusted yet.
    """
    response = _response(host_state="RUNNING", in_container_stdout=None, classification_is_trustworthy=False)
    assert response.dispatch_tier == DispatchTier.INDETERMINATE


def test_dispatch_tier_stale_snapshot_does_not_downgrade_offline_verdict_to_a_restart() -> None:
    """A stale STOPPED reading is not trusted enough to auto-dispatch a host restart.

    HOST_OFFLINE auto-dispatches an unattended restart, so it must only fire off a
    trusted observation. With a stale snapshot the container-offline reading yields
    INDETERMINATE instead.
    """
    response = _response(host_state="STOPPED", in_container_stdout=None, classification_is_trustworthy=False)
    assert response.dispatch_tier == DispatchTier.INDETERMINATE


def test_dispatch_tier_host_unresponsive_when_completed_exec_fails_despite_stale_snapshot() -> None:
    """An exec that completed without the sentinel resolves the tier even with no fresh snapshot.

    The dead-inner-sshd case with a stalled discovery producer: no snapshot taken
    after the outage onset will ever land, so the freshness gate can never open --
    but the exec itself ran to completion and failed, which is a direct fresh
    observation that we cannot get into the container. Waiting on the gate here
    parked the page on "Reconnecting" forever; the completed failure must yield
    the consent-gated HOST_UNRESPONSIVE instead.
    """
    response = _response(
        host_state="RUNNING",
        in_container_stdout=None,
        probe_exec_attempted=True,
        classification_is_trustworthy=False,
    )
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


def test_dispatch_tier_host_unresponsive_when_completed_exec_fails_with_no_host_state() -> None:
    """A completed exec failure needs no host-state observation at all.

    A stopped container under a stalled discovery producer: the resolver's host
    state is absent (or stale) and can never be re-certified, but the attempted
    exec completed and failed -- enough for the consent-gated verdict, whose
    restart also revives a genuinely stopped container.
    """
    response = _response(
        host_state="",
        in_container_stdout=None,
        probe_exec_attempted=True,
        classification_is_trustworthy=False,
    )
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


def test_dispatch_tier_timeout_stays_indeterminate_even_for_an_attempted_exec() -> None:
    """A timed-out exec observed nothing: the attempted flag must not upgrade it.

    The macOS-sleep case again: the exec was attempted but its window spanned a
    suspend, so the timeout is absence of evidence -- INDETERMINATE, never the
    unresponsive verdict.
    """
    response = _response(
        host_state="RUNNING",
        in_container_stdout=None,
        probe_timed_out=True,
        probe_exec_attempted=True,
        classification_is_trustworthy=False,
    )
    assert response.dispatch_tier == DispatchTier.INDETERMINATE


def test_dispatch_tier_trusted_offline_state_beats_a_completed_exec_failure() -> None:
    """A trusted STOPPED observation keeps the unattended HOST_OFFLINE restart.

    When discovery re-observed the host post-onset and read it STOPPED, the
    exec's failure is just the expected consequence of a stopped container; the
    trusted observation earns the unattended restart rather than downgrading to
    the consent-gated verdict.
    """
    response = _response(
        host_state="STOPPED",
        in_container_stdout=None,
        probe_exec_attempted=True,
        classification_is_trustworthy=True,
    )
    assert response.dispatch_tier == DispatchTier.HOST_OFFLINE


def test_dispatch_tier_trusted_unknown_state_falls_through_to_completed_exec_failure() -> None:
    """A trusted UNKNOWN state says nothing; the completed exec failure still resolves.

    UNKNOWN means the host was unobservable during discovery, which carries no
    verdict of its own -- but an exec that completed without reaching the
    container is direct evidence, so the page renders the consent-gated verdict
    instead of checking forever.
    """
    response = _response(
        host_state="UNKNOWN",
        in_container_stdout=None,
        probe_exec_attempted=True,
        classification_is_trustworthy=True,
    )
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


def test_dispatch_tier_healthy_direct_evidence_beats_a_stale_snapshot() -> None:
    """A live GET / 200 is trusted regardless of snapshot freshness.

    Positive in-container evidence short-circuits the INDETERMINATE guard: even
    with an untrustworthy snapshot (and a host_state that stale-reads STOPPED), an
    exec that reached the container and got a 200 proves the workspace is up, so
    the user is sent home (HEALTHY) rather than parked on "reconnecting".
    """
    response = _response(
        host_state="STOPPED",
        in_container_stdout=_probe_stdout({"inner_port": 8000, "curl_status": "200"}),
        classification_is_trustworthy=False,
        probe_timed_out=False,
    )
    assert response.dispatch_tier == DispatchTier.HEALTHY


def test_dispatch_tier_backend_unreachable_beats_indeterminate() -> None:
    """A provider error wins even when the classification would otherwise be INDETERMINATE."""
    response = _response(
        host_state="RUNNING",
        in_container_stdout=None,
        probe_timed_out=True,
        classification_is_trustworthy=False,
        provider_error_message="Your login expired.",
        provider_label="Imbue Cloud",
    )
    assert response.dispatch_tier == DispatchTier.BACKEND_UNREACHABLE


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
