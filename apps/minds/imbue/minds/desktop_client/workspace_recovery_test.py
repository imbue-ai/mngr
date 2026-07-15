"""Unit coverage for the workspace-recovery engine (host-health probe + restart worker).

These exercise the building blocks behind ``GET /api/v1/workspaces/<id>/health``
and ``POST /api/v1/workspaces/<id>/restart`` directly, complementing the
end-to-end route tests in ``api_v1_test.py`` with the granular restart-sequence
failure modes (unresolved system-services agent, stop/start command failures,
the host-already-stopped fast path).
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.recovery_probe import DispatchTier
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import capture_error_logs
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.desktop_client.workspace_recovery import _build_mngr_start_argv
from imbue.minds.desktop_client.workspace_recovery import _build_mngr_stop_argv
from imbue.minds.desktop_client.workspace_recovery import _is_discovery_fresh
from imbue.minds.desktop_client.workspace_recovery import _provider_error_message_for_workspace
from imbue.minds.desktop_client.workspace_recovery import is_recovery_classification_trustworthy
from imbue.minds.desktop_client.workspace_recovery import is_workspace_discovery_stalled
from imbue.minds.desktop_client.workspace_recovery import probe_workspace_health
from imbue.minds.desktop_client.workspace_recovery import run_restart_sequence
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.errors import HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName


def _write_fake_mngr(tmp_path: Path, stop_exit: int = 0, start_exit: int = 0) -> str:
    """Write an executable stub that stands in for the ``mngr`` binary.

    Exits per-subcommand so a test can simulate a failing stop or start
    without a real mngr / provider. Every invocation appends its argv to a
    ``<script>.log`` sibling file so a test can assert which subcommands ran
    (e.g. that the stop step was skipped).
    """
    script = tmp_path / "fake_mngr"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$0.log"\n'
        f'case "$1" in\n  stop) exit {stop_exit} ;;\n  start) exit {start_exit} ;;\n  *) exit 0 ;;\nesac\n'
    )
    script.chmod(0o755)
    return str(script)


def _read_fake_mngr_invocations(mngr_binary: str) -> list[str]:
    """Return the recorded argv lines for a ``_write_fake_mngr`` stub (empty if never invoked)."""
    log_path = Path(mngr_binary + ".log")
    if not log_path.exists():
        return []
    return log_path.read_text().splitlines()


def _resolver_with_system_services(
    workspace_agent: AgentId, services_agent: AgentId, host_state: HostState | None = None
) -> MngrCliBackendResolver:
    """Build a resolver where the workspace agent and system-services agent share a host.

    ``host_state`` records an observed lifecycle state for that shared host in
    the snapshot; None leaves the host state undiscovered.
    """
    host_id = HostId.generate()
    resolver = MngrCliBackendResolver()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, services_agent),
            discovered_agents=(
                DiscoveredAgent(
                    host_id=host_id,
                    agent_id=workspace_agent,
                    agent_name=AgentName("my-claude-agent"),
                    provider_name=ProviderInstanceName("docker"),
                ),
                DiscoveredAgent(
                    host_id=host_id,
                    agent_id=services_agent,
                    agent_name=AgentName("system-services"),
                    provider_name=ProviderInstanceName("docker"),
                ),
            ),
            host_state_by_host_id=({str(host_id): host_state} if host_state is not None else {}),
        )
    )
    return resolver


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


def test_build_mngr_start_argv_targets_the_agent() -> None:
    aid = AgentId.generate()
    argv = _build_mngr_start_argv("/usr/local/bin/mngr", aid)
    assert argv[:3] == ["/usr/local/bin/mngr", "start", str(aid)]


# -- provider-error attribution --


def test_provider_error_message_for_workspace_keys_on_this_workspaces_provider() -> None:
    """The provider error message is attributed by exact provider name.

    This is the per-provider keying that keeps a docker mind's recovery from
    being misclassified during a simultaneous imbue_cloud outage: only an error
    whose provider name matches this workspace's is used.
    """
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="could not reach Imbue Cloud",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    matched = _provider_error_message_for_workspace(errors, "imbue_cloud_acme")
    assert matched == "could not reach Imbue Cloud"


def test_provider_error_message_for_workspace_ignores_other_providers() -> None:
    """An error for a different provider is never blamed on this workspace."""
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="down",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, "docker") is None


def test_provider_error_message_for_workspace_is_none_when_provider_unknown() -> None:
    """Pre-discovery (provider unknown), we cannot attribute any error to this workspace."""
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="down",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, None) is None


# -- restart worker --


def test_run_restart_sequence_fails_when_system_services_agent_is_unresolved(tmp_path: Path) -> None:
    """With no system-services agent discovered, the sequence ends in RESTART_FAILED."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent)
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
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)

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
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)

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
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)

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
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)
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
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)
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
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)
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


