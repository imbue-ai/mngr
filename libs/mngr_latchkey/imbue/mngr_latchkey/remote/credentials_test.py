import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.remote._mirror import machine_credentials_path
from imbue.mngr_latchkey.remote._mirror import materialize_machine_store
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import write_machine_credentials
from imbue.mngr_latchkey.remote.credentials import MachineCredentials
from imbue.mngr_latchkey.remote.credentials import MachineCredentialsError
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.remote.mock_outer_host_test import MACHINE_KEY
from imbue.mngr_latchkey.remote.mock_outer_host_test import SLACK_GRANTED
from imbue.mngr_latchkey.remote.mock_outer_host_test import as_stub
from imbue.mngr_latchkey.remote.mock_outer_host_test import desktop_latchkey
from imbue.mngr_latchkey.remote.mock_outer_host_test import fake_latchkey_binary
from imbue.mngr_latchkey.remote.mock_outer_host_test import grant_host_permissions
from imbue.mngr_latchkey.remote.mock_outer_host_test import machine_script_line
from imbue.mngr_latchkey.remote.mock_outer_host_test import machine_script_variables
from imbue.mngr_latchkey.remote.mock_outer_host_test import store_accounts
from imbue.mngr_latchkey.remote.mock_outer_host_test import store_document
from imbue.mngr_latchkey.remote.mock_outer_host_test import stub_machine
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

# Where the machine keeps the key its gateway runs under, in RAM.
_MACHINE_KEY_PATH = "/run/mngr-latchkey/gateway_encryption_key"


def _credentials_of(
    tmp_path: Path,
    host_id: HostId,
    outer: OuterHostInterface,
    machine_accounts: Mapping[str, Sequence[str]] | None = None,
) -> MachineCredentials:
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts=machine_accounts)
    return MachineCredentials(host=outer, latchkey=latchkey, host_id=host_id)


def _merge_line(outer: OuterHostInterface) -> str:
    """What the last script told the machine's CLI to take out of the bundle it carried."""
    return machine_script_line(outer, "auth re-encrypt")


def test_connecting_a_service_carries_it_to_the_machine(tmp_path: Path) -> None:
    """A sign-in that happened here is not a connection until the machine has it."""
    host_id = HostId.generate()
    outer = stub_machine({})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["a@example.com"]})

    credentials.connect_service("slack", "a@example.com")

    assert as_stub(outer).machine_accounts == {"slack": ["a@example.com"]}


def test_connecting_a_service_keeps_what_the_machine_already_had(tmp_path: Path) -> None:
    """The machine's other accounts -- and whatever it refreshed -- survive the handover.

    The handover is scoped to the one account that was connected: the merge on
    the machine overwrites only that account, so a sibling account the machine
    holds (and may have refreshed since this computer last read it) is never
    written over with this computer's copy of it.
    """
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["already@example.com"]})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["new@example.com"]})

    credentials.connect_service("slack", "new@example.com")

    assert as_stub(outer).machine_accounts == {"slack": ["already@example.com", "new@example.com"]}


def test_connecting_a_second_account_leaves_the_machines_first_alone(tmp_path: Path) -> None:
    """Signing in to a second account must not write over the machine's copy of the first.

    The machine store here holds a *stale* copy of the first account beside the
    fresh second one; an unscoped handover would ship both and replace the
    machine's own (fresher) first account with the stale copy.
    """
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["first@example.com"]})
    credentials = _credentials_of(
        tmp_path, host_id, outer, machine_accounts={"slack": ["first@example.com", "second@example.com"]}
    )

    credentials.connect_service("slack", "second@example.com")

    assert '--account "$_lk_account"' in _merge_line(outer)
    assert machine_script_variables(outer)["_lk_account"] == "second@example.com"
    assert as_stub(outer).machine_accounts == {"slack": ["first@example.com", "second@example.com"]}


def test_connecting_without_an_account_hands_over_every_stored_account(tmp_path: Path) -> None:
    """The empty account means "the whole service": what pre-account-scoping requests ask for."""
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["old@example.com"]})
    credentials = _credentials_of(
        tmp_path, host_id, outer, machine_accounts={"slack": ["a@example.com", "b@example.com"]}
    )

    credentials.connect_service("slack", "")

    assert "--account" not in _merge_line(outer)
    assert as_stub(outer).machine_accounts == {"slack": ["a@example.com", "b@example.com"]}


