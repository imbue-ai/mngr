import json
import stat
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.core import merge_minds_latchkey_config
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.remote._machine import GATEWAY_ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.remote._machine import RemoteCredentialClear
from imbue.mngr_latchkey.remote._machine import RemoteCredentialMerge
from imbue.mngr_latchkey.remote._machine import RemoteMachineState
from imbue.mngr_latchkey.remote._machine import RemoteStateRequest
from imbue.mngr_latchkey.remote._machine import RemoteStateUpdate
from imbue.mngr_latchkey.remote._machine import _MAX_REMOTE_COMMAND_BYTES
from imbue.mngr_latchkey.remote._machine import apply_remote_state
from imbue.mngr_latchkey.remote._machine import read_remote_state
from imbue.mngr_latchkey.remote._mirror import latchkey_for_machine
from imbue.mngr_latchkey.remote._transfer import adopt_machine_desktop_egress_rules
from imbue.mngr_latchkey.remote._transfer import clear_remote_credentials
from imbue.mngr_latchkey.remote._transfer import fetch_machine_state
from imbue.mngr_latchkey.remote._transfer import push_credentials
from imbue.mngr_latchkey.remote._transfer import push_credentials_with_permissions
from imbue.mngr_latchkey.remote._transfer import push_permissions_and_desktop_egress_rules
from imbue.mngr_latchkey.remote._transfer import push_permissions_snapshot
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.remote.mock_outer_host_test import FakeVps
from imbue.mngr_latchkey.remote.mock_outer_host_test import MACHINE_KEY
from imbue.mngr_latchkey.remote.mock_outer_host_test import as_vps
from imbue.mngr_latchkey.remote.mock_outer_host_test import desktop_latchkey
from imbue.mngr_latchkey.remote.mock_outer_host_test import fake_vps
from imbue.mngr_latchkey.remote.mock_outer_host_test import store_accounts
from imbue.mngr_latchkey.remote.mock_outer_host_test import store_document
from imbue.mngr_latchkey.remote.package import REMOTE_EXTENSIONS_DIR_NAME
from imbue.mngr_latchkey.store import DESKTOP_EGRESS_RULES_FILENAME
from imbue.mngr_latchkey.store import desktop_egress_rules_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

_SLACK_ANY = '{"rules": [{"slack-api": ["any"]}]}'
_SLACK_ROUTED = '{\n  "slack": true\n}\n'
_CUSTOM_CONFIG = merge_minds_latchkey_config(
    None, {"custom_api_example_com": build_custom_service_registration("api.example.com", "https")}
)


def _merge(bundle: bytes, account: str) -> RemoteCredentialMerge:
    return RemoteCredentialMerge(service_name="slack", account=account, bundle=bundle, data_format_version="2")


def _apply(vps: FakeVps, update: RemoteStateUpdate, failure_description: str) -> None:
    apply_remote_state(cast(OuterHostInterface, vps), update, failure_description=failure_description)


def _read(vps: FakeVps, request: RemoteStateRequest) -> RemoteMachineState:
    return read_remote_state(cast(OuterHostInterface, vps), request, failure_description="read")


def _grant(vps: FakeVps, bundle: bytes, account: str, permissions_json: str = _SLACK_ANY) -> None:
    _apply(
        vps,
        RemoteStateUpdate(
            encryption_key=SecretStr(MACHINE_KEY),
            credential_merge=_merge(bundle, account),
            permissions_json=permissions_json,
        ),
        failure_description="grant",
    )


# The scripts, run by a real shell against the fake machine.


def test_the_grant_merges_the_named_account_and_installs_the_policy(tmp_path: Path) -> None:
    """One apply does both: a scoped merge into the store, then an atomic 0600 policy."""
    vps = as_vps(fake_vps(tmp_path, {"github": ["kept@example.com"]}))
    vps.run_under_key(MACHINE_KEY)
    bundle = store_document({"slack": ["a@example.com", "not-asked-for@example.com"]}, MACHINE_KEY)

    _grant(vps, bundle, "a@example.com")

    assert vps.machine_accounts() == {"github": ["kept@example.com"], "slack": ["a@example.com"]}
    permissions_path = vps.latchkey_dir / PERMISSIONS_CONFIG_FILENAME
    assert permissions_path.read_text() == _SLACK_ANY
    assert stat.S_IMODE(permissions_path.stat().st_mode) == 0o600
    # Neither the scratch directory nor the rename-in temp file is left behind.
    assert vps.latchkey_dir_entries() == [
        CREDENTIALS_STORE_FILENAME,
        UPSTREAM_DATA_FORMAT_VERSION_FILENAME,
        REMOTE_EXTENSIONS_DIR_NAME,
        PERMISSIONS_CONFIG_FILENAME,
    ]
    assert vps.secrets_dir_entries() == [GATEWAY_ENCRYPTION_KEY_FILENAME]


