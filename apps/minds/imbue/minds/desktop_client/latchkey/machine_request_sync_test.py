from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final
from typing import cast

import pytest
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.test_utils import poll_until
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_request_sync import MachineRequestSync
from imbue.minds.desktop_client.latchkey.machine_request_sync import plan_request_sync
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import create_request_response_event
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.testing import StaticPendingRequests
from imbue.minds.desktop_client.testing import device_id_for_test
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.filed_permission_requests import FiledPermissionRequest
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote.credentials import MachineCredentials
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.store import permissions_path_for_host

_THIS_DESKTOP: Final[DesktopDeviceId] = DesktopDeviceId("desktop-this-4e1a")
_OTHER_DESKTOP: Final[DesktopDeviceId] = DesktopDeviceId("desktop-other-9c22")
_NOW: Final[datetime] = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
_AGENT_ID: Final[str] = "agent-" + "a" * 32


def _filed(request_id: str, devices: str | tuple[str, ...] = "*") -> FiledPermissionRequest:
    return FiledPermissionRequest(
        request_id=request_id,
        devices=devices,
        created_at=_NOW - timedelta(hours=1),
        body={"agent_id": _AGENT_ID, "rationale": f"for {request_id}", "type": "accounts", "payload": {}},
    )


def _pending(
    request_id: str, target: str = "/perms/hosts/h/latchkey_permissions.json", age: timedelta | None = None
) -> StreamedPermissionRequest:
    return StreamedPermissionRequest.model_validate(
        {
            "request_id": request_id,
            "agent_id": _AGENT_ID,
            "rationale": "x",
            "request_type": "accounts",
            "payload": {},
            "target": target,
            "effect": {"rules": []},
            "created_at": None if age is None else (_NOW - age).isoformat(),
        }
    )


def test_a_plan_files_what_the_machine_keeps_for_this_desktop_and_this_desktop_lacks() -> None:
    plan = plan_request_sync(
        filed_on_machine=(_filed("new-for-all"), _filed("already-here"), _filed("for-another", (_OTHER_DESKTOP,))),
        pending_here_for_host=(_pending("already-here"),),
        resolved_here=frozenset(),
        device_id=_THIS_DESKTOP,
        now=_NOW,
    )

    assert [filed.request_id for filed in plan.to_file_here] == ["new-for-all"]
    assert plan.to_drop_here == ()
    assert plan.to_forget_on_machine == ()


def test_a_plan_files_a_request_the_machine_keeps_for_this_desktop_by_name() -> None:
    plan = plan_request_sync(
        filed_on_machine=(_filed("named", (_OTHER_DESKTOP, _THIS_DESKTOP)),),
        pending_here_for_host=(),
        resolved_here=frozenset(),
        device_id=_THIS_DESKTOP,
        now=_NOW,
    )

    assert [filed.request_id for filed in plan.to_file_here] == ["named"]


def test_a_plan_drops_what_this_desktop_holds_and_the_machine_no_longer_keeps_unless_it_is_fresh() -> None:
    """A request another desktop answered goes; one filed moments ago may simply not be recorded yet."""
    plan = plan_request_sync(
        filed_on_machine=(_filed("kept"),),
        pending_here_for_host=(
            _pending("kept"),
            _pending("answered-elsewhere", age=timedelta(minutes=5)),
            _pending("from-before-stamps"),
            _pending("just-filed", age=timedelta(seconds=30)),
        ),
        resolved_here=frozenset(),
        device_id=_THIS_DESKTOP,
        now=_NOW,
    )

    assert plan.to_drop_here == ("answered-elsewhere", "from-before-stamps")
    assert plan.to_file_here == ()


def test_a_plan_asks_the_machine_to_forget_a_request_this_desktop_already_answered() -> None:
    """The verdict here outranks the record: it is the withdrawal that did not reach the machine, not the answer."""
    plan = plan_request_sync(
        filed_on_machine=(_filed("answered-here"), _filed("open")),
        pending_here_for_host=(),
        resolved_here=frozenset({"answered-here"}),
        device_id=_THIS_DESKTOP,
        now=_NOW,
    )

    assert plan.to_forget_on_machine == ("answered-here",)
    assert [filed.request_id for filed in plan.to_file_here] == ["open"]


def test_a_plan_with_nothing_to_do_is_empty() -> None:
    plan = plan_request_sync(
        filed_on_machine=(_filed("same"),),
        pending_here_for_host=(_pending("same"),),
        resolved_here=frozenset(),
        device_id=_THIS_DESKTOP,
        now=_NOW,
    )

    assert plan.is_empty


