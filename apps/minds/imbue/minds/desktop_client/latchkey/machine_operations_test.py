from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from pydantic import Field
from pydantic import SecretStr
from pydantic import SkipValidation

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.latchkey.machine_access import MACHINE_NEWER_FAILURE
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_access import MachineUnreachableError
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperator
from imbue.minds.desktop_client.latchkey.machine_operations import MachinePolicyChangedOperationError
from imbue.minds.desktop_client.latchkey.machine_operations import PERMISSIONS_FORMAT_NEWER_FAILURE
from imbue.minds.desktop_client.latchkey.machine_operations import describe_machine_failure
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import FixedHostBackendResolver
from imbue.minds.desktop_client.testing import device_id_for_test
from imbue.minds.desktop_client.user_facing_failure import UserFacingFailure
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.migrations.interface import PermissionsFormatNewerError
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationContext
from imbue.mngr_latchkey.primitives import MachinePolicyFingerprint
from imbue.mngr_latchkey.primitives import PermissionsFormatVersion
from imbue.mngr_latchkey.remote._mirror import record_machine_as_newer
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote.credentials import FetchedMachineState
from imbue.mngr_latchkey.remote.credentials import MachineCredentials
from imbue.mngr_latchkey.remote.credentials import _write_machine_policy_record
from imbue.mngr_latchkey.remote.errors import MachineNewerError
from imbue.mngr_latchkey.remote.errors import MachinePolicyChangedError
from imbue.mngr_latchkey.remote.errors import MachineTransferError
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.remote.errors import SentToMachine
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import desktop_egress_rules_path_for_host
from imbue.mngr_latchkey.store import permissions_path_for_host

_EMPTY_POLICY = '{"rules": []}'
_SLACK_POLICY = '{"rules": [{"slack-api:a@example.com": ["slack-read-all"]}]}'
_SLACK_WRITE_POLICY = '{"rules": [{"slack-api:a@example.com": ["slack-read-all", "slack-write"]}]}'
_SLACK_DESKTOP_EGRESS_RULES = '{"slack": ["device-1"]}'
_MACHINE_FINGERPRINT = MachinePolicyFingerprint("fingerprint-3307")
_READ_AS_THIS_INSTALL = f"refresh as {device_id_for_test('machine-operations')}"


class _RecordingMachine(MachineCredentials):
    """A machine that records what it was asked to do instead of exchanging anything with a VPS."""

    calls: list[str] = Field(default_factory=list, description="Each exchange this machine was asked for, in order.")
    refusal: str = Field(
        default="", description="When set, the reason every exchange is refused with, as a failed policy push."
    )
    store_failure: str = Field(
        default="", description="When set, the reason this computer's own copies cannot be read or written."
    )
    raised: SkipValidation[Exception | None] = Field(
        default=None, description="When set, the error every exchange raises, as it reaches the operator."
    )
    changes_from_another_computer: list[str] = Field(
        default_factory=list,
        description="Each one has the next push find the machine holding another policy than it was made from.",
    )
    policy_adopted_on_refresh: str | None = Field(
        default=None,
        description="When set, the policy a read puts over this computer's copy, as adopting the machine's does.",
    )

    def refresh(self, migration_context: PermissionsMigrationContext) -> FetchedMachineState:
        self._note(f"refresh as {migration_context.device_id}")
        data_dir = self.latchkey.plugin_data_dir
        if self.policy_adopted_on_refresh is not None:
            permissions_path_for_host(data_dir, self.host_id).write_text(self.policy_adopted_on_refresh)
        _write_machine_policy_record(data_dir, self.host_id, _MACHINE_FINGERPRINT)
        return FetchedMachineState(
            credentials=None,
            permissions_json=None,
            desktop_egress_rules_json=None,
            permissions_format_version=PermissionsFormatVersion(0),
            policy_fingerprint=_MACHINE_FINGERPRINT,
            custom_service_registrations={},
        )

    def connect_service(self, service_name: str, account: str) -> None:
        self._note(f"connect {service_name} {account}")

    def disconnect_account(self, service_name: str, account: str, is_service_deregistered: bool) -> None:
        self._note(f"disconnect {service_name} {account}" + (" and deregister" if is_service_deregistered else ""))

    def set_permissions(self, permissions_json: str) -> None:
        if self.changes_from_another_computer:
            self.changes_from_another_computer.pop(0)
            raise MachinePolicyChangedError(self.host_id, "VPS vps-test holds another policy")
        self._note(f"set_permissions {permissions_json}")
        _write_machine_policy_record(self.latchkey.plugin_data_dir, self.host_id, _MACHINE_FINGERPRINT)

    def set_permissions_and_desktop_egress_rules(self, permissions_json: str, desktop_egress_rules_json: str) -> None:
        self._note(f"set_permissions_and_desktop_egress_rules {permissions_json} {desktop_egress_rules_json}")

    def connect_service_with_permissions(self, service_name: str, account: str, permissions_json: str) -> None:
        self._note(f"grant {service_name} {account} {permissions_json}")

    def connect_service_with_permissions_and_desktop_egress_rules(
        self, service_name: str, account: str, permissions_json: str, desktop_egress_rules_json: str
    ) -> None:
        self._note(f"grant {service_name} {account} {permissions_json} {desktop_egress_rules_json}")

    def _note(self, call: str) -> None:
        if self.raised is not None:
            raise self.raised
        if self.refusal:
            raise MachineTransferError(
                host_id=self.host_id,
                vps_name="vps-test",
                sent=frozenset({SentToMachine.PERMISSIONS}),
                expected=frozenset(),
                reason=self.refusal,
            )
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
    def open_machine(self, workspace_agent_id: str, host_id: HostId) -> Generator[MachineCredentials, None, None]:
        del workspace_agent_id, host_id
        if self.opening_refusal:
            raise MachineUnreachableError(
                self.opening_refusal, UserFacingFailure(message="The machine can't be reached.", remedy=None)
            )
        yield self.machine