def test_the_grant_without_an_account_hands_over_the_whole_service(tmp_path: Path) -> None:
    vps = as_vps(fake_vps(tmp_path))
    vps.run_under_key(MACHINE_KEY)
    bundle = store_document({"slack": ["a@example.com", "b@example.com"]}, MACHINE_KEY)

    _grant(vps, bundle, "")

    assert vps.machine_accounts() == {"slack": ["a@example.com", "b@example.com"]}


def test_a_connect_merges_without_touching_the_machines_policy(tmp_path: Path) -> None:
    """A connect that carries no snapshot leaves whatever policy the machine is enforcing alone."""
    vps = as_vps(fake_vps(tmp_path, machine_permissions='{"rules": []}'))
    vps.run_under_key(MACHINE_KEY)

    _apply(
        vps,
        RemoteStateUpdate(
            encryption_key=SecretStr(MACHINE_KEY),
            credential_merge=_merge(store_document({"slack": ["a@example.com"]}, MACHINE_KEY), "a@example.com"),
        ),
        failure_description="connect",
    )

    assert vps.machine_accounts() == {"slack": ["a@example.com"]}
    assert vps.machine_permissions() == '{"rules": []}'


def test_a_disconnect_takes_one_account_away_and_leaves_its_siblings(tmp_path: Path) -> None:
    """Signing out of one account of a service must not sign the machine out of the others."""
    vps = as_vps(
        fake_vps(tmp_path, {"slack": ["gone@example.com", "kept@example.com"], "github": ["also-kept@example.com"]})
    )
    vps.run_under_key(MACHINE_KEY)

    _apply(
        vps,
        RemoteStateUpdate(credential_clear=RemoteCredentialClear(service_name="slack", account="gone@example.com")),
        failure_description="disconnect",
    )

    assert vps.machine_accounts() == {"slack": ["kept@example.com"], "github": ["also-kept@example.com"]}


def test_a_credential_change_on_a_machine_with_no_key_fails_rather_than_guessing(tmp_path: Path) -> None:
    """Clearing rewrites the store, so it needs the machine's key like every other credential change."""
    vps = as_vps(fake_vps(tmp_path, {"slack": ["gone@example.com"]}))

    with pytest.raises(RemoteGatewayError, match="has no encryption key"):
        _apply(
            vps,
            RemoteStateUpdate(
                credential_clear=RemoteCredentialClear(service_name="slack", account="gone@example.com")
            ),
            failure_description="disconnect",
        )

    assert vps.machine_accounts() == {"slack": ["gone@example.com"]}


def test_a_refused_update_abandons_nothing(tmp_path: Path) -> None:
    """The store is removed only once the machine has taken the key that comes with it.

    An abandon is sent for a machine the read found keyless; one that another
    computer re-keyed in the meantime refuses the key and must keep the store
    its gateway is serving.
    """
    vps = as_vps(fake_vps(tmp_path, {"slack": ["live@example.com"]}, machine_permissions='{"rules": []}'))
    vps.run_under_key("another-computers-key")

    with pytest.raises(RemoteGatewayError, match="different key"):
        _apply(
            vps,
            RemoteStateUpdate(
                is_credential_store_abandoned=True,
                encryption_key=SecretStr(MACHINE_KEY),
                permissions_json=_SLACK_ANY,
                is_gateway_restarted=True,
            ),
            failure_description="provision",
        )

    assert vps.machine_accounts() == {"slack": ["live@example.com"]}
    assert vps.machine_permissions() == '{"rules": []}'
    assert vps.secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == "another-computers-key"
    assert vps.supervisorctl_calls() == []


def test_a_permissions_snapshot_needs_no_key_at_all(tmp_path: Path) -> None:
    """A policy is not encrypted, so a machine that rebooted still takes one."""
    vps = as_vps(fake_vps(tmp_path))

    _apply(vps, RemoteStateUpdate(permissions_json=_SLACK_ANY), failure_description="policy")

    permissions_path = vps.latchkey_dir / PERMISSIONS_CONFIG_FILENAME
    assert permissions_path.read_text() == _SLACK_ANY
    assert stat.S_IMODE(permissions_path.stat().st_mode) == 0o600
    assert vps.latchkey_dir_entries() == [REMOTE_EXTENSIONS_DIR_NAME, PERMISSIONS_CONFIG_FILENAME]
    assert vps.secrets_dir_entries() == []