def test_connecting_a_service_costs_one_round_trip_and_no_read_back(tmp_path: Path) -> None:
    """A push pushes only; the read-back belongs to the read that comes next.

    A read-back per push would re-run the machine-side re-encrypt (a Node
    startup plus an SFTP transfer) once more for every change made.
    """
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["already@example.com"]})
    as_stub(outer).remote_files[_MACHINE_KEY_PATH] = MACHINE_KEY.encode("utf-8")
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["new@example.com"]})
    data_dir = plugin_data_dir(credentials.latchkey.latchkey_directory)

    credentials.connect_service("slack", "new@example.com")

    # The push itself did not read the machine back...
    assert len(as_stub(outer).recorded) == 1
    assert store_accounts(machine_credentials_path(data_dir, host_id).read_bytes()) == {"slack": ["new@example.com"]}

    # ... the next read is what makes this computer's copy current.
    credentials.refresh()
    assert store_accounts(machine_credentials_path(data_dir, host_id).read_bytes()) == {
        "slack": ["already@example.com", "new@example.com"]
    }


def test_disconnecting_an_account_clears_it_on_the_machine(tmp_path: Path) -> None:
    """Clearing the cache instead would leave the machine's agents still able to use it."""
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["gone@example.com", "kept@example.com"]})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={})

    credentials.disconnect_account("slack", "gone@example.com")

    assert as_stub(outer).machine_accounts == {"slack": ["kept@example.com"]}


def test_disconnecting_an_account_is_visible_on_the_next_read(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["gone@example.com", "kept@example.com"]})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={})

    credentials.disconnect_account("slack", "gone@example.com")
    credentials.refresh()

    data_dir = plugin_data_dir(credentials.latchkey.latchkey_directory)
    assert store_accounts(machine_credentials_path(data_dir, host_id).read_bytes()) == {"slack": ["kept@example.com"]}


def test_disconnecting_the_last_account_leaves_a_machine_holding_nothing(tmp_path: Path) -> None:
    """A machine emptied of its last account reads as one holding nothing, not as one that cannot be read."""
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["gone@example.com"]})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["gone@example.com"]})

    credentials.disconnect_account("slack", "gone@example.com")
    fetched = credentials.refresh()

    assert fetched.credentials is None
    data_dir = plugin_data_dir(credentials.latchkey.latchkey_directory)
    assert not machine_credentials_path(data_dir, host_id).exists()


def test_an_operation_on_a_machine_this_computer_never_provisioned_is_refused(tmp_path: Path) -> None:
    """Without the machine's key there is nothing to encrypt for it or read from it."""
    latchkey = desktop_latchkey(tmp_path, host_id=HostId.generate(), machine_accounts={})
    outer = stub_machine({})
    credentials = MachineCredentials(host=outer, latchkey=latchkey, host_id=HostId.generate())

    with pytest.raises(MachineCredentialsError, match="never been provisioned"):
        credentials.disconnect_account("slack", "a@example.com")


def test_a_read_adopts_what_a_machine_from_an_earlier_build_holds(tmp_path: Path) -> None:
    """No migration: such a machine is already holding what was synced to it."""
    host_id = HostId.generate()
    latchkey = desktop_latchkey(
        tmp_path, host_id=host_id, desktop_accounts={"slack": ["a@example.com"]}, machine_accounts=None
    )
    grant_host_permissions(latchkey, host_id, SLACK_GRANTED)
    outer = stub_machine({"slack": ["synced-before@example.com"]})

    MachineCredentials(host=outer, latchkey=latchkey, host_id=host_id).refresh()

    data_dir = plugin_data_dir(latchkey.latchkey_directory)
    assert store_accounts(machine_credentials_path(data_dir, host_id).read_bytes()) == {
        "slack": ["synced-before@example.com"]
    }


def test_a_machine_store_written_here_is_not_lost_by_a_later_operation(tmp_path: Path) -> None:
    """The cache is replaced by what the machine holds, so an operation must land first."""
    host_id = HostId.generate()
    outer = stub_machine({})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={})
    data_dir = plugin_data_dir(credentials.latchkey.latchkey_directory)
    materialize_machine_store(credentials.latchkey.latchkey_directory, data_dir, host_id)
    store_machine_encryption_key(data_dir, host_id, SecretStr(MACHINE_KEY))
    write_machine_credentials(
        machine_credentials_path(data_dir, host_id).parent, store_document({"slack": ["fresh@example.com"]}), "2"
    )

    credentials.connect_service("slack", "fresh@example.com")

    assert as_stub(outer).machine_accounts == {"slack": ["fresh@example.com"]}


