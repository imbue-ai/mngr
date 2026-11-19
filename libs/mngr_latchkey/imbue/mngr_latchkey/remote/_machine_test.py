import base64
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from imbue.mngr.errors import HostConnectionError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_latchkey.baseline_permissions import AGENT_BASELINE_PERMISSIONS
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.devices import DeviceRecord
from imbue.mngr_latchkey.discovery import is_transient_remote_wiring_error
from imbue.mngr_latchkey.primitives import PermissionsFormatVersion
from imbue.mngr_latchkey.remote._machine import OUTCOME_DONE_MARKER
from imbue.mngr_latchkey.remote._machine import RemoteCredentialClear
from imbue.mngr_latchkey.remote._machine import RemoteCredentialMerge
from imbue.mngr_latchkey.remote._machine import RemoteStateRequest
from imbue.mngr_latchkey.remote._machine import RemoteStateUpdate
from imbue.mngr_latchkey.remote._machine import RemoteTunnelTarget
from imbue.mngr_latchkey.remote._machine import _announcement_document
from imbue.mngr_latchkey.remote._machine import _remote_command
from imbue.mngr_latchkey.remote._machine import _request_document
from imbue.mngr_latchkey.remote._machine import _update_document
from imbue.mngr_latchkey.remote._machine import announce_device
from imbue.mngr_latchkey.remote._machine import apply_remote_state
from imbue.mngr_latchkey.remote._machine import forget_permission_request
from imbue.mngr_latchkey.remote._machine import read_filed_permission_requests
from imbue.mngr_latchkey.remote._machine import read_remote_state
from imbue.mngr_latchkey.remote._machine_policy import project_policy_for_machine
from imbue.mngr_latchkey.remote.errors import MachineTransferError
from imbue.mngr_latchkey.remote.errors import ReceivedFromMachine
from imbue.mngr_latchkey.remote.errors import SentToMachine
from imbue.mngr_latchkey.remote.mock_outer_host_test import AnsweringVps
from imbue.mngr_latchkey.remote.mock_outer_host_test import UnreachableVps
from imbue.mngr_latchkey.store import dump_permissions_text

_HOST_ID = HostId.generate()

# What every read-state answers with, whatever its request asks for beyond it.
_EVERY_READ_ANSWERS = frozenset(
    {
        ReceivedFromMachine.ENCRYPTION_KEY,
        ReceivedFromMachine.LISTEN_PASSWORD,
        ReceivedFromMachine.CONFIG,
        ReceivedFromMachine.PERMISSIONS,
        ReceivedFromMachine.PERMISSIONS_FORMAT_VERSION,
        ReceivedFromMachine.DESKTOP_EGRESS_RULES,
    }
)


def test_an_update_document_carries_only_what_the_update_sets() -> None:
    document = _update_document(
        RemoteStateUpdate(
            fallback_encryption_key=SecretStr("key-2213"),
            permissions_json='{"rules": []}',
            is_gateway_restarted=True,
        )
    )

    assert document == {
        "fallback_encryption_key": b"key-2213",
        "permissions_json": b'{"rules":[]}',
        "restart_gateway": b"1",
    }


def test_an_update_document_carries_the_policy_as_the_machine_is_to_hold_it() -> None:
    """The projection is applied as the document is built, so no caller of a push has to remember it."""
    policy = dump_permissions_text(AGENT_BASELINE_PERMISSIONS)

    document = _update_document(RemoteStateUpdate(permissions_json=policy))

    assert document == {"permissions_json": project_policy_for_machine(policy).encode("utf-8")}
    assert document["permissions_json"] != policy.encode("utf-8")


def test_an_update_document_spells_the_whole_service_as_no_account_entry() -> None:
    """The empty account means "the whole service", which the script reads as the entry being absent."""
    merge = RemoteCredentialMerge(service_name="slack", account="", bundle=b"store", data_format_version="2")

    document = _update_document(RemoteStateUpdate(credential_merge=merge))

    assert document == {
        "credential_bundle": b"store",
        "credential_data_format_version": b"2",
        "credential_service": b"slack",
    }