def test_a_grant_accepts_a_key_file_with_a_trailing_newline(tmp_path: Path) -> None:
    """Whitespace around a secret is never part of it, however the file got written."""
    vps = as_vps(fake_vps(tmp_path))
    vps.run_under_key(f"{MACHINE_KEY}\n")

    _grant(vps, store_document({"slack": ["a@example.com"]}, MACHINE_KEY), "")

    assert vps.machine_accounts() == {"slack": ["a@example.com"]}


def test_a_failed_merge_leaves_the_policy_alone_and_cleans_up(tmp_path: Path) -> None:
    """Credential first: a policy must never be enforceable before the credential it rides on landed."""
    vps = as_vps(fake_vps(tmp_path, machine_permissions='{"rules": []}'))
    vps.run_under_key(MACHINE_KEY)

    with pytest.raises(RemoteGatewayError):
        _grant(vps, b"not a credential store", "")

    assert vps.machine_permissions() == '{"rules": []}'
    assert vps.latchkey_dir_entries() == [PERMISSIONS_CONFIG_FILENAME]
    assert vps.secrets_dir_entries() == [GATEWAY_ENCRYPTION_KEY_FILENAME]


def test_a_read_answers_with_the_store_as_the_machine_holds_it_and_the_policy(tmp_path: Path) -> None:
    """One read answers with everything the pane needs: the store, its stamp, the key it is under, and the policy."""
    vps = as_vps(fake_vps(tmp_path, {"slack": ["a@example.com"]}, machine_permissions=_SLACK_ANY))
    vps.run_under_key(MACHINE_KEY)

    state = _read(vps, RemoteStateRequest(is_credential_store_included=True))

    assert state.credential_store is not None
    assert store_accounts(state.credential_store.content) == {"slack": ["a@example.com"]}
    # Still under the machine's own key: nothing on the machine decrypts it.
    assert json.loads(state.credential_store.content)["key"] == MACHINE_KEY
    assert state.credential_store.data_format_version == "2"
    assert state.permissions_json == _SLACK_ANY
    assert state.encryption_key is not None and state.encryption_key.get_secret_value() == MACHINE_KEY
    assert state.has_credential_store is True
    assert vps.latchkey_calls() == []
    assert vps.latchkey_dir_entries() == [
        CREDENTIALS_STORE_FILENAME,
        UPSTREAM_DATA_FORMAT_VERSION_FILENAME,
        PERMISSIONS_CONFIG_FILENAME,
    ]
    assert vps.secrets_dir_entries() == [GATEWAY_ENCRYPTION_KEY_FILENAME]


def test_a_read_answers_for_a_machine_that_holds_nothing_yet(tmp_path: Path) -> None:
    """Nothing connected and nothing provisioned is an answer, not an error."""
    vps = as_vps(fake_vps(tmp_path))

    state = _read(vps, RemoteStateRequest(is_credential_store_included=True))

    assert state.credential_store is None
    assert state.permissions_json is None
    assert state.config_json is None
    assert state.encryption_key is None
    assert state.listen_password is None
    assert state.has_credential_store is False


def test_a_read_of_a_rebooted_machine_hands_it_its_key_and_answers_with_the_store(tmp_path: Path) -> None:
    """The key the read answers with is the one handed back, which is what the store is re-encrypted from here."""
    vps = as_vps(fake_vps(tmp_path, {"slack": ["a@example.com"]}))

    state = _read(
        vps, RemoteStateRequest(is_credential_store_included=True, fallback_encryption_key=SecretStr(MACHINE_KEY))
    )

    assert state.credential_store is not None
    assert state.encryption_key is not None and state.encryption_key.get_secret_value() == MACHINE_KEY
    assert vps.secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == MACHINE_KEY


def test_a_read_brings_back_a_store_the_machine_holds_no_key_for(tmp_path: Path) -> None:
    """Nothing on the machine opens the store, so a machine that lost its key still hands it over for a key to be tried here."""
    vps = as_vps(fake_vps(tmp_path, {"slack": ["a@example.com"]}))

    state = _read(vps, RemoteStateRequest(is_credential_store_included=True))

    assert state.credential_store is not None
    assert json.loads(state.credential_store.content)["key"] == MACHINE_KEY
    assert state.encryption_key is None


def test_a_read_leaves_a_live_key_alone(tmp_path: Path) -> None:
    """The recorded key is written back only to a machine that lost its own, never over a live copy."""
    vps = as_vps(fake_vps(tmp_path))
    vps.run_under_key("the-machines-live-key")

    _read(vps, RemoteStateRequest(fallback_encryption_key=SecretStr(MACHINE_KEY)))

    assert vps.secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == "the-machines-live-key"