class _RecordingMachine(MachineCredentials):
    """A machine that answers with the requests it was given and records what it is asked to forget."""

    filed: tuple[FiledPermissionRequest, ...] = Field(default=(), description="What the machine keeps.")
    forgotten: list[str] = Field(default_factory=list, description="Every request it was asked to forget, in order.")
    refusal: str = Field(default="", description="When set, the reason every exchange is refused with.")

    def list_filed_permission_requests(self) -> tuple[FiledPermissionRequest, ...]:
        if self.refusal:
            raise RemoteGatewayError(self.refusal)
        return self.filed

    def forget_permission_request(self, request_id: str) -> None:
        self.forgotten.append(request_id)


class _GrowingWorkspaces(StaticBackendResolver):
    """Discovery as a test drives it: the workspaces it adds, each on a host of its own, while the sync runs."""

    host_id_by_agent_id: dict[AgentId, HostId] = Field(default_factory=dict, description="Every workspace's host.")

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return tuple(self.host_id_by_agent_id)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        host_id = self.host_id_by_agent_id.get(agent_id)
        return None if host_id is None else AgentDisplayInfo(agent_name=str(agent_id), host_id=str(host_id))


class _RecordedAccess(MachineAccess):
    """Access that opens the recording machine for every workspace with a machine of its own."""

    machine: _RecordingMachine = Field(description="The machine every such workspace resolves to.")
    opened: list[tuple[str, HostId]] = Field(default_factory=list, description="Every open, in order.")

    @contextmanager
    def open_machine(self, workspace_agent_id: str, host_id: HostId) -> Iterator[MachineCredentials]:
        self.opened.append((workspace_agent_id, host_id))
        yield self.machine


class _SyncScenario(FrozenModel):
    """A sync over one workspace with a machine of its own, and everything a test reads back from it."""

    sync: MachineRequestSync = Field(description="The sync under test, over this desktop's gateway and the machine.")
    machine: _RecordingMachine = Field(description="The workspace's machine, recording what it is asked to forget.")
    gateway_client: FakeLatchkeyGatewayClient = Field(
        description="This desktop's gateway, recording filings and drops."
    )
    access: _RecordedAccess = Field(description="How the sync reaches the machine, recording every open.")
    host_id: HostId = Field(description="The workspace's host.")
    agent_id: AgentId = Field(description="The workspace.")


def _sync(
    tmp_path: Path,
    *,
    filed: tuple[FiledPermissionRequest, ...],
    pending_for_host: tuple[tuple[str, timedelta | None], ...] = (),
    pending_elsewhere: tuple[StreamedPermissionRequest, ...] = (),
    answered_ids: tuple[str, ...] = (),
    refused_ids: tuple[str, ...] = (),
    is_machine_of_its_own: bool = True,
    refusal: str = "",
    workspaces: _GrowingWorkspaces | None = None,
) -> _SyncScenario:
    """A sync over one workspace, its machine, and this desktop's gateway; ``pending_for_host`` is ``(id, age)``.

    The workspace is added to ``workspaces`` when given, so a test can grow the topology under a running sync.
    """
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    latchkey = FakeAccountsLatchkey(latchkey_directory=latchkey_directory, latchkey_binary="/nonexistent")
    agent_id = AgentId()
    host_id = HostId.generate()
    if is_machine_of_its_own:
        store_machine_encryption_key(latchkey.plugin_data_dir, host_id, SecretStr("machine-key-7c3e"))
    host_target = str(permissions_path_for_host(latchkey.plugin_data_dir, host_id))
    pending = (
        *(_pending(request_id, target=host_target, age=age) for request_id, age in pending_for_host),
        *pending_elsewhere,
    )
    machine = _RecordingMachine(
        host=cast(OuterHostInterface, object()), latchkey=latchkey, host_id=host_id, filed=filed, refusal=refusal
    )
    resolver = _GrowingWorkspaces(url_by_agent_and_service={}) if workspaces is None else workspaces
    resolver.host_id_by_agent_id[agent_id] = host_id
    access = _RecordedAccess(
        latchkey=latchkey,
        device_id=device_id_for_test("machine-request-sync"),
        concurrency_group=ConcurrencyGroup(name="machine-request-sync-test"),
        backend_resolver=resolver,
        machine=machine,
    )
    gateway_client = FakeLatchkeyGatewayClient(refused_request_ids=refused_ids)
    pending_requests = StaticPendingRequests(
        pending=pending,
        answered=tuple(
            create_request_response_event(request_id, RequestStatus.GRANTED, _AGENT_ID) for request_id in answered_ids
        ),
    )
    sync = MachineRequestSync(
        access=access,
        gateway_client=gateway_client,
        pending_requests=pending_requests,
        device_id=_THIS_DESKTOP,
        concurrency_group=access.concurrency_group,
        now=lambda: _NOW,
    )
    return _SyncScenario(
        sync=sync, machine=machine, gateway_client=gateway_client, access=access, host_id=host_id, agent_id=agent_id
    )


def _target_of(access: MachineAccess, host_id: HostId) -> Path:
    return permissions_path_for_host(access.latchkey.plugin_data_dir, host_id)


