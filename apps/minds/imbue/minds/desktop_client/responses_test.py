import json

import pytest

from imbue.minds.desktop_client.responses import make_json_error_response
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


def test_json_error_response_carries_a_detail_key_only_when_one_is_given() -> None:
    plain = make_json_error_response("Not ready", 409)
    assert plain.status_code == 409
    assert plain.mimetype == "application/json"
    assert json.loads(plain.get_data(as_text=True)) == {"error": "Not ready"}

    detailed = make_json_error_response("Refused", 502, detail="the chat app answered HTTP 400")
    assert json.loads(detailed.get_data(as_text=True)) == {
        "error": "Refused",
        "detail": "the chat app answered HTTP 400",
    }
