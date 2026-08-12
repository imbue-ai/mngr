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
@pytest.mark.witnesses(
    "browser-authorization.no-open-redirects",
    partial="witnesses the confinement predicate's accept side; per-route application is witnessed separately",
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
@pytest.mark.witnesses(
    "browser-authorization.no-open-redirects",
    partial="witnesses the confinement predicate's reject side (incl. the '/\\host' form); per-route application is witnessed separately",
)
def test_safe_local_redirect_path_rejects_unsafe_values(raw: str | None) -> None:
    assert safe_local_redirect_path(raw) is None
