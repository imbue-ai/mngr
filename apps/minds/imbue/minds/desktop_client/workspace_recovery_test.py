"""Unit coverage for the workspace-recovery engine (passive verdicts + restart worker).

These exercise the building blocks behind ``POST /api/v1/workspaces/<id>/restart``
and the recovery card's polled verdicts directly, complementing the end-to-end
route tests in ``api_v1_test.py`` with the granular restart-sequence failure
modes (unresolved system-services agent, stop/start command failures, the
host-already-stopped fast path).
"""

import shlex
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.agent_creator import WORKSPACE_READY_TIMEOUT_SECONDS
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import capture_error_logs
from imbue.minds.desktop_client.testing import record_provider_discovery_error
from imbue.minds.desktop_client.testing import scripted_workspace_probe_server
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.desktop_client.workspace_recovery import HOST_ACCESS_REJECTED_REASON
from imbue.minds.desktop_client.workspace_recovery import RestartDispatchOutcome
from imbue.minds.desktop_client.workspace_recovery import RestartReadinessOutcome
from imbue.minds.desktop_client.workspace_recovery import UnattendedRecoveryDispatcher
from imbue.minds.desktop_client.workspace_recovery import _HOST_RESTART_STARTUP_WAIT_SECONDS
from imbue.minds.desktop_client.workspace_recovery import _await_system_interface_ready
from imbue.minds.desktop_client.workspace_recovery import _build_mngr_start_argv
from imbue.minds.desktop_client.workspace_recovery import _build_mngr_stop_argv
from imbue.minds.desktop_client.workspace_recovery import _did_start_boot_a_host
from imbue.minds.desktop_client.workspace_recovery import _in_band_provider_outage_reason
from imbue.minds.desktop_client.workspace_recovery import _is_discovery_fresh
from imbue.minds.desktop_client.workspace_recovery import _provider_error_message_for_workspace
from imbue.minds.desktop_client.workspace_recovery import _report_restart_step_failure
from imbue.minds.desktop_client.workspace_recovery import dispatch_host_restart
from imbue.minds.desktop_client.workspace_recovery import is_recovery_classification_trustworthy
from imbue.minds.desktop_client.workspace_recovery import read_backend_unreachable_verdict
from imbue.minds.desktop_client.workspace_recovery import read_device_cannot_connect_verdict
from imbue.minds.desktop_client.workspace_recovery import run_restart_sequence
from imbue.minds.errors import MngrCommandError
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.errors import HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason

# Long enough that only a genuinely broken run reaches it. The wait ends the
# instant the command turns up, so this bounds a failure, not a passing test --
# and it is kept inside the suite's own ``--timeout=10`` per-test budget, so a
# broken run fails on the assertion rather than on pytest's opaque timeout.
_DISPATCH_WAIT_SECONDS: Final[float] = 5.0


def _write_mngr_stub(script: Path, subcommand_cases: str) -> str:
    """Write an executable stub that stands in for the ``mngr`` binary, and return its path.

    ``subcommand_cases`` are the ``case "$1"`` arms that give the stub whatever
    behaviour a test needs from it; every other subcommand exits cleanly. Every
    invocation appends its argv to a ``<script>.log`` sibling file, which
    :func:`_read_fake_mngr_invocations` reads back, so a test can assert which
    subcommands ran (e.g. that the stop step was skipped).
    """
    script.write_text('#!/bin/sh\necho "$@" >> "$0.log"\ncase "$1" in\n' + subcommand_cases + "  *) exit 0 ;;\nesac\n")
    script.chmod(0o755)
    return str(script)


def _write_fake_mngr(
    tmp_path: Path,
    stop_exit: int = 0,
    start_exit: int = 0,
    was_host_started: bool | None = None,
) -> str:
    """Write an mngr stub that exits per-subcommand.

    Lets a test simulate a failing stop or start without a real mngr / provider.
    ``was_host_started`` makes the start print the ``--format json`` result line
    real ``mngr start`` prints; None prints nothing, standing in for an older
    binary that cannot answer.
    """
    if was_host_started is None:
        start_case = f"  start) exit {start_exit} ;;\n"
    else:
        result = '{"started_agents": [], "count": 0, "was_host_started": %s}' % (
            "true" if was_host_started else "false"
        )
        start_case = f"  start) echo '{result}'; exit {start_exit} ;;\n"
    return _write_mngr_stub(tmp_path / "fake_mngr", f"  stop) exit {stop_exit} ;;\n" + start_case)


def _read_fake_mngr_invocations(mngr_binary: str) -> list[str]:
    """Return the recorded argv lines for a stub from :func:`_write_mngr_stub` (empty if never invoked)."""
    log_path = Path(mngr_binary + ".log")
    if not log_path.exists():
        return []
    return log_path.read_text().splitlines()


def _wait_for_mngr_invocation(mngr_binary: str, prefix: str) -> bool:
    """Wait for a dispatched restart worker to actually reach ``mngr <prefix>``.

    Must be called while the concurrency group is still open: the worker runs
    its mngr commands *through* that group, and the ``with`` exit flips it out
    of ACTIVE before joining the strands, so a worker not yet scheduled wakes to
    a group that refuses to run processes.
    """
    return poll_until(
        lambda: any(line.startswith(prefix) for line in _read_fake_mngr_invocations(mngr_binary)),
        timeout=_DISPATCH_WAIT_SECONDS,
        poll_interval=0.02,
    )


def _started_registry(workspace_agent: AgentId) -> InMemoryWorkspaceOperationRegistry:
    """A fresh operation registry with a RESTART operation already started for the agent."""
    registry = InMemoryWorkspaceOperationRegistry()
    registry.start(workspace_agent, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))
    return registry


# -- argv builders --


def test_build_mngr_stop_argv_always_stops_the_host() -> None:
    """The restart is host-only (the surgical services tier is gone), so the stop
    always carries --stop-host."""
    aid = AgentId.generate()
    argv = _build_mngr_stop_argv("/usr/local/bin/mngr", aid)
    assert argv[:3] == ["/usr/local/bin/mngr", "stop", str(aid)]
    assert "--stop-host" in argv


