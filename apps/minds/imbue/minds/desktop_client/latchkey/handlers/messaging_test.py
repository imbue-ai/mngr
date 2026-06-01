from pathlib import Path

from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender


def _make_fake_mngr(tmp_path: Path, exit_code: int) -> Path:
    fake = tmp_path / "mngr"
    fake.write_text(f"#!/bin/bash\nexit {exit_code}\n")
    fake.chmod(0o755)
    return fake


def test_try_send_returns_true_on_success(tmp_path: Path) -> None:
    sender = MngrMessageSender(mngr_binary=str(_make_fake_mngr(tmp_path, exit_code=0)))
    assert sender.try_send("some-agent", "hello") is True


def test_try_send_returns_false_on_failure(tmp_path: Path) -> None:
    sender = MngrMessageSender(mngr_binary=str(_make_fake_mngr(tmp_path, exit_code=1)))
    assert sender.try_send("missing-agent", "hello") is False
