"""Unit coverage for the workspace-recovery engine (host-health probe + restart worker).

These exercise the building blocks behind ``GET /api/v1/workspaces/<id>/health``
and ``POST /api/v1/workspaces/<id>/restart`` directly, complementing the
end-to-end route tests in ``api_v1_test.py`` with the granular restart-sequence
failure modes (unresolved system-services agent, stop/start command failures,
the host-already-stopped fast path).
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.desktop_client.workspace_recovery import _build_mngr_start_argv
from imbue.minds.desktop_client.workspace_recovery import _build_mngr_stop_argv
from imbue.minds.desktop_client.workspace_recovery import _provider_error_message_for_workspace
from imbue.minds.desktop_client.workspace_recovery import run_restart_sequence
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.errors import HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
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


def _resolver_with_system_services(workspace_agent: AgentId, services_agent: AgentId) -> MngrCliBackendResolver:
    """Build a resolver where the workspace agent and system-services agent share a host."""
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
        )
    )
    return resolver


def _started_registry(workspace_agent: AgentId) -> InMemoryWorkspaceOperationRegistry:
    """A fresh operation registry with a RESTART operation already started for the agent."""
    registry = InMemoryWorkspaceOperationRegistry()
    registry.start(workspace_agent, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))
    return registry


# -- argv builders --


def test_build_mngr_stop_argv_appends_stop_host_only_for_host_restart() -> None:
    """The host tier adds --stop-host; the surgical tier stops just the agent."""
    aid = AgentId.generate()

    surgical = _build_mngr_stop_argv("/usr/local/bin/mngr", aid, is_host_restart=False)
    assert surgical[:3] == ["/usr/local/bin/mngr", "stop", str(aid)]
    assert "--stop-host" not in surgical

    host = _build_mngr_stop_argv("/usr/local/bin/mngr", aid, is_host_restart=True)
    assert host[:3] == ["/usr/local/bin/mngr", "stop", str(aid)]
    assert "--stop-host" in host


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

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            is_host_restart=False,
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


def test_run_restart_sequence_fails_when_stop_command_errors(tmp_path: Path) -> None:
    """A non-zero ``mngr stop`` ends the sequence in RESTART_FAILED naming the stop step."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            is_host_restart=False,
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
            is_host_restart=False,
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
            is_host_restart=True,
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
            is_host_restart=True,
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
            is_host_restart=True,
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
            is_host_restart=True,
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