def test_a_push_refuses_a_machine_rekeyed_out_from_under_this_computer(tmp_path: Path) -> None:
    """A different key in the machine's tmpfs means another install re-keyed it; ours must not clobber it.

    A bundle is encrypted with the key this computer recorded, so merging it
    into such a machine would leave its gateway a store it cannot read. The
    push fails (and keeps failing) until the next provisioning pass adopts the
    machine's key into this computer's record. Reading is unaffected: the
    machine re-encrypts with whatever key it is actually running under.
    """
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["a@example.com"]})
    as_stub(outer).remote_files[_MACHINE_KEY_PATH] = b"another-installs-key"
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["a@example.com"]})

    with pytest.raises(RemoteGatewayError, match="different key"):
        credentials.connect_service("slack", "a@example.com")

    credentials.refresh()


def test_a_machines_key_is_restored_once_and_then_left_alone(tmp_path: Path) -> None:
    """The tmpfs key is written back only when a reboot wiped it, never over a live copy."""
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["a@example.com"]})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={})

    credentials.refresh()
    credentials.refresh()

    key_writes = [entry for entry in as_stub(outer).written if entry.path == _MACHINE_KEY_PATH]
    assert len(key_writes) == 1


def test_refreshing_adopts_what_the_machine_holds(tmp_path: Path) -> None:
    """Including an account connected from another of the user's computers."""
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["elsewhere@example.com"]})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={})

    credentials.refresh()

    data_dir = plugin_data_dir(credentials.latchkey.latchkey_directory)
    assert store_accounts(machine_credentials_path(data_dir, host_id).read_bytes()) == {
        "slack": ["elsewhere@example.com"]
    }


def test_refreshing_pushes_nothing_of_its_own(tmp_path: Path) -> None:
    """A change made here reaches the machine when it is made, not when a pass notices it."""
    host_id = HostId.generate()
    outer = stub_machine({})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["a@example.com"]})

    credentials.refresh()

    assert as_stub(outer).machine_accounts == {}


def test_refreshing_keeps_a_credential_no_permission_stands_behind(tmp_path: Path) -> None:
    """Dropping a machine's grants and signing an account out of it are separate asks.

    An account with no grants is one the user connected and has not signed out
    of; taking the credential away would make re-granting it a fresh sign-in,
    and nothing arrives on a machine that was not connected for it, so there is
    no leak to clean up either.
    """
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["a@example.com"]}, machine_permissions='{"rules": []}')
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={})

    credentials.refresh()

    assert as_stub(outer).machine_accounts == {"slack": ["a@example.com"]}


def test_connecting_a_service_leaves_the_machines_other_services_alone(tmp_path: Path) -> None:
    """The merge takes what it was told to take, not everything this computer happens to hold.

    This computer's copy is only as fresh as the last time it read the machine,
    and the machine has been refreshing its own tokens since. Handing back a
    stale copy of a service nobody touched would give the machine a refresh
    token it had already rotated away.
    """
    host_id = HostId.generate()
    outer = stub_machine({"github": ["kept@example.com"]})
    credentials = _credentials_of(
        tmp_path,
        host_id,
        outer,
        machine_accounts={"slack": ["new@example.com"], "github": ["stale@example.com"]},
    )

    credentials.connect_service("slack", "new@example.com")

    assert as_stub(outer).machine_accounts == {
        "github": ["kept@example.com"],
        "slack": ["new@example.com"],
    }


def test_connecting_a_service_tells_the_machine_exactly_what_to_take(tmp_path: Path) -> None:
    """Scoped where the write happens, so a wider bundle could not widen the write."""
    host_id = HostId.generate()
    outer = stub_machine({})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["a@example.com"]})

    credentials.connect_service("slack", "a@example.com")

    assert '--services "$_lk_service" --account "$_lk_account"' in _merge_line(outer)
    variables = machine_script_variables(outer)
    assert (variables["_lk_service"], variables["_lk_account"]) == ("slack", "a@example.com")


