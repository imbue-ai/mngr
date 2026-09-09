from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_access import MachineUnreachableError
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperator
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import FixedHostBackendResolver
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote.credentials import FetchedMachineState
from imbue.mngr_latchkey.remote.credentials import MachineCredentials
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host

_EMPTY_POLICY = '{"rules": []}'
_SLACK_POLICY = '{"rules": [{"slack-api:a@example.com": ["slack-read-all"]}]}'


class _RecordingMachine(MachineCredentials):
    """A machine that records what it was asked to do instead of exchanging anything with a VPS."""

    calls: list[str] = Field(default_factory=list, description="Each exchange this machine was asked for, in order.")
    refusal: str = Field(default="", description="When set, the reason every exchange is refused with.")
    store_failure: str = Field(
        default="", description="When set, the reason this computer's own copies cannot be read or written."
    )

    def refresh(self) -> FetchedMachineState:
        self._note("refresh")
        return FetchedMachineState(credentials=None, permissions_json=None)

    def connect_service(self, service_name: str, account: str) -> None:
        self._note(f"connect {service_name} {account}")

    def disconnect_account(self, service_name: str, account: str) -> None:
        self._note(f"disconnect {service_name} {account}")

    def set_permissions(self, permissions_json: str) -> None:
        self._note(f"set_permissions {permissions_json}")

    def connect_service_with_permissions(self, service_name: str, account: str, permissions_json: str) -> None:
        self._note(f"grant {service_name} {account} {permissions_json}")

    def _note(self, call: str) -> None:
        if self.refusal:
            raise RemoteGatewayError(self.refusal)
        if self.store_failure:
            raise LatchkeyStoreError(self.store_failure)
        self.calls.append(call)


class _RecordedAccess(MachineAccess):
    """Access that opens a recording machine, so the operator's own decisions are what is under test.

    Whether a workspace *has* a machine still goes through the real
    :meth:`MachineAccess.machine_host_for`, which is the decision the rest of
    the pane depends on.
    """

    machine: _RecordingMachine = Field(description="The machine every workspace with one resolves to.")
    opening_refusal: str = Field(default="", description="When set, the reason the machine cannot be opened at all.")

    @contextmanager
    def open_machine(self, workspace_agent_id: str, host_id: HostId) -> Iterator[MachineCredentials]:
        del workspace_agent_id, host_id
        if self.opening_refusal:
            raise MachineUnreachableError(self.opening_refusal)
        yield self.machine


def _operator(
    tmp_path: Path,
    *,
    is_machine_of_its_own: bool,
    policy: str | None = None,
    refusal: str = "",
    store_failure: str = "",
    opening_refusal: str = "",
) -> tuple[MachineOperator, _RecordingMachine, str]:
    """An operator over one workspace, and the machine (if any) that workspace resolves to."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    latchkey = FakeAccountsLatchkey(latchkey_directory=latchkey_directory, latchkey_binary="/nonexistent")
    agent_id = AgentId()
    host_id = HostId.generate()
    if is_machine_of_its_own:
        store_machine_encryption_key(latchkey.plugin_data_dir, host_id, SecretStr("machine-key-4471"))
    if policy is not None:
        permissions_path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
        permissions_path.parent.mkdir(parents=True, exist_ok=True)
        permissions_path.write_text(policy)
    machine = _RecordingMachine(
        host=cast(OuterHostInterface, object()),
        latchkey=latchkey,
        host_id=host_id,
        refusal=refusal,
        store_failure=store_failure,
    )
    access = _RecordedAccess(
        latchkey=latchkey,
        concurrency_group=ConcurrencyGroup(name="machine-operations-test"),
        backend_resolver=FixedHostBackendResolver(
            url_by_agent_and_service={}, fixed_host_id=host_id, known_agent_ids=(agent_id,)
        ),
        machine=machine,
        opening_refusal=opening_refusal,
    )
    return MachineOperator(access=access), machine, str(agent_id)


def test_a_local_workspaces_state_is_already_where_its_gateway_reads_it(tmp_path: Path) -> None:
    """A workspace with no machine of its own has nothing to carry: the local edit is the whole change."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=False, policy=_EMPTY_POLICY)

    operator.push_permissions(agent_id)
    operator.connect_service(agent_id, "slack", "a@example.com")
    operator.refresh(agent_id)

    assert machine.calls == []