def test_build_mngr_start_argv_targets_the_agent_and_asks_for_structured_output() -> None:
    """The start must be structured: its ``was_host_started`` is the only way to
    learn that an idempotent start booted nothing, which is what stops the
    terminal state from claiming a restart that never ran."""
    aid = AgentId.generate()
    argv = _build_mngr_start_argv("/usr/local/bin/mngr", aid)
    assert argv[:3] == ["/usr/local/bin/mngr", "start", str(aid)]
    assert argv[-2:] == ["--format", "json"]


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ('{"started_agents": [], "count": 0, "was_host_started": false}', False),
        ('{"started_agents": ["a"], "count": 1, "was_host_started": true}', True),
        # An older mngr on PATH, or output that is not the result line: absence
        # of evidence, which a caller must not fold into "nothing booted".
        ("Successfully started 1 agent(s)", None),
        ("", None),
        ('{"count": 0}', None),
        ("not json at all {", None),
    ],
)
def test_did_start_boot_a_host_reads_only_a_real_answer(stdout: str, expected: bool | None) -> None:
    assert _did_start_boot_a_host(stdout) is expected


def test_an_unreadable_start_result_is_reported_rather_than_passed_over() -> None:
    """Silence disables the no-op detection for every restart, so it cannot be silent itself."""
    with capture_loguru(level="WARNING") as log_output:
        assert _did_start_boot_a_host("Successfully started 1 agent(s)") is None
    # Human output is not JSON, so this is the decode branch specifically -- the
    # other warning arm is for output that parsed but carried no field.
    assert "Could not read" in log_output.getvalue()


# -- provider-error attribution --


def test_provider_error_message_for_workspace_keys_on_this_workspaces_provider() -> None:
    """The provider error message is attributed by exact provider name.

    This is the per-provider keying that keeps a docker mind's recovery from
    being misclassified during a simultaneous imbue_cloud outage: only an error
    whose provider name matches this machine's is used.
    """
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="could not reach Imbue Cloud",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    matched = _provider_error_message_for_workspace(errors, "imbue_cloud_acme", True)
    assert matched == "could not reach Imbue Cloud"
    # The same error is withheld while no snapshot at/after the outage onset
    # backs it: it is a property of one snapshot, like the host state.
    assert _provider_error_message_for_workspace(errors, "imbue_cloud_acme", False) is None


def test_provider_error_message_for_workspace_ignores_other_providers() -> None:
    """An error for a different provider is never blamed on this machine."""
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="down",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, "docker", True) is None


def test_provider_error_message_for_workspace_is_none_when_provider_unknown() -> None:
    """Pre-discovery (provider unknown), we cannot attribute any error to this machine."""
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="down",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, None, True) is None