def test_a_read_pulls_what_the_machine_holds_into_its_machine_store(tmp_path: Path) -> None:
    """The machine owns its credentials -- including whatever its gateway refreshed."""
    host_id = HostId.generate()
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={})
    grant_host_permissions(latchkey, host_id, SLACK_GRANTED)
    outer = stub_machine({"slack": ["signed-in@example.com"]}, machine_permissions=SLACK_GRANTED)

    MachineCredentials(host=outer, latchkey=latchkey, host_id=host_id).refresh()

    data_dir = plugin_data_dir(latchkey.latchkey_directory)
    mirrored = store_accounts(machine_credentials_path(data_dir, host_id).read_bytes())
    assert mirrored == {"slack": ["signed-in@example.com"]}


def test_a_read_adopts_an_account_this_computer_has_never_seen(tmp_path: Path) -> None:
    """Connected on the user's other computer, or on the machine itself: not ours to delete."""
    host_id = HostId.generate()
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={})
    grant_host_permissions(latchkey, host_id, SLACK_GRANTED)
    outer = stub_machine({"slack": ["elsewhere@example.com"]}, machine_permissions=SLACK_GRANTED)

    MachineCredentials(host=outer, latchkey=latchkey, host_id=host_id).refresh()

    assert as_stub(outer).machine_accounts == {"slack": ["elsewhere@example.com"]}
    data_dir = plugin_data_dir(latchkey.latchkey_directory)
    assert store_accounts(machine_credentials_path(data_dir, host_id).read_bytes()) == {
        "slack": ["elsewhere@example.com"]
    }


def test_a_read_of_a_machine_this_computer_never_provisioned_is_refused(tmp_path: Path) -> None:
    """Without the machine's key there is nothing it could re-encrypt its store for."""
    host_id = HostId.generate()
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    latchkey = Latchkey(latchkey_directory=latchkey_directory, latchkey_binary=str(fake_latchkey_binary(tmp_path)))
    outer = stub_machine({})

    with pytest.raises(MachineCredentialsError, match="never been provisioned"):
        MachineCredentials(host=outer, latchkey=latchkey, host_id=host_id).refresh()

    assert as_stub(outer).recorded == []


def test_a_read_adopts_a_policy_this_computer_disagrees_with(tmp_path: Path) -> None:
    """The machine owns the policy, because the user's other computer can push one too.

    A desktop that kept its own copy would revert what the other one granted,
    every time the Permissions tab was opened.
    """
    host_id = HostId.generate()
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={})
    grant_host_permissions(latchkey, host_id, '{"rules": []}')
    outer = stub_machine({}, machine_permissions=SLACK_GRANTED)

    MachineCredentials(host=outer, latchkey=latchkey, host_id=host_id).refresh()

    assert permissions_path_for_host(plugin_data_dir(latchkey.latchkey_directory), host_id).read_text() == (
        SLACK_GRANTED
    )
    assert as_stub(outer).machine_permissions == SLACK_GRANTED


def test_a_read_adopts_the_policy_of_a_host_this_computer_has_none_for(tmp_path: Path) -> None:
    """How a second computer picks up what the first one granted."""
    host_id = HostId.generate()
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={})
    outer = stub_machine({}, machine_permissions=SLACK_GRANTED)

    MachineCredentials(host=outer, latchkey=latchkey, host_id=host_id).refresh()

    assert permissions_path_for_host(plugin_data_dir(latchkey.latchkey_directory), host_id).read_text() == (
        SLACK_GRANTED
    )


def test_a_read_seeds_a_machine_that_has_no_policy_of_its_own(tmp_path: Path) -> None:
    """A gateway with no permissions file permits everything, so it is not left with none."""
    host_id = HostId.generate()
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={})
    grant_host_permissions(latchkey, host_id, SLACK_GRANTED)
    outer = stub_machine({}, machine_permissions=None)

    MachineCredentials(host=outer, latchkey=latchkey, host_id=host_id).refresh()

    assert as_stub(outer).machine_permissions == SLACK_GRANTED


def test_a_grant_lands_both_halves_on_the_machine_in_one_round_trip(tmp_path: Path) -> None:
    """The credential and the policy that grants it travel as one remote command."""
    host_id = HostId.generate()
    outer = stub_machine({"github": ["kept@example.com"]})
    as_stub(outer).remote_files[_MACHINE_KEY_PATH] = MACHINE_KEY.encode("utf-8")
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["a@example.com"]})

    credentials.connect_service_with_permissions("slack", "a@example.com", SLACK_GRANTED)

    assert as_stub(outer).machine_accounts == {"github": ["kept@example.com"], "slack": ["a@example.com"]}
    assert as_stub(outer).machine_permissions == SLACK_GRANTED
    assert len(as_stub(outer).recorded) == 1
    assert as_stub(outer).written == []