def test_a_permissions_edit_travels_to_the_machine_as_a_whole_snapshot(tmp_path: Path) -> None:
    """The machine's gateway enforces the whole file, so the whole file is what is pushed."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_SLACK_POLICY)

    operator.push_permissions(agent_id)

    assert machine.calls == [f"set_permissions {_SLACK_POLICY}"]


def test_a_permissions_edit_with_no_canonical_file_pushes_nothing(tmp_path: Path) -> None:
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True)

    operator.push_permissions(agent_id)

    assert machine.calls == []


def test_a_grant_carries_the_account_and_the_policy_that_grants_it_together(tmp_path: Path) -> None:
    """Applied credential-first on the machine and failed together, so neither half lands alone."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_SLACK_POLICY)

    operator.connect_service_with_permissions(agent_id, "slack", "a@example.com")

    assert machine.calls == [f"grant slack a@example.com {_SLACK_POLICY}"]


def test_a_grant_with_no_policy_to_snapshot_still_carries_the_credential(tmp_path: Path) -> None:
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True)

    operator.connect_service_with_permissions(agent_id, "slack", "a@example.com")

    assert machine.calls == ["connect slack a@example.com"]


def test_a_sign_out_clears_the_account_on_the_machine_itself(tmp_path: Path) -> None:
    """Clearing only the copy here would leave the machine's agents still able to use the credential."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True)

    operator.disconnect_account(agent_id, "slack", "a@example.com")

    assert machine.calls == ["disconnect slack a@example.com"]


def test_a_read_asks_the_machine_what_it_holds(tmp_path: Path) -> None:
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True)

    operator.refresh(agent_id)

    assert machine.calls == ["refresh"]


def test_a_machine_that_refuses_a_change_fails_the_call_that_asked_for_it(tmp_path: Path) -> None:
    """Nothing retries behind the user's back, so the caller has to hear it while it can still say so."""
    operator, _machine, agent_id = _operator(
        tmp_path, is_machine_of_its_own=True, policy=_EMPTY_POLICY, refusal="the VPS said no"
    )

    with pytest.raises(MachineOperationError, match="the VPS said no") as failure:
        operator.push_permissions(agent_id)

    assert "apply the permission change on that workspace" in str(failure.value)


def test_a_local_copy_that_cannot_be_read_fails_the_call_the_same_way(tmp_path: Path) -> None:
    """An unusable machine store or policy file fails the same action for the same user.

    The exchange reads and writes this computer's copies as well as the
    machine's, so those failures have to reach the caller as one kind of thing
    rather than escaping as an unhandled error.
    """
    operator, _machine, agent_id = _operator(
        tmp_path, is_machine_of_its_own=True, store_failure="the machine store is unreadable"
    )

    with pytest.raises(MachineOperationError, match="the machine store is unreadable"):
        operator.refresh(agent_id)


def test_a_machine_that_cannot_be_opened_fails_the_call_that_asked_for_it(tmp_path: Path) -> None:
    """A sleeping workspace, or an unreachable one, reads the same as one that refused."""
    operator, _machine, agent_id = _operator(
        tmp_path, is_machine_of_its_own=True, policy=_EMPTY_POLICY, opening_refusal="that workspace is asleep"
    )

    with pytest.raises(MachineOperationError, match="that workspace is asleep"):
        operator.connect_service(agent_id, "slack", "a@example.com")


def test_a_workspace_this_app_does_not_know_cannot_be_carried_to(tmp_path: Path) -> None:
    """An unresolvable workspace is not a local one: guessing would edit the wrong machine's policy."""
    operator, machine, _agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_EMPTY_POLICY)

    operator.push_permissions(str(AgentId()))

    assert machine.calls == []