def test_provider_error_message_for_workspace_reduces_the_generic_shape_to_its_reason() -> None:
    """A snapshot carrying mngr's whole generic message yields just the reason.

    Discovery records an error's ``__cause__`` when it has one, so a provider
    that raised ``ProviderUnavailableError`` without a ``from`` clause (docker's
    stopped-state-container check) surfaces the wrapper where its neighbours
    surface a bare sentence. The card shows this under its own
    "Can't connect to Docker" heading, so the wrapper would name the provider a
    second time and trail mngr's internal marker sentence at the user.
    """
    reason = "Docker state container is stopped; host records are unreachable"
    errors = {
        ProviderInstanceName("docker"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message=str(ProviderUnavailableError(ProviderInstanceName("docker"), reason)),
            provider_name=ProviderInstanceName("docker"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, "docker", True) == reason


# -- restart worker --


def test_run_restart_sequence_fails_when_system_services_agent_is_unresolved(tmp_path: Path) -> None:
    """With no system-services agent discovered, the sequence ends in RESTART_FAILED."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    registry = _started_registry(workspace_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=MngrCliBackendResolver(),
            mngr_binary="mngr",
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=registry,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "system-services" in (tracker.get_last_restart_error(workspace_agent) or "")
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.FAILED
    assert len(error_records) == 1, error_records


def test_run_restart_sequence_fails_when_stop_command_errors(tmp_path: Path) -> None:
    """A non-zero ``mngr stop`` ends the sequence in RESTART_FAILED naming the stop step."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, stop_exit=1),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "Stop step" in (tracker.get_last_restart_error(workspace_agent) or "")
    assert len(error_records) == 1, error_records


def test_run_restart_sequence_fails_when_start_command_errors(tmp_path: Path) -> None:
    """A non-zero ``mngr start`` ends the sequence in RESTART_FAILED naming the start step."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, start_exit=1),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "Start step" in (tracker.get_last_restart_error(workspace_agent) or "")
    assert len(error_records) == 1, error_records


def test_run_restart_sequence_fails_and_reports_when_interface_never_answers(tmp_path: Path) -> None:
    """A clean stop+start whose interface never answers ends in RESTART_FAILED with one error log.

    With a plugin route configured (nonzero forward port + cookie) but nothing
    answering on it, the readiness wait times out; this failure branch was
    previously unlogged, so pin that it now reports exactly once.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            # Port 1 refuses connections, so every readiness poll fails fast.
            mngr_forward_port=1,
            mngr_forward_preauth_cookie="cookie",
            registry=_started_registry(workspace_agent),
            startup_wait_seconds=0.1,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "did not respond" in (tracker.get_last_restart_error(workspace_agent) or "")
    assert len(error_records) == 1, error_records


def test_run_restart_sequence_fails_when_stop_command_cannot_launch(tmp_path: Path) -> None:
    """A launch failure (missing ``mngr`` binary) surfaces as RESTART_FAILED naming the stop step.

    Exercises the path where ``_run_mngr`` wraps the ``OSError`` from the failed
    fork/exec into a ``MngrCommandError`` and the restart sequence catches that
    single domain error at the call site.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    missing_binary = str(tmp_path / "definitely_not_a_real_mngr")

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=missing_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "Stop step" in (tracker.get_last_restart_error(workspace_agent) or "")


def test_run_restart_sequence_recovers_on_clean_dispatch_without_plugin(tmp_path: Path) -> None:
    """Clean stop+start with no plugin route to probe through recovers the agent to HEALTHY."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=registry,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.DONE


def test_run_restart_sequence_skips_unsupported_stop_and_proceeds(tmp_path: Path) -> None:
    """A host-restart on a provider that cannot stop a host in place (Modal: ``mngr stop
    --stop-host`` raises HostShutdownNotSupportedError) must NOT fail the restart -- the stop
    step is skipped and the sequence proceeds to ``mngr start``, which restarts it on its own."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)
    # A fake mngr whose ``stop`` fails with the host-shutdown-not-supported message (as Modal
    # does) and whose ``start`` succeeds -- mirrors a no-shutdown provider's restart. The stderr
    # is built from mngr's exported HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE, the same constant the
    # restart worker matches on, so this exercises the real shared-source-of-truth mechanism.
    script = tmp_path / "fake_mngr_no_shutdown"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  stop) echo "Provider modal {HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE}" >&2; exit 1 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    script.chmod(0o755)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=str(script),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=registry,
        )

    # The unsupported stop is treated as "skip and proceed", so the restart recovers (not FAILED).
    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.DONE


def test_run_restart_sequence_skips_stop_for_start_only_dispatch(tmp_path: Path) -> None:
    """``skip_stop=True`` (the API's ``start_only``) goes straight to ``mngr start`` (no stop subprocess)."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
            skip_stop=True,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    invocations = _read_fake_mngr_invocations(mngr_binary)
    assert any(line.startswith("start ") for line in invocations)
    assert not any(line.startswith("stop ") for line in invocations)


def test_run_restart_sequence_stops_before_start_by_default(tmp_path: Path) -> None:
    """Without ``skip_stop``, a host restart stops the host before starting it."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    invocations = _read_fake_mngr_invocations(mngr_binary)
    stop_index = next((i for i, line in enumerate(invocations) if line.startswith("stop ")), None)
    start_index = next((i for i, line in enumerate(invocations) if line.startswith("start ")), None)
    assert stop_index is not None, invocations
    assert start_index is not None, invocations
    assert stop_index < start_index


# -- recovery classification trustworthiness (freshness gate on the verdict path) --


def _drive_to_stuck_with_onset(tracker: SystemInterfaceHealthTracker, agent_id: AgentId) -> datetime:
    """Drive ``agent_id`` to STUCK via the real probe path and return its onset.

    A zero stuck-threshold makes the first probe failure stick immediately, so the
    outage onset is recorded deterministically without sleeping.
    """
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)
    assert tracker.get_health(agent_id) == AgentHealth.STUCK
    onset = tracker.get_failure_run_started_wall_at(agent_id)
    assert onset is not None
    return onset


def _register_workspace_agent(resolver: MngrCliBackendResolver, agent_id: AgentId, provider_name: str) -> None:
    """Register one machine agent on ``provider_name`` so its display info resolves a provider.

    Trustworthiness is scoped to the machine's own provider's last snapshot, so
    the agent must be discoverable with a provider for the predicate to find a
    per-provider snapshot time.
    """
    agent = DiscoveredAgent(
        host_id=HostId("host-" + "0" * 31 + "1"),
        agent_id=agent_id,
        agent_name=AgentName("ws-agent"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"labels": {"workspace": "true", "is_primary": "true"}},
    )
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent_id,), discovered_agents=(agent,)))


def _set_provider_snapshot_at(resolver: MngrCliBackendResolver, provider_name: str, snapshot_at: datetime) -> None:
    """Record ``provider_name``'s last per-provider snapshot time on the resolver."""
    resolver.update_providers(
        provider_name=ProviderInstanceName(provider_name),
        provider=None,
        error=None,
        last_snapshot_at=snapshot_at,
    )


def test_is_discovery_fresh_distinguishes_recent_from_stale_and_missing() -> None:
    """Only a recent snapshot backs a trustworthy classification via the age fallback."""
    now = datetime.now(timezone.utc)
    assert _is_discovery_fresh(now, 10.0) is True
    # A snapshot well past the freshness window (a stalled pipeline) is stale.
    assert _is_discovery_fresh(now - timedelta(minutes=5), 10.0) is False
    # No snapshot at all (e.g. before initial discovery) cannot be trusted.
    assert _is_discovery_fresh(None, 10.0) is False


def test_discovery_freshness_is_measured_against_the_providers_own_cadence() -> None:
    """A provider that re-polls slowly is not stale merely for polling slowly.

    The window is a multiple of the cadence of the loop that produced the
    snapshot. Aging a 30s-cadence provider against the 10s stream baseline would
    call it stale one baseline-window after each of its perfectly normal polls.
    """
    aged = datetime.now(timezone.utc) - timedelta(seconds=45)
    assert _is_discovery_fresh(aged, 10.0) is False
    assert _is_discovery_fresh(aged, 30.0) is True


def test_classification_trustworthy_only_after_a_post_onset_snapshot() -> None:
    """A verdict is trustworthy only once a snapshot taken *after* the outage began has landed.

    A snapshot that predates the outage still carries the pre-outage host state (a
    just-stopped container still reads RUNNING), so it must not make the
    classification trustworthy -- only a snapshot at or after the outage onset
    does. Freshness is scoped to the machine's own provider's snapshot time.
    """
    resolver = MngrCliBackendResolver()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    _register_workspace_agent(resolver, agent_id, "docker")
    onset = _drive_to_stuck_with_onset(tracker, agent_id)

    # A recent snapshot of the agent's provider that nonetheless predates the outage
    # is the exact bug case: within the absolute freshness window but still showing
    # the pre-outage host state, so the classification stays untrustworthy.
    _set_provider_snapshot_at(resolver, "docker", onset - timedelta(seconds=1))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is False

    # A snapshot of the agent's provider taken after the outage began reflects it.
    _set_provider_snapshot_at(resolver, "docker", onset + timedelta(seconds=1))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is True


def test_classification_trustworthiness_is_scoped_to_the_workspaces_own_provider() -> None:
    """A fresh snapshot of an *unrelated* provider must not make the verdict trustworthy.

    Each provider is discovered on its own loop, so only the machine's own
    provider's snapshot can establish that its host was re-observed post-onset.
    """
    resolver = MngrCliBackendResolver()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    _register_workspace_agent(resolver, agent_id, "docker")
    onset = _drive_to_stuck_with_onset(tracker, agent_id)

    # A post-onset snapshot for a different provider leaves docker's freshness stale.
    _set_provider_snapshot_at(resolver, "modal", onset + timedelta(seconds=1))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is False

    # Only the agent's own provider going fresh post-onset makes it trustworthy.
    _set_provider_snapshot_at(resolver, "docker", onset + timedelta(seconds=1))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is True


def test_classification_trustworthiness_without_onset_falls_back_to_age() -> None:
    """Without a recorded onset, trustworthiness falls back to the absolute-age freshness check.

    Only the force-``mark_stuck`` path (used in tests) reaches STUCK without a
    probe-failure run, so there is no onset to compare against; the predicate then
    behaves on age alone -- cold start is untrustworthy, a recent snapshot is
    trustworthy. A missing tracker is treated the same way.
    """
    resolver = MngrCliBackendResolver()
    tracker = SystemInterfaceHealthTracker()
    agent_id = AgentId.generate()
    _register_workspace_agent(resolver, agent_id, "docker")
    tracker.mark_stuck(agent_id)
    assert tracker.get_failure_run_started_wall_at(agent_id) is None

    # Cold start, no snapshot yet: untrustworthy.
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is False
    # A recent snapshot of the agent's provider is trustworthy via the age fallback.
    _set_provider_snapshot_at(resolver, "docker", datetime.now(timezone.utc))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is True
    # A stale snapshot (a stalled pipeline) is untrustworthy again.
    _set_provider_snapshot_at(resolver, "docker", datetime.now(timezone.utc) - timedelta(minutes=5))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is False
    # No tracker at all behaves identically to a missing onset.
    assert is_recovery_classification_trustworthy(resolver, None, agent_id) is False


# -- unattended recovery + the shared dispatch --


def _dispatcher(
    tracker: SystemInterfaceHealthTracker,
    resolver: MngrCliBackendResolver,
    registry: InMemoryWorkspaceOperationRegistry,
    concurrency_group: ConcurrencyGroup,
    mngr_binary: str,
    mngr_host_dir: Path,
) -> UnattendedRecoveryDispatcher:
    """The tracker callback wired in ``app.py``, built against test doubles."""
    return UnattendedRecoveryDispatcher(
        tracker=tracker,
        backend_resolver=resolver,
        registry=registry,
        concurrency_group=concurrency_group,
        mngr_binary=mngr_binary,
        mngr_host_dir=mngr_host_dir,
        mngr_forward_port=0,
        mngr_forward_preauth_cookie=None,
    )


def test_unattended_recovery_starts_a_wedged_machine_without_bouncing_it(tmp_path: Path) -> None:
    """The STUCK edge dispatches a start-only restart: ``mngr start`` runs, ``mngr stop`` does not.

    start_only is what makes it safe to fire unprompted -- it can cold-boot a
    stopped host but can never bounce a live one.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    with ConcurrencyGroup(name="test-unattended") as cg:
        dispatch = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path)
        dispatch(workspace_agent)
        is_started = _wait_for_mngr_invocation(mngr_binary, "start ")

    assert is_started, "the unattended dispatch must reach mngr start"
    assert not any(line.startswith("stop ") for line in _read_fake_mngr_invocations(mngr_binary))


def test_unattended_recovery_leaves_a_machine_the_user_stopped_alone(tmp_path: Path) -> None:
    """An in-app stop suppresses the dispatch, so it cannot undo the user's action.

    A stopped host's interface is unreachable, so the probe loop drives it
    STUCK exactly as a wedge would. Without the marker the app would start it
    straight back up -- and bill for it on a metered provider.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    tracker.suppress_unattended_recovery(workspace_agent)

    with ConcurrencyGroup(name="test-unattended") as cg:
        dispatch = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path)
        dispatch(workspace_agent)

    assert _read_fake_mngr_invocations(mngr_binary) == []
    assert tracker.get_health(workspace_agent) != AgentHealth.RESTARTING


def test_unattended_recovery_resumes_after_the_user_starts_the_machine_again(tmp_path: Path) -> None:
    """Starting from inside the app clears the suppression, so wedges self-heal again."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    tracker.suppress_unattended_recovery(workspace_agent)
    tracker.allow_unattended_recovery(workspace_agent)

    with ConcurrencyGroup(name="test-unattended") as cg:
        dispatch = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path)
        dispatch(workspace_agent)
        is_started = _wait_for_mngr_invocation(mngr_binary, "start ")

    assert is_started, "clearing the suppression must let a wedge reach mngr start again"


def test_a_forced_stuck_does_not_re_dispatch(tmp_path: Path) -> None:
    """Only the probe-confirmed HEALTHY -> STUCK edge dispatches, once per episode.

    A ``mark_stuck`` and the probe failures that keep arriving while the
    machine stays down re-report the state, but the edge has already fired;
    a dispatcher keyed on the state instead would restart on every lap.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    with ConcurrencyGroup(name="test-unattended") as cg:
        tracker.add_on_stuck_edge_callback(_dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path))
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        assert _wait_for_mngr_invocation(mngr_binary, "start "), "the outage edge must reach mngr start"
        started_count = len(_read_fake_mngr_invocations(mngr_binary))
        tracker.mark_stuck(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)

    assert len(_read_fake_mngr_invocations(mngr_binary)) == started_count


def test_dispatch_host_restart_does_not_stack_a_second_worker(tmp_path: Path) -> None:
    """A restart already in flight reports ALREADY_RUNNING instead of racing the first."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = InMemoryWorkspaceOperationRegistry()
    # The claim the first caller already won: the workspace's operation slot,
    # which is what owning a restart means.
    registry.start(workspace_agent, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))
    tracker.mark_restarting(workspace_agent, start_only=True)

    with ConcurrencyGroup(name="test-unattended") as cg:
        outcome = dispatch_host_restart(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            registry=registry,
            concurrency_group=cg,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            skip_stop=True,
        )

    assert outcome == RestartDispatchOutcome.ALREADY_RUNNING


def test_probe_failures_alone_drive_a_wedged_machine_back_up(tmp_path: Path) -> None:
    """End to end through the real wiring: probe failures -> STUCK -> ``mngr start``.

    The tracker is wired exactly as ``app.py`` wires it and then driven only by
    probe results, so nothing but sustained unreachability triggers the restart.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    with ConcurrencyGroup(name="test-unattended") as cg:
        tracker.add_on_stuck_edge_callback(_dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path))
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        is_started = _wait_for_mngr_invocation(mngr_binary, "start ")

    assert is_started, "sustained probe failure must reach mngr start without anyone asking"
    assert not any(line.startswith("stop ") for line in _read_fake_mngr_invocations(mngr_binary))


