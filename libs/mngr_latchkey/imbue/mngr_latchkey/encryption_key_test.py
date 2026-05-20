"""Unit tests for the per-latchkey-directory encryption-key resolution.

Covers the three precedence branches (env override, existing file, fresh
mint) plus the on-load permission validation that refuses key files
readable or writable by anyone other than the owner.
"""

import os
from pathlib import Path

import pytest

from imbue.mngr_latchkey.encryption_key import ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.encryption_key import LATCHKEY_ENCRYPTION_KEY_ENV_VAR
from imbue.mngr_latchkey.encryption_key import LatchkeyEncryptionKeyPermissionError
from imbue.mngr_latchkey.encryption_key import encryption_key_path
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key


def _key_path(latchkey_directory: Path) -> Path:
    """Convenience over :func:`encryption_key_path` for shorter test code."""
    return encryption_key_path(latchkey_directory)


def _disable_operator_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the test environment never accidentally satisfies branch 1.

    The CI / dev shell can carry an inherited ``LATCHKEY_ENCRYPTION_KEY``;
    every test that wants to exercise the file path explicitly clears it
    so the precedence rule under test is the one we actually probe.
    """
    monkeypatch.delenv(LATCHKEY_ENCRYPTION_KEY_ENV_VAR, raising=False)


# -- Precedence: operator override ---------------------------------------------


def test_operator_env_override_wins_over_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the env var is set and no file exists, the env value is returned verbatim."""
    monkeypatch.setenv(LATCHKEY_ENCRYPTION_KEY_ENV_VAR, "operator-supplied-key")

    key = load_or_create_encryption_key(tmp_path)

    assert key.get_secret_value() == "operator-supplied-key"
    assert not _key_path(tmp_path).exists()


def test_operator_env_override_wins_over_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var wins even when a per-env key file is already on disk; the file is not consulted."""
    file_key = "on-disk-key"
    _key_path(tmp_path).write_text(file_key, encoding="utf-8")
    _key_path(tmp_path).chmod(0o600)
    monkeypatch.setenv(LATCHKEY_ENCRYPTION_KEY_ENV_VAR, "operator-supplied-key")

    key = load_or_create_encryption_key(tmp_path)

    assert key.get_secret_value() == "operator-supplied-key"


def test_operator_env_override_skips_permission_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A world-readable on-disk key is fine when the operator override is in effect.

    The override branch returns before the file is even ``stat``ed; the
    insecure file just sits unread on disk.
    """
    _key_path(tmp_path).write_text("on-disk-key", encoding="utf-8")
    _key_path(tmp_path).chmod(0o644)
    monkeypatch.setenv(LATCHKEY_ENCRYPTION_KEY_ENV_VAR, "operator-supplied-key")

    key = load_or_create_encryption_key(tmp_path)

    assert key.get_secret_value() == "operator-supplied-key"


# -- Precedence: existing file -------------------------------------------------


def test_returns_existing_owner_only_file_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An on-disk 0o600 key file is read and returned verbatim."""
    _disable_operator_override(monkeypatch)
    existing_key = "already-on-disk-key-value"
    _key_path(tmp_path).write_text(existing_key, encoding="utf-8")
    _key_path(tmp_path).chmod(0o600)

    key = load_or_create_encryption_key(tmp_path)

    assert key.get_secret_value() == existing_key


def test_strips_trailing_whitespace_from_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing newline in the key file is stripped before returning.

    Prevents trailing newlines (left by ``echo "$KEY" > encryption_key``
    or similar operator commands) from making it through into the env
    var and tripping latchkey's encryption-key parser.
    """
    _disable_operator_override(monkeypatch)
    _key_path(tmp_path).write_text("padded-key\n  ", encoding="utf-8")
    _key_path(tmp_path).chmod(0o600)

    key = load_or_create_encryption_key(tmp_path)

    assert key.get_secret_value() == "padded-key"


# -- Precedence: fresh mint ----------------------------------------------------


def test_mints_fresh_key_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First call against a fresh directory mints a key and persists it 0600."""
    _disable_operator_override(monkeypatch)

    key = load_or_create_encryption_key(tmp_path)

    on_disk = _key_path(tmp_path).read_text(encoding="utf-8")
    assert on_disk == key.get_secret_value()
    file_mode = _key_path(tmp_path).stat().st_mode & 0o777
    assert file_mode == 0o600


def test_creates_latchkey_directory_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First call against a not-yet-existing directory ``mkdir -p``s it before minting."""
    _disable_operator_override(monkeypatch)
    nested = tmp_path / "does" / "not" / "yet" / "exist"
    assert not nested.exists()

    load_or_create_encryption_key(nested)

    assert nested.is_dir()
    assert _key_path(nested).is_file()