def test_a_read_answers_the_policy_of_a_machine_without_a_key_when_no_store_is_asked_for(tmp_path: Path) -> None:
    """A rebooted machine can still say what its gateway enforces; only the store needs the key."""
    vps = as_vps(fake_vps(tmp_path, {"slack": ["a@example.com"]}, machine_permissions=_SLACK_ANY))

    state = _read(vps, RemoteStateRequest())

    assert state.permissions_json == _SLACK_ANY
    assert state.credential_store is None
    assert state.has_credential_store is True


def test_a_read_answers_the_desktop_egress_rules_even_of_a_machine_that_lost_its_key(tmp_path: Path) -> None:
    """The rules are not encrypted, so a rebooted machine still says what its router routes."""
    vps = as_vps(fake_vps(tmp_path, {"slack": ["a@example.com"]}))
    vps.hold_desktop_egress_rules(_SLACK_ROUTED)

    state = _read(vps, RemoteStateRequest())

    assert state.desktop_egress_rules_json == _SLACK_ROUTED
    assert state.encryption_key is None


def test_the_permissions_and_rules_update_installs_both_files(tmp_path: Path) -> None:
    """Both files are whole snapshots installed atomically at 0600, and neither needs the machine's key."""
    vps = as_vps(fake_vps(tmp_path))
    vps.hold_desktop_egress_rules("{}")

    _apply(
        vps,
        RemoteStateUpdate(desktop_egress_rules_json=_SLACK_ROUTED, permissions_json=_SLACK_ANY),
        failure_description="policy and rules",
    )

    rules_path = vps.latchkey_dir / DESKTOP_EGRESS_RULES_FILENAME
    assert rules_path.read_text() == _SLACK_ROUTED
    assert stat.S_IMODE(rules_path.stat().st_mode) == 0o600
    assert vps.machine_permissions() == _SLACK_ANY
    # Neither rename-in temp file is left behind.
    assert vps.latchkey_dir_entries() == [
        REMOTE_EXTENSIONS_DIR_NAME,
        PERMISSIONS_CONFIG_FILENAME,
        DESKTOP_EGRESS_RULES_FILENAME,
    ]
    assert vps.secrets_dir_entries() == []


def test_a_failed_merge_leaves_the_desktop_egress_rules_alone(tmp_path: Path) -> None:
    vps = as_vps(fake_vps(tmp_path))
    vps.run_under_key(MACHINE_KEY)
    vps.hold_desktop_egress_rules("{}")

    with pytest.raises(RemoteGatewayError):
        _apply(
            vps,
            RemoteStateUpdate(
                encryption_key=SecretStr(MACHINE_KEY),
                credential_merge=_merge(b"not a credential store", ""),
                desktop_egress_rules_json=_SLACK_ROUTED,
                permissions_json=_SLACK_ANY,
            ),
            failure_description="grant",
        )

    assert vps.machine_desktop_egress_rules() == "{}"
    assert vps.latchkey_dir_entries() == [DESKTOP_EGRESS_RULES_FILENAME]


def test_an_update_without_desktop_egress_rules_leaves_the_machines_alone(tmp_path: Path) -> None:
    vps = as_vps(fake_vps(tmp_path))
    vps.hold_desktop_egress_rules(_SLACK_ROUTED)

    _apply(vps, RemoteStateUpdate(permissions_json=_SLACK_ANY), failure_description="policy")

    assert vps.machine_desktop_egress_rules() == _SLACK_ROUTED


def test_a_connect_installs_the_config_ahead_of_the_credential(tmp_path: Path) -> None:
    """The config travels like the policy -- a whole snapshot, installed atomically -- and lands first.

    A gateway with no entry for a service cannot route a request to it, so a
    merge the machine refuses still leaves the config installed, and never a
    credential without the config that makes it usable.
    """
    vps = as_vps(fake_vps(tmp_path))
    vps.run_under_key(MACHINE_KEY)

    with pytest.raises(RemoteGatewayError):
        _apply(
            vps,
            RemoteStateUpdate(
                encryption_key=SecretStr(MACHINE_KEY),
                config_json=_CUSTOM_CONFIG,
                credential_merge=_merge(b"not a credential store", ""),
            ),
            failure_description="connect",
        )

    assert vps.machine_config() == _CUSTOM_CONFIG
    assert stat.S_IMODE((vps.latchkey_dir / CONFIG_FILENAME).stat().st_mode) == 0o600
    assert vps.machine_accounts() == {}