def test_an_update_document_names_the_tunnel_target() -> None:
    host_id = HostId.generate()

    document = _update_document(
        RemoteStateUpdate(tunnel=RemoteTunnelTarget(host_id=host_id, ssh_user="root", ssh_port=2222))
    )

    assert document == {
        "tunnel_host_id": str(host_id).encode("utf-8"),
        "tunnel_ssh_user": b"root",
        "tunnel_ssh_port": b"2222",
    }


def test_an_announcement_document_names_the_record_by_the_device_id() -> None:
    record = DeviceRecord(
        device_id=DesktopDeviceId("desktop-9021"),
        hostname="laptop",
        port=41988,
        gateway_password="pw-9021",
        permissions_override="jwt-9021",
    )

    assert _announcement_document(record) == {
        "device_id": b"desktop-9021",
        "device_record_json": record.model_dump_json().encode("utf-8"),
    }


def test_a_request_document_carries_only_what_the_request_sets() -> None:
    assert _request_document(RemoteStateRequest()) == {}
    assert _request_document(RemoteStateRequest(is_credential_store_included=True)) == {
        "include_credential_store": b"1"
    }


def test_the_command_hands_the_document_to_the_script_as_a_quoted_heredoc() -> None:
    """One command string: the script on PATH, then the entries as base64 lines the shell never expands."""
    command = _remote_command("apply-state", {"permissions_json": b'{"rules": []}', "restart_gateway": b"1"})

    assert command.splitlines() == [
        "mngr-latchkey apply-state <<'MNGR_LATCHKEY_DOCUMENT_END'",
        "permissions_json " + base64.b64encode(b'{"rules": []}').decode("ascii"),
        "restart_gateway MQ==",
        "MNGR_LATCHKEY_DOCUMENT_END",
    ]


def _answering(tmp_path: Path, stdout: str, success: bool = True) -> OuterHostInterface:
    return cast(
        OuterHostInterface,
        AnsweringVps(root=tmp_path / "vps", canned=CommandResult(stdout=stdout, stderr="", success=success)),
    )


def _answer(name: str, value: bytes) -> str:
    return f"MNGR_LATCHKEY_{name}={base64.b64encode(value).decode('ascii')}"


def _read_stdout(*extra_lines: str, has_credential_store: bool = False) -> str:
    """What a read prints: the answers every read gives, ``extra_lines`` among them, and the outcome marker."""
    return "\n".join(
        (
            _answer("PACKAGE_VERSION", b"1"),
            _answer("HOME", b"/root"),
            _answer("HAS_CREDENTIAL_STORE", b"1" if has_credential_store else b"0"),
            _answer("HAS_CONTAINER_TUNNEL_KEY", b"0"),
            *extra_lines,
            OUTCOME_DONE_MARKER,
        )
    )


def test_a_read_that_answers_without_what_every_read_gives_is_an_error(tmp_path: Path) -> None:
    outer = _answering(tmp_path, "\n".join((_answer("PACKAGE_VERSION", b"1"), OUTCOME_DONE_MARKER)))

    with pytest.raises(MachineTransferError, match="answered without"):
        read_remote_state(outer, RemoteStateRequest(), _HOST_ID)


def test_a_read_whose_answer_is_not_base64_is_an_error(tmp_path: Path) -> None:
    outer = _answering(tmp_path, "\n".join(("MNGR_LATCHKEY_HOME=not base64!", OUTCOME_DONE_MARKER)))

    with pytest.raises(MachineTransferError, match="HOME answer is not the base64"):
        read_remote_state(outer, RemoteStateRequest(), _HOST_ID)


def test_a_read_whose_answer_is_not_utf8_text_is_an_error(tmp_path: Path) -> None:
    """A file on the machine that is not text (a corrupt permissions.json) is reported, not raised through."""
    outer = _answering(tmp_path, _read_stdout(_answer("PERMISSIONS_JSON", b"\xff\xfe not text")))

    with pytest.raises(MachineTransferError, match="not the UTF-8 text"):
        read_remote_state(outer, RemoteStateRequest(), _HOST_ID)


