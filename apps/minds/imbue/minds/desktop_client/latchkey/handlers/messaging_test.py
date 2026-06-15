from pathlib import Path

from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import stdout_reports_message_delivered


def _make_fake_mngr(tmp_path: Path, exit_code: int, stdout: str = "") -> Path:
    fake = tmp_path / "mngr"
    # printf the stdout (may contain JSONL lines) then exit with the given code.
    fake.write_text(f"#!/bin/bash\nprintf '%s' {_bash_squote(stdout)}\nexit {exit_code}\n")
    fake.chmod(0o755)
    return fake


def _bash_squote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


# These two tests only verify exit-code plumbing; the ``-m``/``--`` argv
# construction is covered in predefined_test.py
# (``test_mngr_message_sender_invokes_message_subcommand``).
def test_try_send_returns_true_on_success(tmp_path: Path) -> None:
    sender = MngrMessageSender(mngr_binary=str(_make_fake_mngr(tmp_path, exit_code=0)))
    assert sender.try_send("some-agent", "hello") is True


def test_try_send_returns_false_on_failure(tmp_path: Path) -> None:
    sender = MngrMessageSender(mngr_binary=str(_make_fake_mngr(tmp_path, exit_code=1)))
    assert sender.try_send("missing-agent", "hello") is False


def test_stdout_reports_delivered_true_for_message_sent_event() -> None:
    stdout = '{"event": "message_sent", "agent": "assistant", "message": "Message sent successfully"}\n'
    assert stdout_reports_message_delivered(stdout) is True


def test_stdout_reports_delivered_false_when_no_agent_matched() -> None:
    # "No agents found" produces no message_sent event even though mngr exits 0.
    assert stdout_reports_message_delivered("") is False


def test_stdout_reports_delivered_ignores_non_json_and_error_events() -> None:
    stdout = 'WARNING: some noise line\n{"event": "message_error", "agent": "assistant", "error": "boom"}\n'
    assert stdout_reports_message_delivered(stdout) is False


def test_deliver_true_only_when_message_sent_event_present(tmp_path: Path) -> None:
    delivered_stdout = '{"event": "message_sent", "agent": "assistant", "message": "ok"}\n'
    # mngr exits 0 with a message_sent event -> delivered.
    sender = MngrMessageSender(mngr_binary=str(_make_fake_mngr(tmp_path, exit_code=0, stdout=delivered_stdout)))
    assert sender.deliver("assistant", "hello") is True


def test_deliver_false_when_exit_zero_but_no_delivery(tmp_path: Path) -> None:
    # The key regression: exit 0 with no message_sent event (agent not found
    # yet) must NOT be treated as delivered.
    sender = MngrMessageSender(mngr_binary=str(_make_fake_mngr(tmp_path, exit_code=0, stdout="")))
    assert sender.deliver("assistant", "hello") is False