def test_a_connect_without_a_config_leaves_the_machines_alone(tmp_path: Path) -> None:
    vps = as_vps(fake_vps(tmp_path))
    vps.run_under_key(MACHINE_KEY)
    vps.hold_config('{"settings": {"theme": "dark"}}')

    _apply(
        vps,
        RemoteStateUpdate(
            encryption_key=SecretStr(MACHINE_KEY),
            credential_merge=_merge(store_document({"slack": ["a@example.com"]}, MACHINE_KEY), "a@example.com"),
        ),
        failure_description="connect",
    )

    assert vps.machine_config() == '{"settings": {"theme": "dark"}}'
    assert vps.machine_accounts() == {"slack": ["a@example.com"]}


# The transfers, end to end.


def _push_grant(
    tmp_path: Path, host_id: HostId, outer: OuterHostInterface, account: str, permissions_json: str = _SLACK_ANY
) -> None:
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={"slack": ["a@example.com"]})
    machine_latchkey = latchkey_for_machine(latchkey, plugin_data_dir(latchkey.latchkey_directory), host_id)
    push_credentials_with_permissions(
        outer, machine_latchkey, host_id, "slack", account, SecretStr(MACHINE_KEY), permissions_json
    )


def test_a_grant_costs_one_remote_command(tmp_path: Path) -> None:
    """The whole point: no probes, no uploads, no cleanup calls -- one command, one round trip."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"github": ["kept@example.com"]})
    as_vps(outer).run_under_key(MACHINE_KEY)

    _push_grant(tmp_path, host_id, outer, "a@example.com")

    assert len(as_vps(outer).recorded) == 1
    assert as_vps(outer).written == []
    assert as_vps(outer).machine_accounts() == {"github": ["kept@example.com"], "slack": ["a@example.com"]}
    assert as_vps(outer).machine_permissions() == _SLACK_ANY


def test_a_connect_costs_one_remote_command(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"github": ["kept@example.com"]})
    as_vps(outer).run_under_key(MACHINE_KEY)
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={"slack": ["a@example.com"]})
    machine_latchkey = latchkey_for_machine(latchkey, plugin_data_dir(latchkey.latchkey_directory), host_id)

    push_credentials(outer, machine_latchkey, host_id, "slack", "a@example.com", SecretStr(MACHINE_KEY))

    assert len(as_vps(outer).recorded) == 1
    assert as_vps(outer).written == []
    assert as_vps(outer).machine_accounts() == {"github": ["kept@example.com"], "slack": ["a@example.com"]}
    # A connect carries no policy, so the machine's own is untouched.
    assert as_vps(outer).machine_permissions() is None


def test_a_connect_carries_its_config_in_the_same_command(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)
    as_vps(outer).run_under_key(MACHINE_KEY)
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={"custom_api_example_com": ["me"]})
    machine_latchkey = latchkey_for_machine(latchkey, plugin_data_dir(latchkey.latchkey_directory), host_id)

    push_credentials(
        outer,
        machine_latchkey,
        host_id,
        "custom_api_example_com",
        "me",
        SecretStr(MACHINE_KEY),
        config_json=_CUSTOM_CONFIG,
    )

    assert len(as_vps(outer).recorded) == 1
    assert as_vps(outer).machine_config() == _CUSTOM_CONFIG
    assert as_vps(outer).machine_accounts() == {"custom_api_example_com": ["me"]}


def test_a_disconnect_costs_one_remote_command(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"slack": ["gone@example.com", "kept@example.com"]})
    as_vps(outer).run_under_key(MACHINE_KEY)

    clear_remote_credentials(outer, host_id, "slack", "gone@example.com", SecretStr(MACHINE_KEY))

    assert len(as_vps(outer).recorded) == 1
    assert as_vps(outer).written == []
    assert as_vps(outer).machine_accounts() == {"slack": ["kept@example.com"]}


def test_a_disconnect_is_not_refused_by_a_machine_rekeyed_out_from_under_this_computer(tmp_path: Path) -> None:
    """A sign-out must not leave the credential behind because another computer re-keyed the machine.

    A clear brings nothing of this computer's to write, so the recorded key is
    not what it needs -- only the machine's own, which is what it rewrites the
    store with. The recorded key travels only as what a rebooted machine would
    be handed back.
    """
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)
    as_vps(outer).hold({"slack": ["gone@example.com", "kept@example.com"]}, key="another-computers-key")
    as_vps(outer).run_under_key("another-computers-key")

    clear_remote_credentials(outer, host_id, "slack", "gone@example.com", SecretStr(MACHINE_KEY))

    assert as_vps(outer).machine_accounts() == {"slack": ["kept@example.com"]}
    assert as_vps(outer).secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == "another-computers-key"


def test_a_permissions_snapshot_costs_one_remote_command_and_no_home_probe(tmp_path: Path) -> None:
    """The script expands ``$HOME`` itself, so nothing has to resolve the machine's ~/.latchkey first."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)

    push_permissions_snapshot(outer, host_id, _SLACK_ANY)

    assert len(as_vps(outer).recorded) == 1
    assert as_vps(outer).written == []
    assert as_vps(outer).machine_permissions() == _SLACK_ANY


