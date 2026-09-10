import base64
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.core import merge_minds_latchkey_config
from imbue.mngr_latchkey.custom_services import LoginFlow
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.remote._machine import GATEWAY_ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.remote._machine import TMPFS_SECRETS_DIR
from imbue.mngr_latchkey.remote._mirror import latchkey_for_machine
from imbue.mngr_latchkey.remote._transfer import _CredentialClear
from imbue.mngr_latchkey.remote._transfer import _CredentialMerge
from imbue.mngr_latchkey.remote._transfer import _MAX_MACHINE_SCRIPT_BYTES
from imbue.mngr_latchkey.remote._transfer import _MachineScriptInputs
from imbue.mngr_latchkey.remote._transfer import _MachineScriptOutcome
from imbue.mngr_latchkey.remote._transfer import _READ_CREDENTIALS_PREFIX
from imbue.mngr_latchkey.remote._transfer import _READ_DATA_FORMAT_VERSION_PREFIX
from imbue.mngr_latchkey.remote._transfer import _READ_PERMISSIONS_PREFIX
from imbue.mngr_latchkey.remote._transfer import _outcome_marker
from imbue.mngr_latchkey.remote._transfer import build_machine_read_script
from imbue.mngr_latchkey.remote._transfer import build_machine_script
from imbue.mngr_latchkey.remote._transfer import clear_remote_credentials
from imbue.mngr_latchkey.remote._transfer import push_credentials
from imbue.mngr_latchkey.remote._transfer import push_credentials_with_permissions
from imbue.mngr_latchkey.remote._transfer import push_permissions_snapshot
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.remote.mock_outer_host_test import MACHINE_KEY
from imbue.mngr_latchkey.remote.mock_outer_host_test import RecordedCommand
from imbue.mngr_latchkey.remote.mock_outer_host_test import StubOuter
from imbue.mngr_latchkey.remote.mock_outer_host_test import as_stub
from imbue.mngr_latchkey.remote.mock_outer_host_test import desktop_latchkey
from imbue.mngr_latchkey.remote.mock_outer_host_test import fake_latchkey_binary
from imbue.mngr_latchkey.remote.mock_outer_host_test import store_accounts
from imbue.mngr_latchkey.remote.mock_outer_host_test import store_document
from imbue.mngr_latchkey.remote.mock_outer_host_test import stub_machine
from imbue.mngr_latchkey.store import plugin_data_dir

_MACHINE_KEY_PATH = str(TMPFS_SECRETS_DIR / GATEWAY_ENCRYPTION_KEY_FILENAME)
_SLACK_ANY = '{"rules": [{"slack-api": ["any"]}]}'
_SHELL_TIMEOUT_SECONDS = 30.0


