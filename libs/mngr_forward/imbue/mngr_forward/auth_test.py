from pathlib import Path

import pytest

from imbue.mngr_forward.auth import FileAuthStore
from imbue.mngr_forward.errors import ForwardAuthError
from imbue.mngr_forward.primitives import OneTimeCode


def test_otp_round_trip(tmp_path: Path) -> None:
    store = FileAuthStore(data_directory=tmp_path)
    code = OneTimeCode("abc123")
    store.add_one_time_code(code=code)
    assert store.validate_and_consume_code(code=code) is True


def test_otp_unknown_rejected(tmp_path: Path) -> None:
    store = FileAuthStore(data_directory=tmp_path)
    assert store.validate_and_consume_code(code=OneTimeCode("never-issued")) is False


def test_otp_single_use(tmp_path: Path) -> None:
    store = FileAuthStore(data_directory=tmp_path)
    code = OneTimeCode("once")
    store.add_one_time_code(code=code)
    assert store.validate_and_consume_code(code=code) is True
    assert store.validate_and_consume_code(code=code) is False


def test_otp_not_persisted_to_disk(tmp_path: Path) -> None:
    store_a = FileAuthStore(data_directory=tmp_path)
    store_a.add_one_time_code(code=OneTimeCode("ephemeral"))
    # Spec: codes only live in memory; a fresh process must not see them.
    store_b = FileAuthStore(data_directory=tmp_path)
    assert store_b.validate_and_consume_code(code=OneTimeCode("ephemeral")) is False


def test_signing_key_generated_then_reused(tmp_path: Path) -> None:
    store_a = FileAuthStore(data_directory=tmp_path)
    key_a = store_a.get_signing_key()
    # Disk file is created with the same value.
    key_path = tmp_path / "signing_key"
    assert key_path.read_text().strip() == key_a.get_secret_value()
    # A fresh store reads the same key.
    store_b = FileAuthStore(data_directory=tmp_path)
    assert store_b.get_signing_key().get_secret_value() == key_a.get_secret_value()


def test_signing_key_empty_file_raises(tmp_path: Path) -> None:
    """An empty signing-key file is treated as a broken store rather than silently regenerated."""
    key_path = tmp_path / "signing_key"
    key_path.write_text("")
    store = FileAuthStore(data_directory=tmp_path)
    with pytest.raises(ForwardAuthError):
        store.get_signing_key()