def test_a_read_that_returns_a_store_without_its_stamp_is_an_error(tmp_path: Path) -> None:
    outer = _answering(tmp_path, _read_stdout(_answer("CREDENTIALS", b"store"), has_credential_store=True))

    with pytest.raises(MachineTransferError, match="without the format stamp"):
        read_remote_state(outer, RemoteStateRequest(), _HOST_ID)


@pytest.mark.parametrize(
    ("extra_hosts_answer", "expected_error"),
    [(b"not json", "not the JSON docker prints"), (b"[1, 2]", "not the list of strings docker prints")],
)
def test_a_read_whose_container_extra_hosts_are_not_what_docker_prints_is_an_error(
    tmp_path: Path, extra_hosts_answer: bytes, expected_error: str
) -> None:
    outer = _answering(tmp_path, _read_stdout(_answer("CONTAINER_EXTRA_HOSTS", extra_hosts_answer)))

    with pytest.raises(MachineTransferError, match=expected_error):
        read_remote_state(outer, RemoteStateRequest(container_host_id=HostId.generate()), _HOST_ID)


def test_a_read_that_did_not_run_to_its_end_is_an_error(tmp_path: Path) -> None:
    outer = _answering(tmp_path, "\n".join(("npm WARN something", _answer("PACKAGE_VERSION", b"1"))))

    with pytest.raises(MachineTransferError, match="without reporting an outcome") as raised:
        read_remote_state(outer, RemoteStateRequest(), _HOST_ID)

    assert "'npm WARN something'" in str(raised.value)


def test_a_failed_reads_error_message_carries_none_of_the_machines_answers(tmp_path: Path) -> None:
    """The answers a read printed before it failed are the machine's secrets, so the error must not quote them."""
    answers = "\n".join((_answer("PACKAGE_VERSION", b"1"), _answer("ENCRYPTION_KEY", b"machine-key-5518")))
    failed = _answering(tmp_path, answers, success=False)
    died = _answering(tmp_path, answers)

    with pytest.raises(MachineTransferError, match="reported no reason") as failure:
        read_remote_state(failed, RemoteStateRequest(), _HOST_ID)
    with pytest.raises(MachineTransferError, match="last output line: ''") as death:
        read_remote_state(died, RemoteStateRequest(), _HOST_ID)

    encoded_key = base64.b64encode(b"machine-key-5518").decode("ascii")
    assert encoded_key not in str(failure.value)
    assert encoded_key not in str(death.value)


def test_what_a_successful_command_said_on_stderr_reaches_the_debug_log(tmp_path: Path) -> None:
    """A warning a latchkey invocation printed while the script still ran to its end lands only on stderr."""
    canned_stdout = _read_stdout(has_credential_store=True)
    talkative = cast(
        OuterHostInterface,
        AnsweringVps(
            root=tmp_path / "vps",
            canned=CommandResult(stdout=canned_stdout, stderr="Error: node: not found\n", success=True),
        ),
    )
    quiet = _answering(tmp_path, canned_stdout)

    with capture_loguru("DEBUG") as talkative_log:
        read_remote_state(talkative, RemoteStateRequest(), _HOST_ID)
    with capture_loguru("DEBUG") as quiet_log:
        read_remote_state(quiet, RemoteStateRequest(), _HOST_ID)

    assert "Error: node: not found" in talkative_log.getvalue()
    assert "said on stderr" not in quiet_log.getvalue()


def test_a_read_ignores_what_the_machine_prints_between_its_answers(tmp_path: Path) -> None:
    """Anything else the script runs writes to the same stdout."""
    outer = _answering(
        tmp_path,
        "\n".join(
            (
                "npm WARN something",
                _answer("PACKAGE_VERSION", b"0.1.6+abc"),
                _answer("HOME", b"/root"),
                "{}",
                _answer("HAS_CREDENTIAL_STORE", b"0"),
                _answer("HAS_CONTAINER_TUNNEL_KEY", b"0"),
                _answer("ENCRYPTION_KEY", b""),
                OUTCOME_DONE_MARKER,
            )
        ),
    )

    state = read_remote_state(outer, RemoteStateRequest(), _HOST_ID)

    assert state.package_version == "0.1.6+abc"
    assert state.home == Path("/root")
    # An empty secret file reads as no secret, the way the mirror reads one.
    assert state.encryption_key is None


