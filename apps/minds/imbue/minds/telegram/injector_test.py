"""Unit tests for injector constants."""

from imbue.minds.telegram.injector import _SECRETS_FILE


def test_secrets_file_path_is_in_runtime_directory() -> None:
    assert _SECRETS_FILE == "runtime/secrets"
