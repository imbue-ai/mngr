import pytest

from imbue.minds.desktop_client.responses import safe_local_redirect_path


@pytest.mark.parametrize(
    "raw",
    [
        "/create",
        "/post-login?return_to=%2Fcreate",
        "/accounts",
        "/",
    ],
)
def test_safe_local_redirect_path_accepts_same_origin_paths(raw: str) -> None:
    assert safe_local_redirect_path(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "create",
        "//evil.com",
        "/\\evil.com",
        "https://evil.com",
        "http://evil.com/create",
        "javascript:alert(1)",
    ],
)
def test_safe_local_redirect_path_rejects_unsafe_values(raw: str | None) -> None:
    assert safe_local_redirect_path(raw) is None