class _FakeMachine:
    """A directory tree standing in for a VPS: its ``$HOME/.latchkey``, its tmpfs key, and a ``latchkey`` on PATH."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.latchkey_dir = self.home / ".latchkey"
        self.latchkey_dir.mkdir(parents=True)
        self.key_file = tmp_path / "run" / GATEWAY_ENCRYPTION_KEY_FILENAME
        self.key_file.parent.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "latchkey").symlink_to(fake_latchkey_binary(bin_dir))
        self.env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.home)}

    def run(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "-c", script], env=self.env, capture_output=True, text=True, timeout=_SHELL_TIMEOUT_SECONDS
        )

    def hold(self, accounts_by_service: Mapping[str, list[str]]) -> None:
        (self.latchkey_dir / CREDENTIALS_STORE_FILENAME).write_bytes(store_document(accounts_by_service))

    def stamp(self, data_format_version: str = "2") -> None:
        """Give the store the format stamp a read answers with beside it."""
        (self.latchkey_dir / UPSTREAM_DATA_FORMAT_VERSION_FILENAME).write_text(data_format_version)

    def store_accounts(self) -> dict[str, list[str]]:
        return store_accounts((self.latchkey_dir / CREDENTIALS_STORE_FILENAME).read_bytes())

    def entries(self) -> list[str]:
        return sorted(path.name for path in self.latchkey_dir.iterdir())

    def config(self) -> dict[str, object] | None:
        config_path = self.latchkey_dir / CONFIG_FILENAME
        return dict(json.loads(config_path.read_text())) if config_path.is_file() else None


def _merge(machine: _FakeMachine, bundle: bytes, account: str, service_name: str = "slack") -> _CredentialMerge:
    return _CredentialMerge(
        machine_key_file=machine.key_file,
        machine_key_sha256=hashlib.sha256(MACHINE_KEY.encode("utf-8")).hexdigest(),
        service_name=service_name,
        account=account,
        bundle=bundle,
        data_format_version="2",
    )


def _clear(machine: _FakeMachine, account: str) -> _CredentialClear:
    return _CredentialClear(machine_key_file=machine.key_file, service_name="slack", account=account)


def _grant_script(machine: _FakeMachine, bundle: bytes, account: str, permissions_json: str = _SLACK_ANY) -> str:
    return build_machine_script(
        _MachineScriptInputs(credential_change=_merge(machine, bundle, account), permissions_json=permissions_json)
    )


# -- The script, run by a real shell --------------------------------------------


def test_the_grant_script_merges_the_named_account_and_installs_the_policy(tmp_path: Path) -> None:
    """One ``sh`` run does what the SFTP-and-commands path did: scoped merge, then an atomic 0600 policy."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    machine.hold({"github": ["kept@example.com"]})
    bundle = store_document({"slack": ["a@example.com", "not-asked-for@example.com"]})

    completed = machine.run(_grant_script(machine, bundle, "a@example.com"))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == _outcome_marker(_MachineScriptOutcome.APPLIED)
    assert machine.store_accounts() == {"github": ["kept@example.com"], "slack": ["a@example.com"]}
    permissions_path = machine.latchkey_dir / PERMISSIONS_CONFIG_FILENAME
    assert permissions_path.read_text() == _SLACK_ANY
    assert stat.S_IMODE(permissions_path.stat().st_mode) == 0o600
    # Neither the scratch directory nor the rename-in temp file is left behind.
    assert machine.entries() == [CREDENTIALS_STORE_FILENAME, PERMISSIONS_CONFIG_FILENAME]


def test_the_grant_script_without_an_account_hands_over_the_whole_service(tmp_path: Path) -> None:
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    bundle = store_document({"slack": ["a@example.com", "b@example.com"]})

    completed = machine.run(_grant_script(machine, bundle, ""))

    assert completed.returncode == 0, completed.stderr
    assert machine.store_accounts() == {"slack": ["a@example.com", "b@example.com"]}


def test_the_connect_script_merges_without_touching_the_machines_policy(tmp_path: Path) -> None:
    """A connect that carries no snapshot leaves whatever policy the machine is enforcing alone."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    (machine.latchkey_dir / PERMISSIONS_CONFIG_FILENAME).write_text('{"rules": []}')
    inputs = _MachineScriptInputs(
        credential_change=_merge(machine, store_document({"slack": ["a@example.com"]}), "a@example.com"),
        permissions_json=None,
    )

    completed = machine.run(build_machine_script(inputs))

    assert completed.returncode == 0, completed.stderr
    assert machine.store_accounts() == {"slack": ["a@example.com"]}
    assert (machine.latchkey_dir / PERMISSIONS_CONFIG_FILENAME).read_text() == '{"rules": []}'


def test_the_disconnect_script_takes_one_account_away_and_leaves_its_siblings(tmp_path: Path) -> None:
    """Signing out of one account of a service must not sign the machine out of the others."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    machine.hold({"slack": ["gone@example.com", "kept@example.com"], "github": ["also-kept@example.com"]})
    inputs = _MachineScriptInputs(credential_change=_clear(machine, "gone@example.com"), permissions_json=None)

    completed = machine.run(build_machine_script(inputs))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == _outcome_marker(_MachineScriptOutcome.APPLIED)
    assert machine.store_accounts() == {"slack": ["kept@example.com"], "github": ["also-kept@example.com"]}