def test_a_permissions_snapshot_is_run_without_its_command_reaching_the_logs(tmp_path: Path) -> None:
    """The document carries the whole policy the machine is to enforce, and is large besides."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)

    push_permissions_snapshot(outer, host_id, _SLACK_ANY)

    assert [entry.is_kept_out_of_logs for entry in as_vps(outer).recorded] == [True]


def test_reading_a_machine_runs_without_its_command_reaching_the_logs(tmp_path: Path) -> None:
    """The read's document carries the machine's key, handed back to a machine that lost it."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"slack": ["a@example.com"]})
    as_vps(outer).run_under_key(MACHINE_KEY)
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={"slack": ["a@example.com"]})

    fetch_machine_state(outer, latchkey, host_id, SecretStr(MACHINE_KEY))

    recorded = as_vps(outer).recorded
    assert recorded != []
    assert all(entry.is_kept_out_of_logs for entry in recorded)


def test_a_permissions_snapshot_this_build_cannot_read_never_reaches_the_machine(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)

    with pytest.raises(RemoteGatewayError, match="unreadable permissions snapshot"):
        push_permissions_snapshot(outer, host_id, '{"rules": "not-a-list"}')

    assert as_vps(outer).recorded == []


def test_permissions_and_desktop_egress_rules_cost_one_remote_command_between_them(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)

    push_permissions_and_desktop_egress_rules(outer, host_id, _SLACK_ANY, _SLACK_ROUTED)

    assert len(as_vps(outer).recorded) == 1
    assert as_vps(outer).written == []
    assert as_vps(outer).machine_permissions() == _SLACK_ANY
    assert as_vps(outer).machine_desktop_egress_rules() == _SLACK_ROUTED


@pytest.mark.parametrize("desktop_egress_rules_json", ["[]", "not json", '{"slack": NaN}'])
def test_desktop_egress_rules_the_router_could_not_read_never_reach_the_machine(
    tmp_path: Path, desktop_egress_rules_json: str
) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)

    with pytest.raises(RemoteGatewayError, match="unreadable desktop egress rules"):
        push_permissions_and_desktop_egress_rules(outer, host_id, _SLACK_ANY, desktop_egress_rules_json)

    assert as_vps(outer).recorded == []


def test_an_unreadable_policy_stops_the_desktop_egress_rules_from_reaching_the_machine_too(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)

    with pytest.raises(RemoteGatewayError, match="unreadable permissions snapshot"):
        push_permissions_and_desktop_egress_rules(outer, host_id, '{"rules": "not-a-list"}', _SLACK_ROUTED)

    assert as_vps(outer).recorded == []


def test_a_read_answers_with_the_desktop_egress_rules_the_machine_holds(tmp_path: Path) -> None:
    host_id = HostId.generate()
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={})
    outer = fake_vps(tmp_path, machine_permissions=_SLACK_ANY)
    as_vps(outer).hold_desktop_egress_rules(_SLACK_ROUTED)

    fetched = fetch_machine_state(outer, latchkey, host_id, SecretStr(MACHINE_KEY))

    assert fetched.desktop_egress_rules_json == _SLACK_ROUTED
    assert fetched.permissions_json == _SLACK_ANY
    assert len(as_vps(outer).recorded) == 1


def test_a_read_of_a_machine_with_no_desktop_egress_rules_file_answers_none(tmp_path: Path) -> None:
    host_id = HostId.generate()
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={})
    outer = fake_vps(tmp_path, machine_permissions=_SLACK_ANY)

    fetched = fetch_machine_state(outer, latchkey, host_id, SecretStr(MACHINE_KEY))

    assert fetched.desktop_egress_rules_json is None


def test_adopting_desktop_egress_rules_stores_them_as_this_computers_copy(tmp_path: Path) -> None:
    host_id = HostId.generate()
    local_path = desktop_egress_rules_path_for_host(plugin_data_dir(tmp_path), host_id)

    adopt_machine_desktop_egress_rules(tmp_path, host_id, _SLACK_ROUTED)

    assert local_path.read_text() == _SLACK_ROUTED