def test_unattended_recovery_never_takes_a_backup_operation_s_slot(tmp_path: Path) -> None:
    """A restore stops the machine's services, so the wedge it produces must not evict it.

    ``registry.start`` replaces the workspace's record, so a restart that
    claimed the slot mid-restore would strand the restore's poller and let the
    restore worker's terminal complete/fail land on the restart's record --
    reporting a restart that never ran as done.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    registry.start(workspace_agent, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))

    with ConcurrencyGroup(name="test-unattended") as cg:
        tracker.add_on_stuck_edge_callback(_dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path))
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)

    assert _read_fake_mngr_invocations(mngr_binary) == []
    record = registry.get(workspace_agent)
    assert record is not None and record.kind == WorkspaceOperationKind.BACKUP_RESTORE
    assert record.status == WorkspaceOperationStatus.RUNNING
    assert tracker.get_health(workspace_agent) != AgentHealth.RESTARTING


def test_dispatch_host_restart_reports_a_conflicting_operation(tmp_path: Path) -> None:
    """The route turns this outcome into its 409, so the enum has to name the case."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = InMemoryWorkspaceOperationRegistry()
    registry.start(workspace_agent, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc))

    with ConcurrencyGroup(name="test-unattended") as cg:
        outcome = dispatch_host_restart(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            registry=registry,
            concurrency_group=cg,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            skip_stop=False,
        )

    assert outcome == RestartDispatchOutcome.OPERATION_CONFLICT