def test_the_disconnect_script_reports_a_missing_key_rather_than_failing(tmp_path: Path) -> None:
    """Clearing rewrites the store, so it needs the machine's key like every other credential change."""
    machine = _FakeMachine(tmp_path)
    machine.hold({"slack": ["gone@example.com"]})
    inputs = _MachineScriptInputs(credential_change=_clear(machine, "gone@example.com"), permissions_json=None)

    completed = machine.run(build_machine_script(inputs))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == _outcome_marker(_MachineScriptOutcome.KEY_MISSING)
    assert machine.store_accounts() == {"slack": ["gone@example.com"]}


def test_the_disconnect_script_takes_the_credential_away_under_whatever_key_the_machine_runs(
    tmp_path: Path,
) -> None:
    """A sign-out must not leave the credential behind because another computer re-keyed the machine.

    A clear brings nothing of this computer's to write, so the recorded key is
    not what it needs -- only the machine's own, which is what it rewrites the
    store with.
    """
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text("another-computers-key")
    machine.hold({"slack": ["gone@example.com", "kept@example.com"]})
    inputs = _MachineScriptInputs(credential_change=_clear(machine, "gone@example.com"), permissions_json=None)

    completed = machine.run(build_machine_script(inputs))

    assert completed.returncode == 0, completed.stderr
    assert machine.store_accounts() == {"slack": ["kept@example.com"]}


def test_the_permissions_script_needs_no_key_at_all(tmp_path: Path) -> None:
    """A policy is not encrypted, so a machine that rebooted still takes one."""
    machine = _FakeMachine(tmp_path)
    inputs = _MachineScriptInputs(credential_change=None, permissions_json=_SLACK_ANY)

    completed = machine.run(build_machine_script(inputs))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == _outcome_marker(_MachineScriptOutcome.APPLIED)
    permissions_path = machine.latchkey_dir / PERMISSIONS_CONFIG_FILENAME
    assert permissions_path.read_text() == _SLACK_ANY
    assert stat.S_IMODE(permissions_path.stat().st_mode) == 0o600
    assert machine.entries() == [PERMISSIONS_CONFIG_FILENAME]


def test_the_grant_script_reports_a_missing_key_and_touches_nothing(tmp_path: Path) -> None:
    """A rebooted machine has lost its RAM-backed key: that is an answer, not a failure."""
    machine = _FakeMachine(tmp_path)

    completed = machine.run(_grant_script(machine, store_document({"slack": ["a@example.com"]}), ""))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == _outcome_marker(_MachineScriptOutcome.KEY_MISSING)
    assert machine.entries() == []


def test_the_grant_script_refuses_a_machine_running_under_a_different_key(tmp_path: Path) -> None:
    """The key itself never travels; its hash is enough to tell a re-keyed machine apart."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text("another-computers-key")

    completed = machine.run(_grant_script(machine, store_document({"slack": ["a@example.com"]}), ""))

    assert completed.returncode != 0
    assert "different key" in completed.stderr
    assert machine.entries() == []


def test_the_grant_script_accepts_a_key_file_with_a_trailing_newline(tmp_path: Path) -> None:
    """Matches the Python probe, which strips the key it reads back."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(f"{MACHINE_KEY}\n")

    completed = machine.run(_grant_script(machine, store_document({"slack": ["a@example.com"]}), ""))

    assert completed.returncode == 0, completed.stderr
    assert machine.store_accounts() == {"slack": ["a@example.com"]}


