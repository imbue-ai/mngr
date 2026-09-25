import base64
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_latchkey.remote._machine import OUTCOME_DONE_MARKER
from imbue.mngr_latchkey.remote._machine import RemoteCredentialMerge
from imbue.mngr_latchkey.remote._machine import RemoteStateRequest
from imbue.mngr_latchkey.remote._machine import RemoteStateUpdate
from imbue.mngr_latchkey.remote._machine import RemoteTunnelTarget
from imbue.mngr_latchkey.remote._machine import _remote_command
from imbue.mngr_latchkey.remote._machine import _request_document
from imbue.mngr_latchkey.remote._machine import _update_document
from imbue.mngr_latchkey.remote._machine import read_remote_state
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.remote.mock_outer_host_test import AnsweringVps


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
        "permissions_json": b'{"rules": []}',
        "restart_gateway": b"1",
    }


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

    with pytest.raises(RemoteGatewayError, match="answered without"):
        read_remote_state(outer, RemoteStateRequest(), "read")


def test_a_read_whose_answer_is_not_base64_is_an_error(tmp_path: Path) -> None:
    outer = _answering(tmp_path, "\n".join(("MNGR_LATCHKEY_HOME=not base64!", OUTCOME_DONE_MARKER)))

    with pytest.raises(RemoteGatewayError, match="HOME answer is not the base64"):
        read_remote_state(outer, RemoteStateRequest(), "read")


def test_a_read_whose_answer_is_not_utf8_text_is_an_error(tmp_path: Path) -> None:
    """A file on the machine that is not text (a corrupt permissions.json) is reported, not raised through."""
    outer = _answering(tmp_path, _read_stdout(_answer("PERMISSIONS_JSON", b"\xff\xfe not text")))

    with pytest.raises(RemoteGatewayError, match="not the UTF-8 text"):
        read_remote_state(outer, RemoteStateRequest(), "read")


def test_a_read_that_returns_a_store_without_its_stamp_is_an_error(tmp_path: Path) -> None:
    outer = _answering(tmp_path, _read_stdout(_answer("CREDENTIALS", b"store"), has_credential_store=True))

    with pytest.raises(RemoteGatewayError, match="without the format stamp"):
        read_remote_state(outer, RemoteStateRequest(), "read")


@pytest.mark.parametrize(
    ("extra_hosts_answer", "expected_error"),
    [(b"not json", "not the JSON docker prints"), (b"[1, 2]", "not the list of strings docker prints")],
)
def test_a_read_whose_container_extra_hosts_are_not_what_docker_prints_is_an_error(
    tmp_path: Path, extra_hosts_answer: bytes, expected_error: str
) -> None:
    outer = _answering(tmp_path, _read_stdout(_answer("CONTAINER_EXTRA_HOSTS", extra_hosts_answer)))

    with pytest.raises(RemoteGatewayError, match=expected_error):
        read_remote_state(outer, RemoteStateRequest(container_host_id=HostId.generate()), "read")


def test_a_read_that_did_not_run_to_its_end_is_an_error(tmp_path: Path) -> None:
    outer = _answering(tmp_path, "\n".join(("npm WARN something", _answer("PACKAGE_VERSION", b"1"))))

    with pytest.raises(RemoteGatewayError, match="without reporting an outcome") as raised:
        read_remote_state(outer, RemoteStateRequest(), "read")

    assert "'npm WARN something'" in str(raised.value)


def test_a_failed_reads_error_message_carries_none_of_the_machines_answers(tmp_path: Path) -> None:
    """The answers a read printed before it failed are the machine's secrets, so the error must not quote them."""
    answers = "\n".join((_answer("PACKAGE_VERSION", b"1"), _answer("ENCRYPTION_KEY", b"machine-key-5518")))
    failed = _answering(tmp_path, answers, success=False)
    died = _answering(tmp_path, answers)

    with pytest.raises(RemoteGatewayError, match="reported no reason") as failure:
        read_remote_state(failed, RemoteStateRequest(), "read")
    with pytest.raises(RemoteGatewayError, match="last output line: ''") as death:
        read_remote_state(died, RemoteStateRequest(), "read")

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
        read_remote_state(talkative, RemoteStateRequest(), "read")
    with capture_loguru("DEBUG") as quiet_log:
        read_remote_state(quiet, RemoteStateRequest(), "read")

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

    state = read_remote_state(outer, RemoteStateRequest(), "read")

    assert state.package_version == "0.1.6+abc"
    assert state.home == Path("/root")
    # An empty secret file reads as no secret, the way the mirror reads one.
    assert state.encryption_key is None