def test_adopting_identical_desktop_egress_rules_does_not_rewrite_the_copy(tmp_path: Path) -> None:
    host_id = HostId.generate()
    local_path = desktop_egress_rules_path_for_host(plugin_data_dir(tmp_path), host_id)
    adopt_machine_desktop_egress_rules(tmp_path, host_id, _SLACK_ROUTED)
    modified_at_before = local_path.stat().st_mtime_ns

    adopt_machine_desktop_egress_rules(tmp_path, host_id, _SLACK_ROUTED)

    assert local_path.stat().st_mtime_ns == modified_at_before


def test_adopting_a_machine_with_no_desktop_egress_rules_removes_this_computers_copy(tmp_path: Path) -> None:
    """This computer must never show rules the machine does not have."""
    host_id = HostId.generate()
    local_path = desktop_egress_rules_path_for_host(plugin_data_dir(tmp_path), host_id)
    adopt_machine_desktop_egress_rules(tmp_path, host_id, _SLACK_ROUTED)

    adopt_machine_desktop_egress_rules(tmp_path, host_id, None)
    # Removing a copy that is already gone is not an error.
    adopt_machine_desktop_egress_rules(tmp_path, host_id, None)

    assert not local_path.exists()


def test_adopting_desktop_egress_rules_this_build_cannot_read_keeps_the_copy_it_has(tmp_path: Path) -> None:
    host_id = HostId.generate()
    local_path = desktop_egress_rules_path_for_host(plugin_data_dir(tmp_path), host_id)
    adopt_machine_desktop_egress_rules(tmp_path, host_id, _SLACK_ROUTED)

    with pytest.raises(RemoteGatewayError, match="cannot read"):
        adopt_machine_desktop_egress_rules(tmp_path, host_id, '["slack"]')

    assert local_path.read_text() == _SLACK_ROUTED


def test_a_grant_to_a_rebooted_machine_lands_in_one_round_trip(tmp_path: Path) -> None:
    """Handing the key back costs nothing extra: it rides in the same document as the grant."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)

    _push_grant(tmp_path, host_id, outer, "a@example.com")

    assert as_vps(outer).written == []
    assert len(as_vps(outer).recorded) == 1
    assert as_vps(outer).secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == MACHINE_KEY
    assert as_vps(outer).machine_accounts() == {"slack": ["a@example.com"]}
    assert as_vps(outer).machine_permissions() == _SLACK_ANY


def test_a_disconnect_to_a_rebooted_machine_lands_in_one_round_trip(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"slack": ["gone@example.com"]})

    clear_remote_credentials(outer, host_id, "slack", "gone@example.com", SecretStr(MACHINE_KEY))

    assert as_vps(outer).written == []
    assert len(as_vps(outer).recorded) == 1
    assert as_vps(outer).machine_accounts() == {}


def test_a_grant_refuses_a_machine_rekeyed_out_from_under_this_computer(tmp_path: Path) -> None:
    """The bundle is encrypted with the recorded key; a re-keyed machine could not read what it would merge."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)
    as_vps(outer).run_under_key("another-computers-key")

    with pytest.raises(RemoteGatewayError, match="different key"):
        _push_grant(tmp_path, host_id, outer, "a@example.com")

    assert as_vps(outer).machine_accounts() == {}
    assert as_vps(outer).machine_permissions() is None


def test_a_grant_with_an_unreadable_policy_never_reaches_the_machine(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)

    with pytest.raises(RemoteGatewayError, match="unreadable permissions snapshot"):
        _push_grant(tmp_path, host_id, outer, "a@example.com", permissions_json='{"rules": "nope"}')

    assert as_vps(outer).recorded == []


def test_a_push_too_large_for_one_command_is_refused_rather_than_split(tmp_path: Path) -> None:
    """No second, slower code path: one shape of push, exercised by every push."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)
    as_vps(outer).run_under_key(MACHINE_KEY)
    permission_count = _MAX_REMOTE_COMMAND_BYTES // 8
    huge_policy = json.dumps({"rules": [{"slack-api": [f"perm-{idx}" for idx in range(permission_count)]}]})

    with pytest.raises(RemoteGatewayError, match="past the"):
        _push_grant(tmp_path, host_id, outer, "a@example.com", permissions_json=huge_policy)

    assert as_vps(outer).recorded == []
    assert as_vps(outer).machine_permissions() is None


def test_a_read_re_encrypts_the_store_for_this_computer_here(tmp_path: Path) -> None:
    """The machine hands its store over as it holds it; the desktop's key never leaves this computer."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"slack": ["a@example.com"]})
    as_vps(outer).run_under_key(MACHINE_KEY)
    latchkey = desktop_latchkey(tmp_path, host_id=host_id)
    desktop_key = load_or_create_encryption_key(latchkey.latchkey_directory).get_secret_value()

    fetched = fetch_machine_state(outer, latchkey, host_id, SecretStr(MACHINE_KEY))

    assert fetched.credentials is not None
    assert store_accounts(fetched.credentials) == {"slack": ["a@example.com"]}
    assert json.loads(fetched.credentials)["key"] == desktop_key
    assert not as_vps(outer).has_received(desktop_key)
    # Nothing ran latchkey on the machine: the store was never decrypted there.
    assert as_vps(outer).latchkey_calls() == []