def test_a_failed_merge_leaves_the_policy_alone_and_cleans_up(tmp_path: Path) -> None:
    """Credential first: a policy must never be enforceable before the credential it rides on landed."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    (machine.latchkey_dir / PERMISSIONS_CONFIG_FILENAME).write_text('{"rules": []}')

    completed = machine.run(_grant_script(machine, b"not a credential store", ""))

    assert completed.returncode != 0
    assert (machine.latchkey_dir / PERMISSIONS_CONFIG_FILENAME).read_text() == '{"rules": []}'
    assert machine.entries() == [PERMISSIONS_CONFIG_FILENAME]


def test_the_read_script_answers_with_the_store_re_encrypted_and_the_policy(tmp_path: Path) -> None:
    """One ``sh`` run answers with everything the pane needs: the credentials, their stamp, and the policy."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    machine.hold({"slack": ["a@example.com"]})
    machine.stamp()
    (machine.latchkey_dir / PERMISSIONS_CONFIG_FILENAME).write_text(_SLACK_ANY)

    completed = machine.run(build_machine_read_script(SecretStr("desktop-key-7781"), machine.key_file))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == _outcome_marker(_MachineScriptOutcome.APPLIED)
    answers = _read_answers(completed.stdout)
    assert store_accounts(answers[_READ_CREDENTIALS_PREFIX]) == {"slack": ["a@example.com"]}
    # Re-encrypted for the desktop on the way out, so the machine's own key never leaves it.
    assert json.loads(answers[_READ_CREDENTIALS_PREFIX])["key"] == "desktop-key-7781"
    assert answers[_READ_DATA_FORMAT_VERSION_PREFIX] == b"2"
    assert answers[_READ_PERMISSIONS_PREFIX] == _SLACK_ANY.encode("utf-8")
    # The scratch copy the re-encrypt wrote is gone.
    assert machine.entries() == [
        CREDENTIALS_STORE_FILENAME,
        UPSTREAM_DATA_FORMAT_VERSION_FILENAME,
        PERMISSIONS_CONFIG_FILENAME,
    ]


def test_the_read_script_answers_for_a_machine_that_holds_nothing_yet(tmp_path: Path) -> None:
    """Nothing connected and nothing provisioned is an answer, not an error."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)

    completed = machine.run(build_machine_read_script(SecretStr("desktop-key-7781"), machine.key_file))

    assert completed.returncode == 0, completed.stderr
    assert _read_answers(completed.stdout) == {}


def test_the_read_script_answers_for_a_machine_whose_last_account_was_disconnected(tmp_path: Path) -> None:
    """Disconnecting the last account empties the store rather than removing it; that still holds nothing."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    machine.hold({})
    machine.stamp()
    (machine.latchkey_dir / PERMISSIONS_CONFIG_FILENAME).write_text(_SLACK_ANY)

    completed = machine.run(build_machine_read_script(SecretStr("desktop-key-7781"), machine.key_file))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == _outcome_marker(_MachineScriptOutcome.APPLIED)
    assert _read_answers(completed.stdout) == {_READ_PERMISSIONS_PREFIX: _SLACK_ANY.encode("utf-8")}
    assert machine.entries() == [
        CREDENTIALS_STORE_FILENAME,
        UPSTREAM_DATA_FORMAT_VERSION_FILENAME,
        PERMISSIONS_CONFIG_FILENAME,
    ]


def test_the_read_script_still_fails_loudly_when_the_store_cannot_be_re_encrypted(tmp_path: Path) -> None:
    """Only "it holds nothing" is tolerated: anything else the CLI refuses is reported with its own reason."""
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    (machine.latchkey_dir / CREDENTIALS_STORE_FILENAME).write_text("not a credential store")

    completed = machine.run(build_machine_read_script(SecretStr("desktop-key-7781"), machine.key_file))

    assert completed.returncode != 0
    assert completed.stderr.strip() != ""
    assert _read_answers(completed.stdout) == {}


def test_the_read_script_answers_the_policy_of_a_machine_that_lost_its_key(tmp_path: Path) -> None:
    """A rebooted machine can still say what its gateway enforces; only the store needs the key."""
    machine = _FakeMachine(tmp_path)
    machine.hold({"slack": ["a@example.com"]})
    (machine.latchkey_dir / PERMISSIONS_CONFIG_FILENAME).write_text(_SLACK_ANY)

    completed = machine.run(build_machine_read_script(SecretStr("desktop-key-7781"), machine.key_file))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == _outcome_marker(_MachineScriptOutcome.KEY_MISSING)
    assert _read_answers(completed.stdout) == {_READ_PERMISSIONS_PREFIX: _SLACK_ANY.encode("utf-8")}


