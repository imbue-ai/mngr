"""The update run's host lifecycle: which thread each half runs on."""

import threading
from pathlib import Path

from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.app import _UpdateRunHostLifecycle
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.mngr.primitives import AgentId


class _ThreadRecordingResolver(StaticBackendResolver):
    """Records which thread asked for the services agent, and knows no host (so nothing shells out)."""

    _asked_on: threading.Event = PrivateAttr(default_factory=threading.Event)
    _asking_thread_name: str = PrivateAttr(default="")

    def get_system_services_agent_id(self, workspace_agent_id: AgentId) -> AgentId | None:
        self._asking_thread_name = threading.current_thread().name
        self._asked_on.set()
        return None

    def wait_for_asking_thread_name(self) -> str:
        assert self._asked_on.wait(timeout=10.0), "the stop never asked the resolver for the services agent"
        return self._asking_thread_name


def test_the_scheduled_run_host_stop_runs_off_the_calling_thread(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    resolver = _ThreadRecordingResolver(url_by_agent_and_service={})
    _client, app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=False, backend_resolver=resolver
    )
    lifecycle = _UpdateRunHostLifecycle(
        flask_app=app,
        backend_resolver=resolver,
        tracker=SystemInterfaceHealthTracker(),
        concurrency_group=root_concurrency_group,
        mngr_binary="mngr",
        mngr_host_dir=tmp_path / "hosts",
    )

    lifecycle.stop_in_background(AgentId.generate())

    assert resolver.wait_for_asking_thread_name() != threading.current_thread().name


def test_the_update_run_host_start_runs_on_the_calling_thread(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The dispatch's next step execs into the machine, so it cannot leave the start in flight."""
    resolver = _ThreadRecordingResolver(url_by_agent_and_service={})
    _client, app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=False, backend_resolver=resolver
    )
    lifecycle = _UpdateRunHostLifecycle(
        flask_app=app,
        backend_resolver=resolver,
        tracker=SystemInterfaceHealthTracker(),
        concurrency_group=root_concurrency_group,
        mngr_binary="mngr",
        mngr_host_dir=tmp_path / "hosts",
    )

    # The resolver knows no host, so this reports failure without shelling out.
    assert lifecycle.start_and_wait(AgentId.generate()) is False

    assert resolver.wait_for_asking_thread_name() == threading.current_thread().name