def test_run_restart_sequence_skips_stop_when_host_already_stopped(tmp_path: Path) -> None:
    """``skip_stop=True`` on a host restart goes straight to ``mngr start`` (no stop subprocess)."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)
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
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)
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
    """Register one workspace agent on ``provider_name`` so its display info resolves a provider.

    Trustworthiness is scoped to the workspace's own provider's last snapshot, so
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
    assert _is_discovery_fresh(now) is True
    # A snapshot well past the freshness window (a stalled pipeline) is stale.
    assert _is_discovery_fresh(now - timedelta(minutes=5)) is False
    # No snapshot at all (e.g. before initial discovery) cannot be trusted.
    assert _is_discovery_fresh(None) is False


def test_classification_trustworthy_only_after_a_post_onset_snapshot() -> None:
    """A verdict is trustworthy only once a snapshot taken *after* the outage began has landed.

    A snapshot that predates the outage still carries the pre-outage host state (a
    just-stopped container still reads RUNNING), so it must not make the
    classification trustworthy -- only a snapshot at or after the outage onset
    does. Freshness is scoped to the workspace's own provider's snapshot time.
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

    Each provider is discovered on its own loop, so only the workspace's own
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


def test_discovery_stalled_when_no_snapshot_or_snapshot_is_old() -> None:
    """Discovery is stalled when the workspace's provider has no (or only an old) snapshot.

    This is the dead-producer condition (e.g. the ``mngr latchkey forward``
    supervisor died): the onset-based freshness gate can never open again, so the
    probe path uses this predicate to stop waiting and gather direct evidence.
    """
    resolver = MngrCliBackendResolver()
    agent_id = AgentId.generate()
    _register_workspace_agent(resolver, agent_id, "docker")

    # No snapshot for the provider at all: stalled.
    assert is_workspace_discovery_stalled(resolver, agent_id) is True
    # A recent snapshot: flowing normally.
    _set_provider_snapshot_at(resolver, "docker", datetime.now(timezone.utc))
    assert is_workspace_discovery_stalled(resolver, agent_id) is False
    # Several missed polls (providers poll every 30s): stalled.
    _set_provider_snapshot_at(resolver, "docker", datetime.now(timezone.utc) - timedelta(minutes=5))
    assert is_workspace_discovery_stalled(resolver, agent_id) is True


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


# -- host-health probe: classification-time consistency --


class _HostStateFlipResolver(MngrCliBackendResolver):
    """Resolver whose host state flips RUNNING -> STOPPED across successive reads.

    Emulates a fresh discovery snapshot landing while the slow in-container exec
    is in flight: the pre-exec host-state read sees the stale RUNNING; every read
    after that sees the fresh STOPPED.
    """

    _host_state_reads: int = PrivateAttr(default=0)

    def get_host_state(self, host_id: HostId) -> HostState | None:
        self._host_state_reads += 1
        return HostState.RUNNING if self._host_state_reads == 1 else HostState.STOPPED


def _register_workspace_with_services(
    resolver: MngrCliBackendResolver, workspace_agent: AgentId, services_agent: AgentId, provider_name: str
) -> None:
    """Register a workspace agent and its system-services agent on one shared host."""
    host_id = HostId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, services_agent),
            discovered_agents=(
                DiscoveredAgent(
                    host_id=host_id,
                    agent_id=workspace_agent,
                    agent_name=AgentName("ws-agent"),
                    provider_name=ProviderInstanceName(provider_name),
                    certified_data={"labels": {"workspace": "true", "is_primary": "true"}},
                ),
                DiscoveredAgent(
                    host_id=host_id,
                    agent_id=services_agent,
                    agent_name=AgentName("system-services"),
                    provider_name=ProviderInstanceName(provider_name),
                ),
            ),
        )
    )


def test_probe_pairs_the_classified_host_state_with_the_freshness_gate(tmp_path: Path) -> None:
    """A snapshot landing mid-exec must not split the verdict from its evidence.

    The in-container exec takes tens of seconds. If a fresh discovery snapshot
    lands during it, the freshness gate (evaluated after the exec) sees a
    post-onset snapshot time -- but the host state read *before* the exec still
    holds the pre-snapshot value. Classifying that pair rendered a trusted
    HOST_UNRESPONSIVE off a stale RUNNING when the very snapshot that opened the
    gate already read STOPPED. The probe must classify the host state as re-read
    at gate time: HOST_OFFLINE.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _HostStateFlipResolver()
    _register_workspace_with_services(resolver, workspace_agent, services_agent, "docker")
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    onset = _drive_to_stuck_with_onset(tracker, workspace_agent)
    # The mid-exec snapshot: post-onset, so the gate opens.
    _set_provider_snapshot_at(resolver, "docker", onset + timedelta(seconds=1))

    with ConcurrencyGroup(name="test-probe") as cg:
        response = probe_workspace_health(
            workspace_agent,
            backend_resolver=resolver,
            tracker=tracker,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            envelope_stream_consumer=None,
        )

    # Two reads happened: the pre-exec gate read (RUNNING, so the exec ran) and
    # the classification-time read (STOPPED).
    assert resolver._host_state_reads >= 2
    assert response.dispatch_tier == DispatchTier.HOST_OFFLINE


