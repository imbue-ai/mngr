from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import stdout_reports_message_delivered
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller


class _RecordingCaller(MngrCaller):
    """In-process ``MngrCaller`` stub: records argv and returns a canned result.

    Used so the caller-backed code path can be tested without forking a real
    forkserver child (which would import the full ``mngr`` CLI).
    """

    result: MngrCallResult = Field(description="Canned result returned by every call.")
    _calls: list[list[str]] = PrivateAttr(default_factory=list)

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
    ) -> MngrCallResult:
        self._calls.append(list(argv))
        return self.result


def _make_fake_mngr(tmp_path: Path, exit_code: int, stdout: str = "") -> Path:
    fake = tmp_path / "mngr"
    # printf the stdout (may contain JSONL lines) then exit with the given code.
    fake.write_text(f"#!/bin/bash\nprintf '%s' {_bash_squote(stdout)}\nexit {exit_code}\n")
    fake.chmod(0o755)
    return fake


def _bash_squote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


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


def test_caller_path_try_send_builds_message_argv() -> None:
    caller = _RecordingCaller(result=MngrCallResult(returncode=0))
    sender = MngrMessageSender(caller=caller)

    assert sender.try_send("some-agent", "hello") is True
    # The text must go through ``-m`` and the target after ``--`` so it is not
    # parsed as a second agent identifier.
    assert caller._calls == [["message", "-m", "hello", "--", "some-agent"]]


def test_caller_path_try_send_returns_false_on_nonzero_exit() -> None:
    caller = _RecordingCaller(result=MngrCallResult(returncode=1, stderr="agent missing"))
    sender = MngrMessageSender(caller=caller)

    assert sender.try_send("missing-agent", "hello") is False


def test_caller_path_deliver_uses_jsonl_output() -> None:
    delivered_stdout = '{"event": "message_sent", "agent": "assistant", "message": "ok"}\n'
    caller = _RecordingCaller(result=MngrCallResult(returncode=0, stdout=delivered_stdout))
    sender = MngrMessageSender(caller=caller)

    assert sender.deliver("assistant", "hello") is True
    assert caller._calls == [["message", "--format", "jsonl", "-m", "hello", "--", "assistant"]]


def test_caller_path_deliver_false_when_no_message_sent_event() -> None:
    caller = _RecordingCaller(result=MngrCallResult(returncode=0, stdout=""))
    sender = MngrMessageSender(caller=caller)

    assert sender.deliver("assistant", "hello") is False