def test_a_read_re_encrypts_from_the_key_the_machine_runs_under(tmp_path: Path) -> None:
    """A machine another computer re-keyed still reads back: its store is opened with the key it reports."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)
    as_vps(outer).hold({"slack": ["a@example.com"]}, key="another-computers-key")
    as_vps(outer).run_under_key("another-computers-key")
    latchkey = desktop_latchkey(tmp_path, host_id=host_id)

    fetched = fetch_machine_state(outer, latchkey, host_id, SecretStr(MACHINE_KEY))

    assert fetched.credentials is not None
    assert store_accounts(fetched.credentials) == {"slack": ["a@example.com"]}


def test_a_read_opens_the_machines_store_under_its_own_key_whatever_the_operator_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's global key names this computer's store, so it is what the copy is re-encrypted for, never what the machine's store is opened with."""
    monkeypatch.setenv("LATCHKEY_ENCRYPTION_KEY", "operator-key-3307")
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"slack": ["a@example.com"]})
    as_vps(outer).run_under_key(MACHINE_KEY)
    latchkey = desktop_latchkey(tmp_path, host_id=host_id)

    fetched = fetch_machine_state(outer, latchkey, host_id, SecretStr(MACHINE_KEY))

    assert fetched.credentials is not None
    assert json.loads(fetched.credentials)["key"] == "operator-key-3307"
    assert not as_vps(outer).has_received("operator-key-3307")


def test_a_read_of_a_machine_whose_last_account_was_disconnected_answers_no_credentials(tmp_path: Path) -> None:
    """An emptied store is an ordinary machine holding nothing, not a failed read."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"slack": ["gone@example.com"]})
    as_vps(outer).run_under_key(MACHINE_KEY)
    clear_remote_credentials(outer, host_id, "slack", "gone@example.com", SecretStr(MACHINE_KEY))
    latchkey = desktop_latchkey(tmp_path, host_id=host_id)

    fetched = fetch_machine_state(outer, latchkey, host_id, SecretStr(MACHINE_KEY))

    assert fetched.credentials is None
    assert fetched.data_format_version == ""


def test_a_read_still_fails_loudly_when_the_store_cannot_be_re_encrypted(tmp_path: Path) -> None:
    """Only the one known "nothing to re-encrypt" answer is tolerated; any other failure is reported."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"slack": ["a@example.com"]})
    as_vps(outer).run_under_key("not-the-stores-key")
    latchkey = desktop_latchkey(tmp_path, host_id=host_id)

    with pytest.raises(RemoteGatewayError, match="The encryption key may have changed"):
        fetch_machine_state(outer, latchkey, host_id, SecretStr(MACHINE_KEY))


def test_a_read_maps_the_machines_answers_to_what_the_pane_shows(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = fake_vps(tmp_path, {"slack": ["a@example.com"]}, machine_permissions=_SLACK_ANY)
    latchkey = desktop_latchkey(tmp_path, host_id=host_id)

    fetched = fetch_machine_state(outer, latchkey, host_id, SecretStr(MACHINE_KEY))

    assert fetched.credentials is not None
    assert store_accounts(fetched.credentials) == {"slack": ["a@example.com"]}
    assert fetched.data_format_version == "2"
    assert fetched.permissions_json == _SLACK_ANY
    assert len(as_vps(outer).recorded) == 1


def test_a_push_carries_its_secrets_only_in_the_document(tmp_path: Path) -> None:
    """The key rides in the heredoc the script reads on stdin, base64 like every entry, never as a shell word."""
    host_id = HostId.generate()
    outer = fake_vps(tmp_path)
    as_vps(outer).run_under_key(MACHINE_KEY)

    _push_grant(tmp_path, host_id, outer, "a@example.com")

    command = as_vps(outer).recorded[0].command
    assert MACHINE_KEY not in command
    first_line, *document_lines, last_line = command.splitlines()
    assert first_line == "mngr-latchkey apply-state <<'MNGR_LATCHKEY_DOCUMENT_END'"
    assert last_line == "MNGR_LATCHKEY_DOCUMENT_END"
    assert all(len(line.split(" ")) == 2 for line in document_lines)