class _RegistryWithSlotStolenMidClaim(InMemoryWorkspaceOperationRegistry):
    """A registry where a backup operation claims the slot during the restart's own claim.

    Stands in for the interleaving a non-atomic claim would lose to: a backup
    dispatch claiming from a request thread, against a restart dispatched from
    the probe thread.
    """

    _thief_agent_id: AgentId | None = PrivateAttr(default=None)

    def steal_slot_on_next_claim(self, agent_id: AgentId) -> None:
        self._thief_agent_id = agent_id

    def start_if_idle(
        self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime, target: str | None
    ) -> bool:
        if self._thief_agent_id == agent_id:
            self._thief_agent_id = None
            super().start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now)
        return super().start_if_idle(agent_id, kind, now, target)


def test_a_backup_that_claims_the_slot_first_is_not_evicted_by_the_restart(tmp_path: Path) -> None:
    """The restart must lose the slot it did not win, not overwrite the winner's record.

    ``registry.start`` *replaces* the record, so a restart that read an idle
    slot and then filled it unconditionally would strand the restore's poller
    and let the restore worker's terminal complete/fail land on the restart's
    record -- reporting a restart that never ran as done. One atomic claim is
    what closes that window, so this drives a backup into the middle of it.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = _RegistryWithSlotStolenMidClaim()
    registry.steal_slot_on_next_claim(workspace_agent)

    with ConcurrencyGroup(name="test-unattended") as cg:
        outcome = dispatch_host_restart(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            registry=registry,
            concurrency_group=cg,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            skip_stop=True,
        )

    assert outcome == RestartDispatchOutcome.OPERATION_CONFLICT
    record = registry.get(workspace_agent)
    assert record is not None and record.kind == WorkspaceOperationKind.BACKUP_RESTORE
    assert record.status == WorkspaceOperationStatus.RUNNING
    # Nothing of the restart may survive its lost claim: no worker, and no
    # RESTARTING the recovery surfaces would render over the restore.
    assert _read_fake_mngr_invocations(mngr_binary) == []
    assert tracker.get_health(workspace_agent) != AgentHealth.RESTARTING


def test_a_machine_that_answers_is_never_restarted(tmp_path: Path) -> None:
    """The other half of the edge: a reachable machine stays untouched."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    with ConcurrencyGroup(name="test-unattended") as cg:
        tracker.add_on_stuck_edge_callback(_dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path))
        tracker.record_failure(workspace_agent)
        tracker.record_probe_success(workspace_agent)

    assert _read_fake_mngr_invocations(mngr_binary) == []


# -- backend-unreachable verdict (the poll-cheap read) --


def _write_fake_mngr_with_provider_outage(
    tmp_path: Path, failing_subcommand: str, provider_name: str = "docker"
) -> tuple[str, str]:
    """Write an mngr stub whose ``failing_subcommand`` fails the way an unreachable backend does.

    Returns ``(binary_path, reason)``. The stderr is rendered from a real
    ``ProviderUnavailableError`` -- exactly what mngr prints when it cannot reach
    the provider -- so this exercises the shared message shape rather than a
    hand-copied literal. The message is written to a sibling file and ``cat``ed so
    the quotes it embeds around the provider name survive the shell stub.

    ``provider_name`` is the provider the command fails at, which defaults to the
    one the test resolvers put the workspace on. A command aborts on whichever
    provider it queried is unavailable, so passing another one is how a test
    poses an outage that is not this machine's.
    """
    reason = "could not reach the provider backend: [Errno 8] nodename nor servname provided, or not known"
    error = ProviderUnavailableError(ProviderInstanceName(provider_name), reason)
    message_path = tmp_path / f"provider_outage_{failing_subcommand}_{provider_name}.txt"
    message_path.write_text(f"Error: {error}\n")
    script = tmp_path / f"fake_mngr_provider_outage_{failing_subcommand}_{provider_name}"
    mngr_binary = _write_mngr_stub(
        script, f"  {failing_subcommand}) cat {shlex.quote(str(message_path))} >&2; exit 1 ;;\n"
    )
    return mngr_binary, reason


