from pathlib import Path
from typing import Any

import pytest

from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.errors import SigningKeyError
from imbue.minds.primitives import OneTimeCode


def _make_auth_store(tmp_path: Path) -> FileAuthStore:
    return FileAuthStore(data_directory=tmp_path / "auth")


def test_get_signing_key_generates_key_on_first_access(tmp_path: Path) -> None:
    store = _make_auth_store(tmp_path)
    key = store.get_signing_key()
    assert len(key.get_secret_value()) > 32


def test_get_signing_key_returns_same_key_on_subsequent_access(tmp_path: Path) -> None:
    store = _make_auth_store(tmp_path)
    key_first = store.get_signing_key()
    key_second = store.get_signing_key()
    assert key_first.get_secret_value() == key_second.get_secret_value()


def test_get_signing_key_persists_across_instances(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    store_a = FileAuthStore(data_directory=auth_dir)
    key_a = store_a.get_signing_key()

    store_b = FileAuthStore(data_directory=auth_dir)
    key_b = store_b.get_signing_key()

    assert key_a.get_secret_value() == key_b.get_secret_value()


def test_get_signing_key_raises_for_empty_key_file(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "signing_key").write_text("")

    store = FileAuthStore(data_directory=auth_dir)
    with pytest.raises(SigningKeyError):
        store.get_signing_key()


def test_add_and_validate_one_time_code(tmp_path: Path) -> None:
    store = _make_auth_store(tmp_path)
    code = OneTimeCode("test-code-82734")

    store.add_one_time_code(code=code)

    is_valid = store.validate_and_consume_code(code=code)
    assert is_valid is True


def test_validate_rejects_unknown_code(tmp_path: Path) -> None:
    store = _make_auth_store(tmp_path)

    is_valid = store.validate_and_consume_code(
        code=OneTimeCode("unknown-code-38294"),
    )
    assert is_valid is False


def test_validate_rejects_already_used_code(tmp_path: Path) -> None:
    store = _make_auth_store(tmp_path)
    code = OneTimeCode("single-use-code-19283")

    store.add_one_time_code(code=code)

    first_result = store.validate_and_consume_code(code=code)
    assert first_result is True

    second_result = store.validate_and_consume_code(code=code)
    assert second_result is False


def test_codes_persist_across_store_instances(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    code = OneTimeCode("persistent-code-39271")

    store_a = FileAuthStore(data_directory=auth_dir)
    store_a.add_one_time_code(code=code)

    store_b = FileAuthStore(data_directory=auth_dir)
    is_valid = store_b.validate_and_consume_code(code=code)
    assert is_valid is True


def test_signing_key_file_has_restricted_permissions(tmp_path: Path) -> None:
    store = _make_auth_store(tmp_path)
    store.get_signing_key()

    key_path = tmp_path / "auth" / "signing_key"
    permissions = key_path.stat().st_mode & 0o777
    assert permissions == 0o600


def test_get_signing_key_reads_existing_key(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "signing_key").write_text("my-custom-key-82734")

    store = FileAuthStore(data_directory=auth_dir)
    key = store.get_signing_key()
    assert key.get_secret_value() == "my-custom-key-82734"


def test_get_signing_key_raises_on_read_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If an existing signing key file is not readable, SigningKeyError is raised."""
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)
    key_file = auth_dir / "signing_key"
    key_file.write_text("some-key")

    # Monkeypatch read_text to simulate a read failure. We can't use chmod
    # because Modal sandboxes run as root, which bypasses permission checks.
    original_read_text = Path.read_text

    def _raise_on_key_file(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "signing_key":
            raise OSError("simulated read failure")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_on_key_file)

    store = FileAuthStore(data_directory=auth_dir)
    with pytest.raises(SigningKeyError):
        store.get_signing_key()


def test_get_signing_key_raises_on_write_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If the auth directory cannot be written to, SigningKeyError is raised on key generation."""
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)

    # Monkeypatch write_text to simulate a write failure. We can't use chmod
    # because Modal sandboxes run as root, which bypasses permission checks.
    original_write_text = Path.write_text

    def _raise_on_key_file(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == "signing_key":
            raise OSError("simulated write failure")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _raise_on_key_file)

    store = FileAuthStore(data_directory=auth_dir)
    with pytest.raises(SigningKeyError):
        store.get_signing_key()


def test_validate_code_returns_false_on_json_decode_error(tmp_path: Path) -> None:
    """If the codes file contains invalid JSON, code validation returns False without crashing."""
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "one_time_codes.json").write_text("not valid json {{{")

    store = FileAuthStore(data_directory=auth_dir)
    result = store.validate_and_consume_code(OneTimeCode("any-code"))
    assert result is False