def _read_answers(stdout: str) -> dict[str, bytes]:
    """The prefixed, base64-encoded lines a read script answered with, decoded."""
    prefixes = (_READ_CREDENTIALS_PREFIX, _READ_DATA_FORMAT_VERSION_PREFIX, _READ_PERMISSIONS_PREFIX)
    return {
        prefix: base64.b64decode(line[len(prefix) :], validate=True)
        for line in stdout.splitlines()
        for prefix in prefixes
        if line.startswith(prefix)
    }


# -- The transfers, against the stub machine ------------------------------------


def _push_grant(
    tmp_path: Path, host_id: HostId, outer: OuterHostInterface, account: str, permissions_json: str = _SLACK_ANY
) -> None:
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={"slack": ["a@example.com"]})
    machine_latchkey = latchkey_for_machine(latchkey, plugin_data_dir(latchkey.latchkey_directory), host_id)
    push_credentials_with_permissions(
        outer, machine_latchkey, host_id, "slack", account, SecretStr(MACHINE_KEY), permissions_json
    )


def test_a_grant_costs_one_remote_command_when_the_machine_has_its_key(tmp_path: Path) -> None:
    """The whole point: no probes, no uploads, no cleanup calls -- one script, one round trip."""
    host_id = HostId.generate()
    outer = stub_machine({"github": ["kept@example.com"]})
    as_stub(outer).remote_files[_MACHINE_KEY_PATH] = MACHINE_KEY.encode("utf-8")

    _push_grant(tmp_path, host_id, outer, "a@example.com")

    assert len(as_stub(outer).recorded) == 1
    assert as_stub(outer).written == []
    assert as_stub(outer).machine_accounts == {"github": ["kept@example.com"], "slack": ["a@example.com"]}
    assert as_stub(outer).machine_permissions == _SLACK_ANY


def test_a_connect_costs_one_remote_command(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = stub_machine({"github": ["kept@example.com"]})
    as_stub(outer).remote_files[_MACHINE_KEY_PATH] = MACHINE_KEY.encode("utf-8")
    latchkey = desktop_latchkey(tmp_path, host_id=host_id, machine_accounts={"slack": ["a@example.com"]})
    machine_latchkey = latchkey_for_machine(latchkey, plugin_data_dir(latchkey.latchkey_directory), host_id)

    push_credentials(outer, machine_latchkey, host_id, "slack", "a@example.com", SecretStr(MACHINE_KEY))

    assert len(as_stub(outer).recorded) == 1
    assert as_stub(outer).written == []
    assert as_stub(outer).machine_accounts == {"github": ["kept@example.com"], "slack": ["a@example.com"]}
    # A connect carries no policy, so the machine's own is untouched.
    assert as_stub(outer).machine_permissions is None


def test_a_disconnect_costs_one_remote_command(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["gone@example.com", "kept@example.com"]})
    as_stub(outer).remote_files[_MACHINE_KEY_PATH] = MACHINE_KEY.encode("utf-8")

    clear_remote_credentials(outer, host_id, "slack", "gone@example.com", SecretStr(MACHINE_KEY))

    assert len(as_stub(outer).recorded) == 1
    assert as_stub(outer).written == []
    assert as_stub(outer).machine_accounts == {"slack": ["kept@example.com"]}


def test_a_disconnect_is_not_refused_by_a_machine_rekeyed_out_from_under_this_computer(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["gone@example.com"]})
    as_stub(outer).remote_files[_MACHINE_KEY_PATH] = b"another-computers-key"

    clear_remote_credentials(outer, host_id, "slack", "gone@example.com", SecretStr(MACHINE_KEY))

    assert as_stub(outer).machine_accounts == {}


