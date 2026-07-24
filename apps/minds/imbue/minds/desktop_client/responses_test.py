import pytest

from imbue.minds.desktop_client.responses import safe_local_redirect_path


@pytest.mark.witnesses(
    "authentication.no-open-redirects",
    partial="unit-level positive direction of the shared root-relative guard (root-relative paths are honored); the goto-bridge next param uses a separate guard in mngr_forward",
)
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


@pytest.mark.witnesses(
    "authentication.no-open-redirects",
    partial="unit-level: the shared root-relative guard rejects protocol-relative, backslash, scheme, host, and non-root forms; the goto-bridge next param uses a separate guard in mngr_forward",
)
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