def test_mints_key_with_sufficient_entropy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The minted key matches the upstream ``openssl rand -base64 32`` shape.

    URL-safe base64 of 32 random bytes is 43 chars (no padding when
    ``secrets.token_urlsafe`` is used). A weaker key would silently
    pass the rest of the suite but matter in prod.
    """
    _disable_operator_override(monkeypatch)

    key = load_or_create_encryption_key(tmp_path).get_secret_value()

    # token_urlsafe(32) yields a 43-char URL-safe base64 string with
    # only [A-Za-z0-9_-] characters.
    assert len(key) == 43
    assert all(c.isalnum() or c in "_-" for c in key)


def test_idempotent_across_calls_returns_same_persisted_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Subsequent calls return the file we minted on the first call."""
    _disable_operator_override(monkeypatch)

    first = load_or_create_encryption_key(tmp_path)
    second = load_or_create_encryption_key(tmp_path)

    assert first.get_secret_value() == second.get_secret_value()


# -- Permission validation -----------------------------------------------------


@pytest.mark.parametrize("insecure_mode", [0o604, 0o640, 0o644, 0o660, 0o666, 0o755])
def test_rejects_existing_file_with_group_or_other_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, insecure_mode: int
) -> None:
    """Any group or other access bit on the on-disk file is rejected with a clear error."""
    _disable_operator_override(monkeypatch)
    _key_path(tmp_path).write_text("on-disk-key", encoding="utf-8")
    _key_path(tmp_path).chmod(insecure_mode)

    with pytest.raises(LatchkeyEncryptionKeyPermissionError) as exc_info:
        load_or_create_encryption_key(tmp_path)

    message = str(exc_info.value)
    assert str(_key_path(tmp_path)) in message
    assert f"chmod 600 {_key_path(tmp_path)}" in message


@pytest.mark.parametrize("safe_mode", [0o400, 0o600, 0o700])
def test_accepts_owner_only_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, safe_mode: int) -> None:
    """Owner-only modes (no group / other bits) load cleanly even when not exactly 0o600."""
    _disable_operator_override(monkeypatch)
    _key_path(tmp_path).write_text("on-disk-key", encoding="utf-8")
    _key_path(tmp_path).chmod(safe_mode)

    key = load_or_create_encryption_key(tmp_path)

    assert key.get_secret_value() == "on-disk-key"


def test_permission_error_inherits_from_permissionerror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catching plain ``PermissionError`` is enough to handle the validation failure.

    Locks in the inheritance choice so existing ``except PermissionError``
    blocks keep working after this validation lands.
    """
    _disable_operator_override(monkeypatch)
    _key_path(tmp_path).write_text("on-disk-key", encoding="utf-8")
    _key_path(tmp_path).chmod(0o644)

    with pytest.raises(PermissionError):
        load_or_create_encryption_key(tmp_path)


# -- Misc ----------------------------------------------------------------------


def test_encryption_key_path_is_under_directory(tmp_path: Path) -> None:
    """The key file lives at the documented relative path inside the latchkey directory."""
    assert encryption_key_path(tmp_path) == tmp_path / ENCRYPTION_KEY_FILENAME


def test_fresh_mint_persists_to_disk_with_owner_only_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The atomic-link write path also installs 0600 perms (matches the chmod-explicit test).

    Belt-and-suspenders: even when the umask is permissive, the temp
    file is created with mode 0600 (via :func:`tempfile.mkstemp`,
    which ignores umask) and the on-disk file inherits that mode
    through ``os.link``. The next load would reject the file if it
    ended up with group/other bits set.
    """
    _disable_operator_override(monkeypatch)
    # Even a permissive umask must not produce a group/other-readable
    # key file. Save + restore around the test so we don't leak.
    previous_umask = os.umask(0o022)
    try:
        load_or_create_encryption_key(tmp_path)
    finally:
        os.umask(previous_umask)

    file_mode = _key_path(tmp_path).stat().st_mode & 0o777
    # Zero group + other bits.
    assert file_mode & 0o077 == 0