def test_probe_attempts_exec_and_resolves_when_discovery_is_stalled(tmp_path: Path) -> None:
    """With a stalled discovery stream, the probe gathers direct evidence instead of waiting.

    The dead-producer dead-end: no snapshot for the workspace's provider means
    the resolver has no (trusted) host state and the freshness gate can never
    open, so the old behavior re-classified INDETERMINATE forever. The probe must
    attempt the in-container exec despite the host not reading RUNNING; the
    exec's completed failure (the stub exits 0 with no sentinel) then resolves to
    the consent-gated HOST_UNRESPONSIVE, whose restart also revives a genuinely
    stopped container.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_with_system_services(workspace_agent, services_agent)
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    _drive_to_stuck_with_onset(tracker, workspace_agent)
    # No provider snapshot is ever recorded: discovery is stalled.
    assert is_workspace_discovery_stalled(resolver, workspace_agent) is True

    mngr_binary = _write_fake_mngr(tmp_path)
    with ConcurrencyGroup(name="test-probe-stalled") as cg:
        response = probe_workspace_health(
            workspace_agent,
            backend_resolver=resolver,
            tracker=tracker,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            envelope_stream_consumer=None,
        )

    exec_invocations = [line for line in _read_fake_mngr_invocations(mngr_binary) if line.startswith("exec ")]
    assert exec_invocations, "the exec probe must be attempted when discovery is stalled"
    assert response.dispatch_tier == DispatchTier.HOST_UNRESPONSIVE


def test_probe_skips_exec_for_a_trusted_not_running_host(tmp_path: Path) -> None:
    """With discovery flowing and the host trustworthily observed STOPPED, no exec fires.

    The doomed-round-trip guard: a fresh post-onset snapshot already answers the
    question, so the probe classifies HOST_OFFLINE (unattended restart) without
    paying the exec's provider round-trip.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_with_system_services(workspace_agent, services_agent, host_state=HostState.STOPPED)
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    onset = _drive_to_stuck_with_onset(tracker, workspace_agent)
    _set_provider_snapshot_at(resolver, "docker", onset + timedelta(seconds=1))

    mngr_binary = _write_fake_mngr(tmp_path)
    with ConcurrencyGroup(name="test-probe-skip-exec") as cg:
        response = probe_workspace_health(
            workspace_agent,
            backend_resolver=resolver,
            tracker=tracker,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            envelope_stream_consumer=None,
        )

    assert _read_fake_mngr_invocations(mngr_binary) == []
    assert response.dispatch_tier == DispatchTier.HOST_OFFLINE