def test_a_permissions_snapshot_costs_one_remote_command_and_no_home_probe(tmp_path: Path) -> None:
    """The script expands ``$HOME`` itself, so nothing has to resolve the machine's ~/.latchkey first."""
    host_id = HostId.generate()
    outer = stub_machine({})

    push_permissions_snapshot(outer, host_id, _SLACK_ANY)

    assert len(as_stub(outer).recorded) == 1
    assert as_stub(outer).written == []
    assert as_stub(outer).machine_permissions == _SLACK_ANY


def test_a_permissions_snapshot_this_build_cannot_read_never_reaches_the_machine(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = stub_machine({})

    with pytest.raises(RemoteGatewayError, match="unreadable permissions snapshot"):
        push_permissions_snapshot(outer, host_id, '{"rules": "not-a-list"}')

    assert as_stub(outer).recorded == []


def test_a_grant_to_a_rebooted_machine_writes_its_key_back_and_runs_again(tmp_path: Path) -> None:
    """The one case that costs more than a round trip, and it still lands."""
    host_id = HostId.generate()
    outer = stub_machine({})

    _push_grant(tmp_path, host_id, outer, "a@example.com")

    assert [entry.path for entry in as_stub(outer).written] == [_MACHINE_KEY_PATH]
    assert as_stub(outer).remote_files[_MACHINE_KEY_PATH] == MACHINE_KEY.encode("utf-8")
    assert len(as_stub(outer).recorded) == 2
    assert as_stub(outer).machine_accounts == {"slack": ["a@example.com"]}
    assert as_stub(outer).machine_permissions == _SLACK_ANY


def test_a_disconnect_to_a_rebooted_machine_writes_its_key_back_and_runs_again(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = stub_machine({"slack": ["gone@example.com"]})

    clear_remote_credentials(outer, host_id, "slack", "gone@example.com", SecretStr(MACHINE_KEY))

    assert [entry.path for entry in as_stub(outer).written] == [_MACHINE_KEY_PATH]
    assert len(as_stub(outer).recorded) == 2
    assert as_stub(outer).machine_accounts == {}


def test_a_grant_refuses_a_machine_rekeyed_out_from_under_this_computer(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = stub_machine({})
    as_stub(outer).remote_files[_MACHINE_KEY_PATH] = b"another-computers-key"

    with pytest.raises(RemoteGatewayError, match="different key"):
        _push_grant(tmp_path, host_id, outer, "a@example.com")

    assert as_stub(outer).machine_accounts == {}
    assert as_stub(outer).machine_permissions is None


def test_a_grant_with_an_unreadable_policy_never_reaches_the_machine(tmp_path: Path) -> None:
    host_id = HostId.generate()
    outer = stub_machine({})

    with pytest.raises(RemoteGatewayError, match="unreadable permissions snapshot"):
        _push_grant(tmp_path, host_id, outer, "a@example.com", permissions_json='{"rules": "nope"}')

    assert as_stub(outer).recorded == []


def test_a_push_too_large_for_one_command_is_refused_rather_than_split(tmp_path: Path) -> None:
    """No second, slower code path: one shape of push, exercised by every push."""
    host_id = HostId.generate()
    outer = stub_machine({})
    as_stub(outer).remote_files[_MACHINE_KEY_PATH] = MACHINE_KEY.encode("utf-8")
    permission_count = _MAX_MACHINE_SCRIPT_BYTES // 8
    huge_policy = json.dumps({"rules": [{"slack-api": [f"perm-{idx}" for idx in range(permission_count)]}]})

    with pytest.raises(RemoteGatewayError, match="past the"):
        _push_grant(tmp_path, host_id, outer, "a@example.com", permissions_json=huge_policy)

    assert as_stub(outer).recorded == []
    assert as_stub(outer).machine_permissions is None


class _KeyForgettingMachine(StubOuter):
    """A stub machine whose tmpfs swallows every key written to it."""

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        if str(path) != _MACHINE_KEY_PATH:
            super().write_file(path, content, mode, is_atomic)


def test_a_grant_fails_loudly_when_the_machine_keeps_reporting_its_key_missing(tmp_path: Path) -> None:
    """Writing the key back and still finding none is not something to loop on."""
    host_id = HostId.generate()
    outer = cast(OuterHostInterface, _KeyForgettingMachine(result=CommandResult(stdout="", stderr="", success=True)))

    with pytest.raises(RemoteGatewayError, match="still reports its encryption key missing"):
        _push_grant(tmp_path, host_id, outer, "a@example.com")

    assert len(as_stub(outer).recorded) == 2


class _MuteMachine(StubOuter):
    """A stub machine whose shell exits 0 without printing anything."""

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(RecordedCommand(command=command, timeout_seconds=timeout_seconds))
        return CommandResult(stdout="", stderr="", success=True)


def test_a_grant_that_finishes_without_an_outcome_is_an_error(tmp_path: Path) -> None:
    """A script that exits 0 without reaching either of its ends did not do what it says."""
    host_id = HostId.generate()
    outer = cast(OuterHostInterface, _MuteMachine(result=CommandResult(stdout="", stderr="", success=True)))

    with pytest.raises(RemoteGatewayError, match="without reporting an outcome"):
        _push_grant(tmp_path, host_id, outer, "a@example.com")


def test_a_machine_script_embeds_no_secret_key(tmp_path: Path) -> None:
    """Only the hash rides along, so a process listing on the machine never shows the key."""
    machine = _FakeMachine(tmp_path)

    script = _grant_script(machine, store_document({"slack": ["a@example.com"]}), "a@example.com")

    assert MACHINE_KEY not in script
    assert hashlib.sha256(MACHINE_KEY.encode("utf-8")).hexdigest() in script


_CUSTOM_CONFIG = merge_minds_latchkey_config(
    None,
    {
        "custom_api_example_com": build_custom_service_registration(
            "api.example.com",
            "https",
            login_url="https://api.example.com/login",
            login_flow=LoginFlow.COOKIE_CAPTURE,
            login_flow_params={"cookieKeys": ["session", "csrf"], "cookieUrl": "https://api.example.com/"},
        )
    },
)


def test_the_grant_script_installs_the_config_ahead_of_the_credential(tmp_path: Path) -> None:
    """The config travels like the policy -- a whole snapshot, installed atomically -- and lands first.

    A gateway with no entry for a service cannot route a request to it, so a
    credential the machine held for a service its config does not name would
    be one it could never use.
    """
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    machine.hold({})
    script = build_machine_script(
        _MachineScriptInputs(
            config_json=_CUSTOM_CONFIG,
            credential_change=_merge(
                machine,
                store_document({"custom_api_example_com": ["me@example.com"]}),
                "me@example.com",
                service_name="custom_api_example_com",
            ),
            permissions_json=None,
        )
    )

    completed = machine.run(script)

    assert completed.returncode == 0, completed.stderr
    assert machine.config() == json.loads(_CUSTOM_CONFIG)
    assert (machine.latchkey_dir / CONFIG_FILENAME).stat().st_mode & 0o777 == 0o600
    assert machine.store_accounts() == {"custom_api_example_com": ["me@example.com"]}
    lines = script.splitlines()
    assert next(i for i, line in enumerate(lines) if f"/{CONFIG_FILENAME}" in line and "mv -f" in line) < next(
        i for i, line in enumerate(lines) if "auth re-encrypt" in line
    )


def test_a_script_without_a_config_leaves_the_machines_alone(tmp_path: Path) -> None:
    machine = _FakeMachine(tmp_path)
    machine.key_file.write_text(MACHINE_KEY)
    machine.hold({})
    script = build_machine_script(
        _MachineScriptInputs(
            credential_change=_merge(machine, store_document({"slack": ["a@example.com"]}), "a@example.com"),
            permissions_json=None,
        )
    )

    assert CONFIG_FILENAME not in script
    assert machine.run(script).returncode == 0
    assert machine.config() is None