def _operator(
    tmp_path: Path,
    *,
    is_machine_of_its_own: bool,
    policy: str | None = None,
    desktop_egress_rules: str | None = None,
    refusal: str = "",
    store_failure: str = "",
    opening_refusal: str = "",
    raised: Exception | None = None,
    changes_from_another_computer: Sequence[str] = (),
    policy_adopted_on_refresh: str | None = None,
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
    if desktop_egress_rules is not None:
        rules_path = desktop_egress_rules_path_for_host(latchkey.plugin_data_dir, host_id)
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(desktop_egress_rules)
    machine = _RecordingMachine(
        host=cast(OuterHostInterface, object()),
        latchkey=latchkey,
        host_id=host_id,
        refusal=refusal,
        store_failure=store_failure,
        raised=raised,
        changes_from_another_computer=list(changes_from_another_computer),
        policy_adopted_on_refresh=policy_adopted_on_refresh,
    )
    access = _RecordedAccess(
        latchkey=latchkey,
        device_id=device_id_for_test("machine-operations"),
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
    operator.push_permissions_and_desktop_egress_rules(agent_id)
    operator.connect_service(agent_id, "slack", "a@example.com")
    operator.connect_service_with_permissions_and_desktop_egress_rules(agent_id, "slack", "a@example.com")
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


def test_a_desktop_egress_edit_travels_as_both_snapshots_in_one_exchange(tmp_path: Path) -> None:
    operator, machine, agent_id = _operator(
        tmp_path,
        is_machine_of_its_own=True,
        policy=_SLACK_POLICY,
        desktop_egress_rules=_SLACK_DESKTOP_EGRESS_RULES,
    )

    operator.push_permissions_and_desktop_egress_rules(agent_id)

    assert machine.calls == [f"set_permissions_and_desktop_egress_rules {_SLACK_POLICY} {_SLACK_DESKTOP_EGRESS_RULES}"]


def test_a_desktop_egress_edit_with_no_rules_copy_to_push_fails(tmp_path: Path) -> None:
    """The caller writes the copy right before the push, so a missing one is a failed write, not an empty file."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_SLACK_POLICY)

    with pytest.raises(MachineOperationError, match="no copy of the desktop egress rules") as failure:
        operator.push_permissions_and_desktop_egress_rules(agent_id)

    assert failure.value.transfer is None
    assert machine.calls == []


def test_a_desktop_egress_edit_with_no_canonical_policy_pushes_nothing(tmp_path: Path) -> None:
    operator, machine, agent_id = _operator(
        tmp_path, is_machine_of_its_own=True, desktop_egress_rules=_SLACK_DESKTOP_EGRESS_RULES
    )

    operator.push_permissions_and_desktop_egress_rules(agent_id)

    assert machine.calls == []


def test_a_machine_that_refuses_a_desktop_egress_edit_fails_the_call_that_asked_for_it(tmp_path: Path) -> None:
    operator, _machine, agent_id = _operator(
        tmp_path,
        is_machine_of_its_own=True,
        policy=_SLACK_POLICY,
        desktop_egress_rules=_SLACK_DESKTOP_EGRESS_RULES,
        refusal="the VPS said no",
    )

    with pytest.raises(MachineOperationError, match="the VPS said no"):
        operator.push_permissions_and_desktop_egress_rules(agent_id)


def _edit_making_the_policy(
    operator: MachineOperator,
    machine: _RecordingMachine,
    agent_id: str,
    policy: str,
    # Gains what the copy here said each time the edit ran.
    copies_edited_out: list[str],
) -> Callable[[], None]:
    """An edit that makes this computer's copy say ``policy`` and pushes it."""
    permissions_path = permissions_path_for_host(machine.latchkey.plugin_data_dir, machine.host_id)

    def edit() -> None:
        copies_edited_out.append(permissions_path.read_text())
        permissions_path.write_text(policy)
        operator.push_permissions(agent_id)

    return edit


def test_an_edit_of_the_policy_just_read_costs_the_one_exchange_of_its_push(tmp_path: Path) -> None:
    """The read the Permissions tab opened with is the read the edit is made on, and each push stands in for the next."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_SLACK_POLICY)
    copies_edited: list[str] = []
    operator.refresh(agent_id)

    operator.carry_policy_edit(
        agent_id, _edit_making_the_policy(operator, machine, agent_id, _SLACK_WRITE_POLICY, copies_edited)
    )
    operator.carry_policy_edit(
        agent_id, _edit_making_the_policy(operator, machine, agent_id, _EMPTY_POLICY, copies_edited)
    )

    assert machine.calls == [
        _READ_AS_THIS_INSTALL,
        f"set_permissions {_SLACK_WRITE_POLICY}",
        f"set_permissions {_EMPTY_POLICY}",
    ]
    assert copies_edited == [_SLACK_POLICY, _SLACK_WRITE_POLICY]


def test_an_edit_of_a_policy_never_read_from_the_machine_is_made_on_a_fresh_read(tmp_path: Path) -> None:
    """Nothing says the copy here is the machine's, so it is not edited as it stands."""
    operator, machine, agent_id = _operator(
        tmp_path, is_machine_of_its_own=True, policy=_EMPTY_POLICY, policy_adopted_on_refresh=_SLACK_POLICY
    )
    copies_edited: list[str] = []

    operator.carry_policy_edit(
        agent_id, _edit_making_the_policy(operator, machine, agent_id, _SLACK_WRITE_POLICY, copies_edited)
    )

    assert machine.calls == [_READ_AS_THIS_INSTALL, f"set_permissions {_SLACK_WRITE_POLICY}"]
    assert copies_edited == [_SLACK_POLICY]


def test_an_edit_the_machine_has_moved_on_from_is_made_again_on_the_policy_it_holds(tmp_path: Path) -> None:
    """A grant made from another computer since the last read refuses the push, is read, and survives the edit."""
    operator, machine, agent_id = _operator(
        tmp_path,
        is_machine_of_its_own=True,
        policy=_SLACK_POLICY,
        changes_from_another_computer=("a grant",),
    )
    copies_edited: list[str] = []
    operator.refresh(agent_id)
    machine_calls_before_the_edit = list(machine.calls)
    permissions_path = permissions_path_for_host(machine.latchkey.plugin_data_dir, machine.host_id)

    def adopt_the_other_computers_grant_then_edit() -> None:
        # The second run follows a read, which this machine answers by leaving the other computer's grant here.
        if copies_edited:
            permissions_path.write_text(_SLACK_WRITE_POLICY)
        _edit_making_the_policy(operator, machine, agent_id, _EMPTY_POLICY, copies_edited)()

    operator.carry_policy_edit(agent_id, adopt_the_other_computers_grant_then_edit)

    assert machine.calls == [*machine_calls_before_the_edit, _READ_AS_THIS_INSTALL, f"set_permissions {_EMPTY_POLICY}"]
    assert copies_edited == [_SLACK_POLICY, _SLACK_WRITE_POLICY]


def test_a_push_the_machine_refuses_as_made_from_another_policy_says_so(tmp_path: Path) -> None:
    """Told apart from every other failure, so a caller can read the machine and make its edit again."""
    operator, machine, agent_id = _operator(
        tmp_path, is_machine_of_its_own=True, policy=_SLACK_POLICY, changes_from_another_computer=("a grant",)
    )

    with pytest.raises(MachinePolicyChangedOperationError, match="holds another policy") as failure:
        operator.push_permissions(agent_id)

    assert failure.value.explained_failure is None
    assert machine.calls == []


def test_an_edit_is_not_made_on_top_of_one_the_machine_never_took(tmp_path: Path) -> None:
    """A failed push leaves its edit in the copy here, which the machine never held: the next edit reads first."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_SLACK_POLICY)
    operator.refresh(agent_id)
    permissions_path_for_host(machine.latchkey.plugin_data_dir, machine.host_id).write_text(_SLACK_WRITE_POLICY)

    operator.carry_policy_edit(agent_id, _edit_making_the_policy(operator, machine, agent_id, _EMPTY_POLICY, []))

    assert machine.calls == [_READ_AS_THIS_INSTALL, _READ_AS_THIS_INSTALL, f"set_permissions {_EMPTY_POLICY}"]


def test_an_edit_that_finds_nothing_to_change_exchanges_nothing(tmp_path: Path) -> None:
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_SLACK_POLICY)
    operator.refresh(agent_id)

    operator.carry_policy_edit(agent_id, lambda: None)

    assert machine.calls == [_READ_AS_THIS_INSTALL]


def test_an_edit_of_a_local_workspaces_policy_is_the_whole_change(tmp_path: Path) -> None:
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=False, policy=_SLACK_POLICY)
    copies_edited: list[str] = []

    operator.carry_policy_edit(
        agent_id, _edit_making_the_policy(operator, machine, agent_id, _SLACK_WRITE_POLICY, copies_edited)
    )

    assert machine.calls == []
    assert copies_edited == [_SLACK_POLICY]


def test_an_edit_is_not_made_for_a_machine_this_build_leaves_alone(tmp_path: Path) -> None:
    """Found out before anything is edited, so the copy here is not left saying what the machine never will."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_SLACK_POLICY)
    record_machine_as_newer(machine.latchkey.plugin_data_dir, machine.host_id)
    copies_edited: list[str] = []

    with pytest.raises(MachineOperationError) as failure:
        operator.carry_policy_edit(
            agent_id, _edit_making_the_policy(operator, machine, agent_id, _SLACK_WRITE_POLICY, copies_edited)
        )

    assert failure.value.explained_failure == MACHINE_NEWER_FAILURE
    assert copies_edited == []
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


def test_a_grant_that_changes_a_route_carries_the_account_the_policy_and_the_rules_in_one_exchange(
    tmp_path: Path,
) -> None:
    operator, machine, agent_id = _operator(
        tmp_path,
        is_machine_of_its_own=True,
        policy=_SLACK_POLICY,
        desktop_egress_rules=_SLACK_DESKTOP_EGRESS_RULES,
    )

    operator.connect_service_with_permissions_and_desktop_egress_rules(agent_id, "slack", "a@example.com")

    assert machine.calls == [f"grant slack a@example.com {_SLACK_POLICY} {_SLACK_DESKTOP_EGRESS_RULES}"]


def test_a_grant_that_changes_a_route_with_no_rules_copy_to_push_fails(tmp_path: Path) -> None:
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_SLACK_POLICY)

    with pytest.raises(MachineOperationError, match="no copy of the desktop egress rules") as failure:
        operator.connect_service_with_permissions_and_desktop_egress_rules(agent_id, "slack", "a@example.com")

    assert failure.value.transfer is None
    assert machine.calls == []


def test_a_grant_that_changes_a_route_with_no_policy_to_snapshot_fails(tmp_path: Path) -> None:
    """The caller writes the grant right before the carry, so a missing policy is a failed write."""
    operator, machine, agent_id = _operator(
        tmp_path, is_machine_of_its_own=True, desktop_egress_rules=_SLACK_DESKTOP_EGRESS_RULES
    )

    with pytest.raises(MachineOperationError, match="no copy of the permissions"):
        operator.connect_service_with_permissions_and_desktop_egress_rules(agent_id, "slack", "a@example.com")

    assert machine.calls == []


def test_a_sign_out_clears_the_account_on_the_machine_itself(tmp_path: Path) -> None:
    """Clearing only the copy here would leave the machine's agents still able to use the credential."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True)

    operator.disconnect_account(agent_id, "slack", "a@example.com", is_service_deregistered=False)

    assert machine.calls == ["disconnect slack a@example.com"]


def test_a_sign_out_of_a_custom_services_last_account_deregisters_it_on_the_machine(tmp_path: Path) -> None:
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True)

    operator.disconnect_account(agent_id, "custom_https_example_com", "me@example.com", is_service_deregistered=True)

    assert machine.calls == ["disconnect custom_https_example_com me@example.com and deregister"]


def test_a_read_asks_the_machine_what_it_holds_as_this_install(tmp_path: Path) -> None:
    """The device a read names is the one a migrated file-sharing grant is scoped to."""
    operator, machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True)

    operator.refresh(agent_id)

    assert machine.calls == [f"refresh as {device_id_for_test('machine-operations')}"]


def test_a_machine_that_refuses_a_change_fails_the_call_that_asked_for_it(tmp_path: Path) -> None:
    """Nothing retries behind the user's back, so the caller has to hear it while it can still say so."""
    operator, _machine, agent_id = _operator(
        tmp_path, is_machine_of_its_own=True, policy=_EMPTY_POLICY, refusal="the VPS said no"
    )

    with pytest.raises(MachineOperationError, match="the VPS said no") as failure:
        operator.push_permissions(agent_id)

    transfer = failure.value.transfer
    assert transfer is not None
    assert (transfer.sent, transfer.expected) == ({SentToMachine.PERMISSIONS}, frozenset())


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

    with pytest.raises(MachineOperationError, match="that workspace is asleep") as failure:
        operator.connect_service(agent_id, "slack", "a@example.com")

    assert failure.value.transfer is None


def test_a_workspace_this_app_does_not_know_cannot_be_carried_to(tmp_path: Path) -> None:
    """An unresolvable workspace is not a local one: guessing would edit the wrong machine's policy."""
    operator, machine, _agent_id = _operator(tmp_path, is_machine_of_its_own=True, policy=_EMPTY_POLICY)

    operator.push_permissions(str(AgentId()))

    assert machine.calls == []


def _raised_from(error: Exception, cause: Exception) -> Exception:
    """``error`` as it looks once raised from ``cause``."""
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("raised", "expected_failure"),
    [
        pytest.param(MachineNewerError("vps-test"), MACHINE_NEWER_FAILURE, id="the-machine-is-newer"),
        pytest.param(
            _raised_from(RemoteGatewayError("Could not read the machine"), MachineNewerError("vps-test")),
            MACHINE_NEWER_FAILURE,
            id="the-machine-is-newer-under-another-error",
        ),
        pytest.param(
            _raised_from(
                RemoteGatewayError("Could not migrate the policy"),
                PermissionsFormatNewerError("the policy is in format 9"),
            ),
            PERMISSIONS_FORMAT_NEWER_FAILURE,
            id="the-policy-format-is-newer-under-another-error",
        ),
    ],
)
def test_a_failure_is_explained_by_whichever_of_its_causes_explains_itself(
    tmp_path: Path, raised: Exception, expected_failure: UserFacingFailure
) -> None:
    """A cause that explains itself is routinely wrapped on its way to the operator, so the whole chain is read."""
    operator, _machine, agent_id = _operator(tmp_path, is_machine_of_its_own=True, raised=raised)

    with pytest.raises(MachineOperationError) as failure:
        operator.refresh(agent_id)

    assert failure.value.explained_failure == expected_failure
    assert describe_machine_failure(failure.value, "The caller's own sentence.") == expected_failure


def test_a_failure_none_of_whose_causes_explains_itself_is_told_in_the_callers_sentence(tmp_path: Path) -> None:
    operator, _machine, agent_id = _operator(
        tmp_path,
        is_machine_of_its_own=True,
        raised=_raised_from(RemoteGatewayError("Could not read the machine"), OSError("connection reset")),
    )

    with pytest.raises(MachineOperationError) as failure:
        operator.refresh(agent_id)

    assert failure.value.explained_failure is None
    assert describe_machine_failure(failure.value, "The caller's own sentence.") == UserFacingFailure(
        message="The caller's own sentence.", remedy=None
    )