def test_in_band_provider_outage_answers_only_for_a_known_matching_provider() -> None:
    """A command rejected at some other provider is not this machine's backend going down.

    The unknown-provider case is the one that matters: mngr aborts on whichever
    provider it queried turns out to be unavailable, so a caller that could not
    resolve its own provider (discovery lost the host across the restart, or has
    not surfaced it yet) has nothing to compare against -- and adopting the
    outage anyway would withhold a restart from a machine whose backend is fine.
    """
    reason = "could not reach Imbue Cloud: [Errno 8] nodename nor servname provided, or not known"
    error = ProviderUnavailableError(ProviderInstanceName("imbue_cloud_someone-imbue-com"), reason)
    exc = MngrCommandError(f"exited 1: Error: {error}")

    assert _in_band_provider_outage_reason(exc, "imbue_cloud_someone-imbue-com") == reason
    assert _in_band_provider_outage_reason(exc, "docker") is None
    assert _in_band_provider_outage_reason(exc, None) is None


def test_restart_step_failure_names_the_step_when_the_machines_provider_is_unknown() -> None:
    """A restart step that failed at an unidentifiable provider stays a failed step.

    The stop step can drop the host out of discovery, so the start step's failure
    is read with no display info to resolve a provider from. Reporting the foreign
    outage the stderr happens to name would tell the user their machine's backend
    is down when it is another account's cloud that is -- and would leave that
    claim on the tracker for the rest of the episode.
    """
    reason = "could not reach Imbue Cloud: [Errno 8] nodename nor servname provided, or not known"
    error = ProviderUnavailableError(ProviderInstanceName("imbue_cloud_someone-imbue-com"), reason)
    exc = MngrCommandError(f"exited 1: Error: {error}")
    workspace_agent = AgentId.generate()
    tracker = SystemInterfaceHealthTracker()

    with capture_error_logs():
        _report_restart_step_failure(
            "Start",
            exc,
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
            registry=_started_registry(workspace_agent),
        )

    message = tracker.get_last_restart_error(workspace_agent) or ""
    assert message.startswith("Start step of host restart failed:")
    assert "This machine's backend is unreachable" not in message
    assert tracker.get_backend_outage(workspace_agent) is None


def test_backend_unreachable_verdict_surfaces_the_providers_own_error() -> None:
    """A surfaced provider error is the verdict, carrying that provider's own words.

    The message is shown verbatim precisely so minds never has to hand-author a
    sentence per provider failure mode.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    record_provider_discovery_error(
        resolver, "docker", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    )

    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=None)

    assert verdict is not None
    assert verdict.provider_label == "Docker"
    assert verdict.reason == "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"


def test_backend_unreachable_verdict_withholds_a_provider_error_from_a_previous_episode() -> None:
    """A provider error latched before this outage began must not explain it.

    ``get_provider_errors()`` holds a provider's last reported error until its
    next poll lands, so a backend that failed and then recovered keeps an error
    on the books for up to a full poll interval. A machine that wedges in that
    window (for reasons entirely its own) would otherwise be reported as
    "Can't connect to Docker" on the strength of a snapshot taken before it
    wedged -- naming a backend that is, by then, answering fine.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent, host_state=HostState.RUNNING)
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    onset = _drive_to_stuck_with_onset(tracker, workspace_agent)
    reason = "Docker Desktop is manually paused."
    record_provider_discovery_error(resolver, "docker", reason, last_snapshot_at=onset - timedelta(seconds=1))

    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker) is None

    # The next poll of that provider settles it: if the backend really is down,
    # its error is now an observation of *this* outage and speaks again.
    record_provider_discovery_error(resolver, "docker", reason, last_snapshot_at=onset + timedelta(seconds=1))
    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker)
    assert verdict is not None
    assert verdict.reason == reason


def test_backend_unreachable_verdict_is_none_while_the_backend_answers() -> None:
    """No provider error and a reachable host is not a verdict this read can make.

    This read only ever answers "is the backend unreachable?", and a wedged but
    reachable container looks identical to a healthy one here -- it is the probe
    loop that settles that -- so silence must not be mistaken for a healthy
    machine.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent, host_state=HostState.RUNNING)

    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=None) is None


def test_backend_unreachable_verdict_covers_a_trusted_rejected_host() -> None:
    """A trustworthily UNAUTHENTICATED host is a backend verdict, with the canned reason.

    Discovery carries no per-host failure detail for this state, and a restart
    routes through the same rejected credential -- which is exactly why it must
    not read as an ordinary unresponsive machine.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(
        workspace_agent, services_agent, host_state=HostState.UNAUTHENTICATED
    )
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    onset = _drive_to_stuck_with_onset(tracker, workspace_agent)
    _set_provider_snapshot_at(resolver, "docker", onset + timedelta(seconds=1))

    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker)

    assert verdict is not None
    assert verdict.reason == HOST_ACCESS_REJECTED_REASON


def test_backend_unreachable_verdict_withholds_an_untrusted_rejected_host() -> None:
    """A pre-onset snapshot cannot carry the rejected-host verdict.

    Same freshness gate the probe's host-state verdicts use: this state steers
    the card away from offering a restart, so a stale reading must not.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(
        workspace_agent, services_agent, host_state=HostState.UNAUTHENTICATED
    )
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    onset = _drive_to_stuck_with_onset(tracker, workspace_agent)
    _set_provider_snapshot_at(resolver, "docker", onset - timedelta(seconds=1))

    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker) is None


def test_run_restart_sequence_reports_the_backend_when_the_start_is_rejected_there(tmp_path: Path) -> None:
    """A start that mngr rejected at the provider is reported as a backend outage.

    The command never reached the host, so naming the start step would blame the
    machine for its backend being down -- and send the reader looking in the
    wrong place. The provider's own reason is surfaced instead.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary, reason = _write_fake_mngr_with_provider_outage(tmp_path, "start")

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    message = tracker.get_last_restart_error(workspace_agent) or ""
    assert reason in message
    assert "Start step" not in message
    assert len(error_records) == 1, error_records