def test_a_grant_reads_nothing_back_like_every_other_push(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["already@example.com"]})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["new@example.com"]})
    data_dir = plugin_data_dir(credentials.latchkey.latchkey_directory)

    credentials.connect_service_with_permissions("slack", "new@example.com", SLACK_GRANTED)

    assert store_accounts(machine_credentials_path(data_dir, host_id).read_bytes()) == {"slack": ["new@example.com"]}
    assert as_stub(outer).machine_accounts == {"slack": ["already@example.com", "new@example.com"]}


def test_a_permissions_snapshot_reaches_a_machine_that_has_lost_its_key(tmp_path: Path) -> None:
    """A policy is not encrypted, so a rebooted machine's gateway still gets the rules it will enforce."""
    host_id = HostId.generate()
    outer = stub_machine({})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={})

    credentials.set_permissions(SLACK_GRANTED)

    assert as_stub(outer).machine_permissions == SLACK_GRANTED
    assert len(as_stub(outer).recorded) == 1
    assert as_stub(outer).written == []


def test_a_grant_to_a_machine_this_computer_never_provisioned_is_refused(tmp_path: Path) -> None:
    latchkey = desktop_latchkey(tmp_path, host_id=HostId.generate(), machine_accounts={})
    outer = stub_machine({})
    credentials = MachineCredentials(host=outer, latchkey=latchkey, host_id=HostId.generate())

    with pytest.raises(MachineCredentialsError, match="never been provisioned"):
        credentials.connect_service_with_permissions("slack", "a@example.com", SLACK_GRANTED)

    assert as_stub(outer).recorded == []


def _desktop_config(credentials: MachineCredentials, registered_services: dict[str, object]) -> None:
    (credentials.latchkey.latchkey_directory / CONFIG_FILENAME).write_text(
        json.dumps(
            {
                # What a desktop's file also carries, and a machine must not:
                # the browser this computer signs in with.
                "browser": {"executablePath": "/Applications/Chromium.app", "source": "system"},
                "settings": {"keyringServiceName": "minds-desktop"},
                "registeredServices": registered_services,
            }
        )
    )


def test_connecting_carries_this_computers_half_of_the_config_ahead_of_the_credential(tmp_path: Path) -> None:
    """A machine's config is seeded at provisioning and never read back, so every connect carries it fresh.

    A whole snapshot, like the policy: the hidden built-in services and every
    registered service, bundled and custom -- but nothing of this computer's
    own file beyond that, whose browser and keyring settings belong here.
    """
    host_id = HostId.generate()
    outer = stub_machine({})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"slack": ["a@example.com"]})
    _desktop_config(credentials, {"custom_example_com": build_custom_service_registration("example.com", "https")})

    credentials.connect_service("slack", "a@example.com")

    machine_config = json.loads(as_stub(outer).config_json or "")
    assert set(machine_config) == {"settings", "registeredServices"}
    assert "notion" in machine_config["settings"]["hideBuiltinServices"]
    assert "keyringServiceName" not in machine_config["settings"]
    assert machine_config["registeredServices"]["custom_example_com"] == {"baseApiUrl": "https://example.com/"}
    assert "claude-ai" in machine_config["registeredServices"]
    script = as_stub(outer).recorded[-1].command
    assert script.index(f"/{CONFIG_FILENAME}") < script.index("auth re-encrypt")
    assert as_stub(outer).machine_accounts == {"slack": ["a@example.com"]}


def test_connecting_a_custom_service_this_computer_has_not_registered_is_refused(tmp_path: Path) -> None:
    # A credential the machine's gateway could never route a request to is
    # worse than no credential: it reads as connected and is silently unusable.
    host_id = HostId.generate()
    outer = stub_machine({})
    credentials = _credentials_of(tmp_path, host_id, outer, machine_accounts={"custom_example_com": [""]})
    _desktop_config(credentials, {})

    with pytest.raises(MachineCredentialsError, match="no registration"):
        credentials.connect_service("custom_example_com", "")

    assert as_stub(outer).latchkey_commands == []
