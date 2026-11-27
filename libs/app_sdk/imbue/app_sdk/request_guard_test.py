import pytest
from flask import Flask
from flask import Response
from flask import request
from flask.testing import FlaskClient

from imbue.app_sdk.app_testing import CROSS_SITE_FETCH_HEADERS
from imbue.app_sdk.app_testing import OWNER_IDENTITY_HEADERS
from imbue.app_sdk.app_testing import assert_request_guard_refuses_unknown_provenance
from imbue.app_sdk.identity import IDENTITY_HEADER
from imbue.app_sdk.identity import OTHER_ORIGIN_REFUSAL
from imbue.app_sdk.identity import UNTRUSTED_LOOPBACK_REFUSAL
from imbue.app_sdk.identity import parse_identity_header
from imbue.app_sdk.request_guard import install_request_guard

_PUBLIC_MODULE_RULE = "/_static/module.js"


def _guarded_app() -> Flask:
    app = Flask("guarded", static_folder=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True
    order: list[str] = []
    install_request_guard(app, exempt_rules=frozenset({_PUBLIC_MODULE_RULE}))

    @app.before_request
    def note_the_later_hook_ran() -> None:
        order.append("later hook")

    @app.post("/act")
    def act() -> Response:
        return Response("acted", mimetype="text/plain")

    @app.get("/page")
    def page() -> Response:
        return Response("<p>page</p>", mimetype="text/html")

    @app.get(_PUBLIC_MODULE_RULE)
    def module() -> Response:
        return Response("export const built = true;", mimetype="text/javascript")

    @app.get("/who")
    def who() -> Response:
        return Response(
            str(parse_identity_header(request.headers.get(IDENTITY_HEADER)).owner),
            mimetype="text/plain",
        )

    app.extensions["hook_order"] = order
    return app


@pytest.fixture
def guarded() -> FlaskClient:
    return _guarded_app().test_client()


def test_an_in_container_caller_and_a_proxied_request_reach_their_route(
    guarded: FlaskClient,
) -> None:
    # The test client is loopback under a localhost Host, as an agent's command or the shell is.
    assert guarded.post("/act").get_data(as_text=True) == "acted"
    # A proxied request carries the header, whatever Host the proxy kept.
    stamped = guarded.post(
        "/act",
        headers=OWNER_IDENTITY_HEADERS,
        base_url="http://screen-x.agent-1.localhost:8421",
    )
    assert stamped.get_data(as_text=True) == "acted"
    # The viewer's own page calls its own origin.
    own = guarded.post("/act", headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"})
    assert own.get_data(as_text=True) == "acted"
    assert_request_guard_refuses_unknown_provenance(guarded, "POST", "/act")


def test_a_page_of_another_origin_is_refused_before_any_other_hook_runs(
    guarded: FlaskClient,
) -> None:
    refused = guarded.post("/act", headers=CROSS_SITE_FETCH_HEADERS)
    assert refused.status_code == 403
    assert refused.get_json() == {"detail": OTHER_ORIGIN_REFUSAL}
    assert guarded.application.extensions["hook_order"] == []
    # Nor does a stamped header let it through, nor a path with no route.
    assert guarded.post("/act", headers={**CROSS_SITE_FETCH_HEADERS, **OWNER_IDENTITY_HEADERS}).status_code == 403
    assert guarded.post("/nowhere", headers=CROSS_SITE_FETCH_HEADERS).status_code == 403


def test_a_get_navigation_from_another_origin_and_a_public_module_are_admitted(
    guarded: FlaskClient,
) -> None:
    framed = guarded.get("/page", headers={"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate"})
    assert framed.status_code == 200
    imported = guarded.get(_PUBLIC_MODULE_RULE, headers=CROSS_SITE_FETCH_HEADERS)
    assert imported.status_code == 200
    assert guarded.get("/page", headers=CROSS_SITE_FETCH_HEADERS).status_code == 403


def test_a_header_less_request_is_admitted_from_loopback_alone_under_a_loopback_host(
    guarded: FlaskClient,
) -> None:
    rebound = guarded.post("/act", base_url="http://rebound.example:8090")
    assert rebound.status_code == 403
    assert rebound.get_json() == {"detail": UNTRUSTED_LOOPBACK_REFUSAL}
    assert guarded.post("/act", environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code == 403
    assert guarded.post("/act", base_url="http://127.0.0.1:8090").status_code == 200
    assert guarded.post("/act", base_url="http://[::1]:8090", environ_base={"REMOTE_ADDR": "::1"}).status_code == 200


def test_a_present_header_that_does_not_parse_is_answered_with_a_400_on_every_route(
    guarded: FlaskClient,
) -> None:
    assert guarded.get("/who").get_data(as_text=True) == "True"
    refused = guarded.get("/who", headers={IDENTITY_HEADER: "{not json"})
    assert refused.status_code == 400
    assert IDENTITY_HEADER in refused.get_json()["detail"]
