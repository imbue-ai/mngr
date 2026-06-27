from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.app import _is_modal_workspace
from imbue.minds.desktop_client.app import _run_restart_sequence
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.mngr.primitives import AgentId


class _FakeResolver(StaticBackendResolver):
    """StaticBackendResolver that returns a fixed system-services agent id and an
    optional provider name (the two resolver methods the restart worker uses)."""

    services_agent_id: AgentId
    provider_name: str | None = None

    def get_system_services_agent_id(self, workspace_agent_id: AgentId) -> AgentId | None:
        return self.services_agent_id

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if self.provider_name is None:
            return None
        return AgentDisplayInfo(agent_name="ws", host_id="h", provider_name=self.provider_name)


def _write_fake_mngr(tmp_path: Path, stop_host_stderr: str) -> str:
    """Write a fake `mngr` that fails `--stop-host` (with the given stderr) and succeeds otherwise.

    Mirrors how the real `mngr` behaves for a no-shutdown provider: `mngr stop --stop-host`
    exits non-zero, everything else (notably `mngr start`) exits clean.
    """
    script = tmp_path / "mngr"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "--stop-host" ]; then\n'
        f'    echo "{stop_host_stderr}" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return str(script)


def _run(tmp_path: Path, stop_host_stderr: str) -> tuple[SystemInterfaceHealthTracker, AgentId]:
    workspace_id = AgentId.generate()
    services_id = AgentId.generate()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=1.0)
    tracker.mark_restarting(workspace_id)
    resolver = _FakeResolver(url_by_agent_and_service={}, services_agent_id=services_id)
    with ConcurrencyGroup(name="app-test-restart") as cg:
        _run_restart_sequence(
            workspace_id,
            True,
            tracker,
            resolver,
            _write_fake_mngr(tmp_path, stop_host_stderr),
            tmp_path,
            cg,
            0,
            None,
            skip_stop=False,
        )
    return tracker, workspace_id


def test_run_restart_sequence_skips_unsupported_stop_and_proceeds(tmp_path: Path) -> None:
    """A host-restart on a provider that can't stop in place (Modal: `mngr stop --stop-host`
    raises HostShutdownNotSupportedError) must NOT fail the restart -- it skips the stop and
    proceeds to `mngr start`, which restarts the host on its own."""
    tracker, workspace_id = _run(tmp_path, "Provider modal does not support stopping hosts")
    assert tracker.get_health(workspace_id) != AgentHealth.RESTART_FAILED
    assert tracker.get_last_restart_error(workspace_id) is None


def test_run_restart_sequence_still_fails_on_other_stop_error(tmp_path: Path) -> None:
    """A genuine stop failure (not the unsupported-shutdown signal) still fails the restart."""
    tracker, workspace_id = _run(tmp_path, "some unrelated stop failure")
    assert tracker.get_health(workspace_id) == AgentHealth.RESTART_FAILED


def test_is_modal_workspace_true_for_modal() -> None:
    """A workspace whose discovery provider is 'modal' is treated as Modal (longer restart budget)."""
    resolver = _FakeResolver(url_by_agent_and_service={}, services_agent_id=AgentId.generate(), provider_name="modal")
    assert _is_modal_workspace(resolver, AgentId.generate()) is True


def test_is_modal_workspace_false_for_other_provider() -> None:
    """Non-Modal providers keep the fast default restart budget."""
    resolver = _FakeResolver(url_by_agent_and_service={}, services_agent_id=AgentId.generate(), provider_name="docker")
    assert _is_modal_workspace(resolver, AgentId.generate()) is False


def test_is_modal_workspace_false_for_unknown_agent() -> None:
    """An agent with no discovery display info is treated as non-Modal."""
    resolver = _FakeResolver(url_by_agent_and_service={}, services_agent_id=AgentId.generate())
    assert _is_modal_workspace(resolver, AgentId.generate()) is False
