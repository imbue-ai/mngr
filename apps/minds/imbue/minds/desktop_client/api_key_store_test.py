from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.api_key_store import is_valid_minds_api_key


def test_generate_api_key_returns_unique_keys() -> None:
    assert generate_api_key() != generate_api_key()


def test_generate_api_key_is_url_safe() -> None:
    # ``token_urlsafe`` only emits the unreserved URL characters
    # ``[A-Za-z0-9_-]``, which is what callers depend on when sticking
    # the value into a Bearer header without further encoding.
    key = generate_api_key()
    assert all(c.isalnum() or c in "-_" for c in key)


def test_is_valid_minds_api_key_accepts_matching_key() -> None:
    key = generate_api_key()
    assert is_valid_minds_api_key(key, key)


def test_is_valid_minds_api_key_rejects_mismatched_key() -> None:
    assert not is_valid_minds_api_key("a", "b")


def test_is_valid_minds_api_key_rejects_empty_presented() -> None:
    assert not is_valid_minds_api_key("", "some-key")


def test_is_valid_minds_api_key_rejects_empty_expected() -> None:
    assert not is_valid_minds_api_key("some-key", "")