def _run_restart_rejected_at_the_backend(
    tmp_path: Path, workspace_agent: AgentId, tracker: SystemInterfaceHealthTracker, resolver: MngrCliBackendResolver
) -> str:
    """Run the restart the tracker's stuck edge dispatches, against a backend that refuses it.

    Returns the provider's own reason. This is the sequence the app runs
    unattended the moment a machine wedges, so it is what has happened by the
    time any recovery surface is on screen.
    """
    mngr_binary, reason = _write_fake_mngr_with_provider_outage(tmp_path, "start")
    with ConcurrencyGroup(name="test-restart-rejected") as cg, capture_error_logs():
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
            skip_stop=True,
        )
    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    return reason


def test_a_restart_rejected_at_the_backend_is_the_verdict_without_waiting_for_a_poll(tmp_path: Path) -> None:
    """The rejection names the backend on the edge that raises the card, not a poll later.

    Discovery has surfaced nothing about this provider -- the outage is seconds
    old, and its next poll can be half a minute away. The rejected restart is the
    only observation there is, and it is the same observation discovery will
    eventually make, so the card opens on "Can't connect to Docker" instead of
    opening on "unresponsive" with a Restart button routed through the backend
    that just refused one, and correcting itself while the user reads it.
    """
    workspace_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, AgentId.generate(), host_state=HostState.RUNNING)
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    _drive_to_stuck_with_onset(tracker, workspace_agent)

    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker) is None

    reason = _run_restart_rejected_at_the_backend(tmp_path, workspace_agent, tracker, resolver)

    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker)
    assert verdict is not None
    assert verdict.provider_label == "Docker"
    assert verdict.reason == reason


def test_a_rejected_restarts_verdict_lasts_until_the_backend_is_next_polled(tmp_path: Path) -> None:
    """The next poll of that provider settles it, whichever way it reads.

    A rejection is only the freshest word on the backend until discovery gets
    one of its own. A poll that clears the provider must therefore end the
    claim: the backend is answering again, and a machine still wedged after that
    is wedged for reasons of its own -- reporting it as "Can't connect to Docker"
    for the rest of the episode would be the same stale-evidence mistake the
    freshness gate exists to prevent, made from the other side. A poll that
    still errors keeps the verdict, now on the resolver's own evidence.
    """
    workspace_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, AgentId.generate(), host_state=HostState.RUNNING)
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    _drive_to_stuck_with_onset(tracker, workspace_agent)
    reason = _run_restart_rejected_at_the_backend(tmp_path, workspace_agent, tracker, resolver)

    # Still down when discovery next looks: the same verdict, now off the poll.
    record_provider_discovery_error(resolver, "docker", reason)
    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker)
    assert verdict is not None
    assert verdict.reason == reason

    # Back up when discovery next looks: nothing left for the rejection to say.
    _set_provider_snapshot_at(resolver, "docker", datetime.now(timezone.utc))
    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker) is None


# -- the this-device-cannot-connect verdict --


@pytest.mark.parametrize(
    "reason",
    [
        SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED,
        SystemInterfaceBackendFailureReason.POOL_EXHAUSTED,
    ],
)
def test_device_verdict_covers_every_failure_raised_before_the_backend_was_dialed(
    reason: SystemInterfaceBackendFailureReason,
) -> None:
    """Both causes that never reached the network read as this device's fault.

    They are not separated for the user: an app restart is the remedy for both
    and the only one available, so a second card would be a distinction with no
    action behind it. The verbatim error travels so the card can show what
    actually broke.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.record_connection_failure(workspace_agent, reason, "the exact error")

    verdict = read_device_cannot_connect_verdict(workspace_agent, tracker=tracker)

    assert verdict is not None
    assert verdict.detail == "the exact error"


@pytest.mark.parametrize(
    "reason",
    [
        # Reached the network and failed there: the workspace is still implicated.
        SystemInterfaceBackendFailureReason.CONNECT_ERROR,
        # The host answered and refused the inner port -- about the workspace,
        # not this device.
        SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING,
    ],
)
def test_device_verdict_is_withheld_for_a_failure_that_reached_the_network(
    reason: SystemInterfaceBackendFailureReason,
) -> None:
    """Only a failure raised against this device's own resources clears it.

    Anything that reached the network leaves the workspace's own reachability
    open, and claiming otherwise would suppress the restart that fixes it.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.record_connection_failure(workspace_agent, reason, "boom")

    assert read_device_cannot_connect_verdict(workspace_agent, tracker=tracker) is None


def test_device_verdict_outranks_the_restart_episodes_own_conclusion(tmp_path: Path) -> None:
    """A restart that ran and failed does not displace the verdict that explains it.

    The app restarts a machine that stops answering without being asked, so a
    device-side fault produces RESTARTING and then RESTART_FAILED all by itself.
    Reporting those would blame the machine for the app's own broken connection,
    which is the whole misdiagnosis this verdict exists to stop.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.record_connection_failure(
        workspace_agent, SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED, "no known_hosts"
    )

    tracker.mark_stuck(workspace_agent)
    tracker.mark_restarting(workspace_agent, start_only=True)
    assert read_device_cannot_connect_verdict(workspace_agent, tracker=tracker) is not None

    tracker.mark_restart_failed(workspace_agent, "The system interface did not respond.")
    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert read_device_cannot_connect_verdict(workspace_agent, tracker=tracker) is not None


def test_device_verdict_clears_the_moment_the_machine_answers() -> None:
    """A probe that reaches the machine settles it: the connection works now."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.record_connection_failure(
        workspace_agent, SystemInterfaceBackendFailureReason.POOL_EXHAUSTED, "pool timeout"
    )

    tracker.record_probe_success(workspace_agent)

    assert read_device_cannot_connect_verdict(workspace_agent, tracker=tracker) is None


def test_device_verdict_is_none_without_a_tracker() -> None:
    """No tracker is no evidence, not a verdict."""
    assert read_device_cannot_connect_verdict(AgentId.generate(), tracker=None) is None


# -- a start that booted nothing --