def test_an_update_document_carries_the_data_format_version_as_decimal_text() -> None:
    document = _update_document(
        RemoteStateUpdate(permissions_json='{"rules": []}', permissions_format_version=PermissionsFormatVersion(2))
    )

    assert document == {"permissions_json": b'{"rules":[]}', "permissions_format_version": b"2"}


def test_a_read_of_a_machine_never_stamped_reads_as_format_version_zero(tmp_path: Path) -> None:
    outer = _answering(tmp_path, _read_stdout())

    state = read_remote_state(outer, RemoteStateRequest(), _HOST_ID)

    assert state.permissions_format_version == 0


def test_a_read_answers_the_format_version_the_machine_is_stamped_with(tmp_path: Path) -> None:
    outer = _answering(tmp_path, _read_stdout(_answer("PERMISSIONS_FORMAT_VERSION", b"3\n")))

    state = read_remote_state(outer, RemoteStateRequest(), _HOST_ID)

    assert state.permissions_format_version == 3


@pytest.mark.parametrize("stamp", [b"two", b"-1", b""])
def test_a_read_whose_format_version_is_not_a_non_negative_integer_is_an_error(tmp_path: Path, stamp: bytes) -> None:
    outer = _answering(tmp_path, _read_stdout(_answer("PERMISSIONS_FORMAT_VERSION", stamp)))

    with pytest.raises(MachineTransferError, match="permissions format version is not the non-negative integer"):
        read_remote_state(outer, RemoteStateRequest(), _HOST_ID)


def _failing(tmp_path: Path) -> OuterHostInterface:
    """A machine that fails every command it runs, without saying why."""
    return _answering(tmp_path, "", success=False)


def test_a_failed_apply_names_what_it_was_sending(tmp_path: Path) -> None:
    """The parts come from the update itself, so no caller describes what it was pushing."""
    update = RemoteStateUpdate(
        permissions_json='{"rules": []}', permissions_format_version=PermissionsFormatVersion(2)
    )

    with pytest.raises(MachineTransferError) as raised:
        apply_remote_state(_failing(tmp_path), update, _HOST_ID)

    assert raised.value.sent == {SentToMachine.PERMISSIONS, SentToMachine.PERMISSIONS_FORMAT_VERSION}
    assert raised.value.expected == frozenset()
    assert raised.value.host_id == _HOST_ID


def test_a_failed_read_names_what_it_expected_back_and_what_it_sent_along(tmp_path: Path) -> None:
    """A read carries a write too when it hands a rebooted machine its key back, so it names both directions."""
    request = RemoteStateRequest(
        is_credential_store_included=True,
        fallback_encryption_key=SecretStr("key-3318"),
        container_host_id=HostId.generate(),
    )

    with pytest.raises(MachineTransferError) as raised:
        read_remote_state(_failing(tmp_path), request, _HOST_ID)

    assert raised.value.sent == {SentToMachine.FALLBACK_ENCRYPTION_KEY}
    assert raised.value.expected == _EVERY_READ_ANSWERS | {
        ReceivedFromMachine.CREDENTIAL_STORE,
        ReceivedFromMachine.CONTAINER_EXTRA_HOSTS,
    }


def test_a_read_whose_answers_cannot_be_read_names_what_it_expected_back(tmp_path: Path) -> None:
    """A read that came back unreadable failed to bring back what it was for, the same as one that never ran."""
    outer = _answering(tmp_path, "\n".join(("MNGR_LATCHKEY_HOME=not base64!", OUTCOME_DONE_MARKER)))

    with pytest.raises(MachineTransferError) as raised:
        read_remote_state(outer, RemoteStateRequest(), _HOST_ID)

    assert raised.value.sent == frozenset()
    assert raised.value.expected == _EVERY_READ_ANSWERS


