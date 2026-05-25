import os
import stat
from pathlib import Path

from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.api_key_store import is_valid_minds_api_key
from imbue.minds.desktop_client.api_key_store import load_or_create_minds_api_key
from imbue.minds.desktop_client.api_key_store import minds_api_key_path


def test_generate_api_key_returns_unique_keys() -> None:
    assert generate_api_key() != generate_api_key()


def test_generate_api_key_is_url_safe() -> None:
    # ``token_urlsafe`` only emits the unreserved URL characters
    # ``[A-Za-z0-9_-]``, which is what callers depend on when sticking
    # the value into a Bearer header without further encoding.
    key = generate_api_key()
    assert all(c.isalnum() or c in "-_" for c in key)


def test_load_or_create_creates_file_when_missing(tmp_path: Path) -> None:
    key = load_or_create_minds_api_key(tmp_path)
    assert key
    file_path = minds_api_key_path(tmp_path)
    assert file_path.is_file()
    assert file_path.read_text() == key


def test_load_or_create_is_idempotent(tmp_path: Path) -> None:
    first = load_or_create_minds_api_key(tmp_path)
    second = load_or_create_minds_api_key(tmp_path)
    assert first == second


def test_load_or_create_writes_file_at_mode_0600(tmp_path: Path) -> None:
    load_or_create_minds_api_key(tmp_path)
    file_path = minds_api_key_path(tmp_path)
    mode = stat.S_IMODE(os.stat(file_path).st_mode)
    assert mode == 0o600


def test_load_or_create_regenerates_when_file_is_empty(tmp_path: Path) -> None:
    file_path = minds_api_key_path(tmp_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("")
    regenerated = load_or_create_minds_api_key(tmp_path)
    assert regenerated
    assert file_path.read_text() == regenerated


def test_load_or_create_preserves_existing_key_across_calls(tmp_path: Path) -> None:
    file_path = minds_api_key_path(tmp_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("preexisting-key-value")
    assert load_or_create_minds_api_key(tmp_path) == "preexisting-key-value"


def test_is_valid_minds_api_key_accepts_matching_key() -> None:
    key = generate_api_key()
    assert is_valid_minds_api_key(key, key)


def test_is_valid_minds_api_key_rejects_mismatched_key() -> None:
    assert not is_valid_minds_api_key("a", "b")


def test_is_valid_minds_api_key_rejects_empty_presented() -> None:
    assert not is_valid_minds_api_key("", "some-key")


def test_is_valid_minds_api_key_rejects_empty_expected() -> None:
    assert not is_valid_minds_api_key("some-key", "")