def test_a_pass_files_the_machines_requests_here_against_the_hosts_permissions_file(tmp_path: Path) -> None:
    scenario = _sync(tmp_path, filed=(_filed("missing-here"),))

    scenario.sync.sync_every_machine()

    body, target = scenario.gateway_client.filed_elsewhere["missing-here"]
    assert body["rationale"] == "for missing-here"
    assert target == _target_of(scenario.access, scenario.host_id)
    assert scenario.gateway_client.deleted_request_ids == ()


def test_a_pass_drops_what_another_desktop_answered_and_tells_the_machine_what_this_one_did(tmp_path: Path) -> None:
    """Only the host's own requests are judged against its machine; another host's are not this machine's to say."""
    scenario = _sync(
        tmp_path,
        filed=(_filed("answered-here-earlier"), _filed("open")),
        pending_for_host=(("open", None), ("answered-elsewhere", timedelta(minutes=10))),
        pending_elsewhere=(
            _pending("of-another-host", target="/elsewhere/latchkey_permissions.json", age=timedelta(minutes=10)),
        ),
        answered_ids=("answered-here-earlier",),
    )

    scenario.sync.sync_every_machine()

    assert scenario.gateway_client.deleted_request_ids == ("answered-elsewhere",)
    assert scenario.machine.forgotten == ["answered-here-earlier"]
    assert scenario.gateway_client.filed_elsewhere == {}


def test_a_request_this_desktops_gateway_refuses_is_forgotten_on_the_machine(tmp_path: Path) -> None:
    """No desktop will ever take it, so it is not left for every pass to find again."""
    scenario = _sync(tmp_path, filed=(_filed("bad-body"), _filed("fine")), refused_ids=("bad-body",))

    scenario.sync.sync_every_machine()

    assert scenario.machine.forgotten == ["bad-body"]
    assert list(scenario.gateway_client.filed_elsewhere) == ["fine"]


def test_a_workspace_without_a_machine_of_its_own_is_never_synced(tmp_path: Path) -> None:
    scenario = _sync(tmp_path, filed=(_filed("x"),), is_machine_of_its_own=False)

    scenario.sync.sync_every_machine()

    assert scenario.access.opened == []
    assert scenario.gateway_client.filed_elsewhere == {}


def test_a_machine_that_cannot_be_read_is_left_for_the_next_pass(tmp_path: Path) -> None:
    scenario = _sync(tmp_path, filed=(_filed("x"),), refusal="the machine is away")

    scenario.sync.sync_every_machine()

    assert scenario.gateway_client.filed_elsewhere == {}
    assert scenario.gateway_client.deleted_request_ids == ()


def test_a_verdict_given_here_is_carried_to_the_machine_off_the_callers_path(tmp_path: Path) -> None:
    scenario = _sync(tmp_path, filed=())

    with scenario.access.concurrency_group:
        scenario.sync.forget_in_background("answered-now", scenario.agent_id)

    assert scenario.machine.forgotten == ["answered-now"]
    assert scenario.access.opened == [(str(scenario.agent_id), scenario.host_id)]


def test_a_verdict_on_a_local_workspaces_request_reaches_no_machine(tmp_path: Path) -> None:
    scenario = _sync(tmp_path, filed=(), is_machine_of_its_own=False)

    with scenario.access.concurrency_group:
        scenario.sync.forget_in_background("answered-now", scenario.agent_id)

    assert scenario.machine.forgotten == []
    assert scenario.access.opened == []


@pytest.mark.parametrize("refusal", ["", "the machine is away"], ids=["machine-readable", "machine-away"])
def test_the_background_sync_takes_every_machine_at_start_and_then_only_one_that_appears(
    tmp_path: Path, refusal: str
) -> None:
    """The first pass takes every machine, readable or not; a wake, which comes with every change the backend
    reports, takes only a machine that appeared since, leaving the rest (a machine that is away included) to
    the interval."""
    workspaces = _GrowingWorkspaces(url_by_agent_and_service={})
    scenario = _sync(tmp_path, filed=(), refusal=refusal, workspaces=workspaces)
    first = (str(scenario.agent_id), scenario.host_id)
    later_agent_id = AgentId()
    later_host_id = HostId.generate()
    store_machine_encryption_key(
        scenario.access.latchkey.plugin_data_dir, later_host_id, SecretStr("machine-key-9f0d")
    )

    with scenario.access.concurrency_group:
        scenario.sync.start()
        assert poll_until(lambda: scenario.access.opened == [first])
        workspaces.host_id_by_agent_id[later_agent_id] = later_host_id
        scenario.sync.notice_topology_change()
        assert poll_until(lambda: (str(later_agent_id), later_host_id) in scenario.access.opened)
        scenario.sync.stop()

    assert scenario.access.opened == [first, (str(later_agent_id), later_host_id)]