def test_every_entry_an_update_can_send_belongs_to_a_named_part(tmp_path: Path) -> None:
    """An update setting every field sends every part an apply can carry, and nothing a side command sends."""
    update = RemoteStateUpdate(
        encryption_key=SecretStr("key-4410"),
        fallback_encryption_key=SecretStr("key-4411"),
        listen_password="password-4412",
        config_json="{}",
        credential_merge=RemoteCredentialMerge(
            service_name="slack", account="a@example.com", bundle=b"store", data_format_version="2"
        ),
        credential_clear=RemoteCredentialClear(service_name="github", account="b@example.com"),
        desktop_egress_rules_json="{}",
        permissions_json='{"rules": []}',
        permissions_format_version=PermissionsFormatVersion(2),
        gateway_listen_host="172.17.0.1",
        tunnel=RemoteTunnelTarget(host_id=HostId.generate(), ssh_user="root", ssh_port=2222),
        is_credential_store_abandoned=True,
        is_gateway_restarted=True,
    )

    with pytest.raises(MachineTransferError) as raised:
        apply_remote_state(_failing(tmp_path), update, _HOST_ID)

    assert raised.value.sent == set(SentToMachine) - {
        SentToMachine.DEVICE_RECORD,
        SentToMachine.PERMISSION_REQUEST_WITHDRAWAL,
    }


def test_the_side_commands_name_what_they_carry(tmp_path: Path) -> None:
    """Announcing this computer and withdrawing a request send; listing the requests reads."""
    record = DeviceRecord(
        device_id=DesktopDeviceId("desktop-6620"),
        hostname="laptop",
        port=41988,
        gateway_password="pw-6620",
        permissions_override="jwt-6620",
    )

    with pytest.raises(MachineTransferError) as announced:
        announce_device(_failing(tmp_path), record, _HOST_ID)
    with pytest.raises(MachineTransferError) as forgotten:
        forget_permission_request(_failing(tmp_path), "request-6621", _HOST_ID)
    with pytest.raises(MachineTransferError) as listed:
        read_filed_permission_requests(_failing(tmp_path), _HOST_ID)

    assert (announced.value.sent, announced.value.expected) == ({SentToMachine.DEVICE_RECORD}, frozenset())
    assert (forgotten.value.sent, forgotten.value.expected) == (
        {SentToMachine.PERMISSION_REQUEST_WITHDRAWAL},
        frozenset(),
    )
    assert (listed.value.sent, listed.value.expected) == (
        frozenset(),
        {ReceivedFromMachine.FILED_PERMISSION_REQUESTS},
    )


def test_a_connection_that_fails_is_reported_as_the_command_it_interrupted(tmp_path: Path) -> None:
    """The first command a machine runs opens its SSH connection, so a connect that fails fails that command.

    The connection's own error stays the cause, so a check that walks the
    chain -- whether discovery should retry, say -- still sees it.
    """
    reset = HostConnectionError("the connection was reset")
    outer = cast(OuterHostInterface, UnreachableVps(root=tmp_path / "vps", error=reset))

    with pytest.raises(MachineTransferError) as raised:
        apply_remote_state(outer, RemoteStateUpdate(permissions_json='{"rules": []}'), _HOST_ID)

    assert raised.value.__cause__ is reset
    assert raised.value.sent == {SentToMachine.PERMISSIONS}
    assert is_transient_remote_wiring_error(raised.value)


def test_a_failed_commands_message_names_its_parts_the_vps_and_the_host(tmp_path: Path) -> None:
    host_id = HostId.generate()

    with pytest.raises(MachineTransferError) as raised:
        apply_remote_state(
            _failing(tmp_path),
            RemoteStateUpdate(permissions_json='{"rules": []}', desktop_egress_rules_json="{}"),
            host_id,
        )

    assert str(raised.value) == (
        f"Failed to send DESKTOP_EGRESS_RULES, PERMISSIONS to VPS vps-test for host {host_id}: "
        "the command reported no reason"
    )