def test_a_start_that_booted_nothing_is_recorded_against_the_episode(tmp_path: Path) -> None:
    """A no-op start that leaves the machine unreachable must not read as a failed restart.

    The unattended dispatch fires ``mngr start`` at any machine that stops
    answering, and that start is idempotent: against a host that is already up
    it does nothing at all. The tracker still reaches RESTART_FAILED -- the
    machine really did not come back -- but the surfaces read the recorded
    no-op and report the machine as unresponsive instead of blaming a restart
    that never ran.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker = SystemInterfaceHealthTracker()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs():
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, was_host_started=False),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            # Port 1 refuses every probe, so the readiness wait runs out: the
            # machine stayed unreachable after a start that started nothing.
            mngr_forward_port=1,
            mngr_forward_preauth_cookie="cookie",
            registry=_started_registry(workspace_agent),
            skip_stop=True,
            startup_wait_seconds=0.1,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert tracker.is_restart_a_no_op(workspace_agent) is True


def test_a_start_that_really_booted_the_host_keeps_the_restart_framing(tmp_path: Path) -> None:
    """A cold boot that did not converge *is* a failed restart, and still reads as one."""
    workspace_agent = AgentId.generate()
    tracker = SystemInterfaceHealthTracker()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, AgentId.generate())

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs():
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, was_host_started=True),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=1,
            mngr_forward_preauth_cookie="cookie",
            registry=_started_registry(workspace_agent),
            skip_stop=True,
            startup_wait_seconds=0.1,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert tracker.is_restart_a_no_op(workspace_agent) is False


# -- post-restart readiness wait --


def test_restart_readiness_budget_covers_a_cold_boot() -> None:
    """The restart's readiness budget is the calibrated cold-boot budget, not its own guess.

    ``mngr start`` cold-boots the container, so the restart worker waits for
    exactly the event the create flow already measures. Sizing both from one
    constant is what keeps an ordinary slow restart from tripping the failure
    branch: the budget was independently set to 30s against a cold boot that
    regularly runs 90-180s, so a workspace that was merely slow got reported --
    at error level, to error reporting -- as a failed restart.
    """
    assert _HOST_RESTART_STARTUP_WAIT_SECONDS == WORKSPACE_READY_TIMEOUT_SECONDS


def test_run_restart_sequence_recovers_a_workspace_that_boots_slowly(tmp_path: Path) -> None:
    """A workspace that only answers after several polls recovers rather than failing.

    The interface stays 503 for the first two polls and answers on the third, so
    the wait must keep polling across them and end in HEALTHY with the operation
    DONE -- and, crucially, log no error: a slow boot is not a failed restart.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)

    with scripted_workspace_probe_server(not_ready_count=2) as port:
        with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
            run_restart_sequence(
                workspace_agent_id=workspace_agent,
                tracker=tracker,
                backend_resolver=resolver,
                mngr_binary=_write_fake_mngr(tmp_path),
                mngr_host_dir=tmp_path,
                concurrency_group=cg,
                mngr_forward_port=port,
                mngr_forward_preauth_cookie="cookie",
                registry=registry,
                skip_stop=True,
                # Comfortably past the ~2s the scripted boot takes, so the
                # budget is not what ends the wait.
                startup_wait_seconds=30.0,
            )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.DONE
    assert error_records == []


def test_await_system_interface_ready_reports_a_slow_boot_that_answers() -> None:
    """The wait polls past not-ready responses and reports READY on the first 200."""
    with scripted_workspace_probe_server(not_ready_count=2) as port:
        with ConcurrencyGroup(name="test-wait") as cg:
            outcome = _await_system_interface_ready(str(HostId.generate()), port, "cookie", 30.0, concurrency_group=cg)
    assert outcome is RestartReadinessOutcome.READY


def test_await_system_interface_ready_times_out_when_nothing_ever_answers() -> None:
    """A budget that elapses with no answer is a TIMED_OUT verdict (the real failure)."""
    with ConcurrencyGroup(name="test-wait") as cg:
        # Port 1 refuses connections, so every poll fails fast.
        outcome = _await_system_interface_ready(str(HostId.generate()), 1, "cookie", 0.1, concurrency_group=cg)
    assert outcome is RestartReadinessOutcome.TIMED_OUT


def test_await_system_interface_ready_gives_up_promptly_on_shutdown() -> None:
    """A shutdown cuts the wait short well inside the cold-boot budget, and is not a timeout.

    The budget spans a full cold boot -- far longer than the concurrency group's
    ~10s exit budget -- so a quit during a restart must not leave this thread
    parked until the budget expires. Sleeping on the shutdown event rather than a
    bare timer is what bounds it.
    """
    with ConcurrencyGroup(name="test-wait") as cg:
        cg.shutdown()
        started = time.monotonic()
        # Port 1 refuses connections, so only the shutdown check can end this.
        outcome = _await_system_interface_ready(
            str(HostId.generate()), 1, "cookie", _HOST_RESTART_STARTUP_WAIT_SECONDS, concurrency_group=cg
        )
        elapsed = time.monotonic() - started

    assert outcome is RestartReadinessOutcome.ABANDONED
    assert elapsed < 5.0, f"the wait ran {elapsed:.1f}s past a shutdown"


def test_run_restart_sequence_does_not_report_a_failure_when_shutdown_cuts_it_short(tmp_path: Path) -> None:
    """A restart truncated by app shutdown is not reported as a failed restart.

    Shutdown says nothing about whether the workspace recovered, and the tracker
    and operation registry are per-process, so there is nothing left to render a
    verdict to. Claiming RESTART_FAILED here would report a failure that was
    never observed -- and would reach error reporting on every quit that lands
    mid-restart.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)

    with ConcurrencyGroup(name="test-restart") as cg:
        # Quit at the first readiness probe: by then ``mngr start`` has already
        # run, so this lands the shutdown inside the wait -- the real ordering --
        # rather than before the sequence's subprocess steps.
        with scripted_workspace_probe_server(not_ready_count=10**6, on_first_request=cg.shutdown) as port:
            with capture_error_logs() as error_records:
                run_restart_sequence(
                    workspace_agent_id=workspace_agent,
                    tracker=tracker,
                    backend_resolver=resolver,
                    mngr_binary=_write_fake_mngr(tmp_path),
                    mngr_host_dir=tmp_path,
                    concurrency_group=cg,
                    mngr_forward_port=port,
                    mngr_forward_preauth_cookie="cookie",
                    registry=registry,
                    skip_stop=True,
                    startup_wait_seconds=_HOST_RESTART_STARTUP_WAIT_SECONDS,
                )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTARTING
    assert error_records == []
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.RUNNING
