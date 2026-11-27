import pytest
from flask import Blueprint
from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from pydantic import BaseModel

from imbue.app_sdk.api_schema import AlternateResponse
from imbue.app_sdk.api_schema import ErrorResponse
from imbue.app_sdk.api_schema import RouteModels
from imbue.app_sdk.api_schema import RouteTable
from imbue.app_sdk.api_schema import SchemalessRoute
from imbue.app_sdk.api_schema import register_api_schema_route
from imbue.app_sdk.errors import RouteModelsMismatchError
from imbue.app_sdk.routes import error_response
from imbue.app_sdk.routes import model_response
from imbue.app_sdk.routes import parse_request_body


class Greeting(BaseModel):
    name: str


class Greeted(BaseModel):
    text: str


class Queued(BaseModel):
    position: int


def _greeter(route_table: RouteTable) -> Flask:
    app = Flask("greeter", static_folder=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True

    @app.post("/greet")
    def greet() -> Response:
        """Greet by name."""
        body = parse_request_body(Greeting)
        if body.name == "later":
            return model_response(Queued(position=3), 202)
        if body.name == "nobody":
            return error_response("nobody to greet", 404)
        return model_response(Greeted(text=f"hello {body.name}"))

    @app.get("/whoami")
    def whoami() -> Response:
        """Who greets."""
        return model_response(Greeted(text="the greeter"))

    @app.get("/page")
    def page() -> Response:
        """A page that wrongly answers a model."""
        return model_response(Greeted(text="page"))

    register_api_schema_route(
        app,
        "/api/schema",
        title="Greeter",
        description="Greets.",
        route_table=route_table,
    )
    return app


_TABLE = {
    ("POST", "/greet"): RouteModels(
        request_model=Greeting,
        response_model=Greeted,
        alternate_responses=(AlternateResponse(status=202, model=Queued),),
    ),
    ("GET", "/whoami"): RouteModels(response_model=Greeted),
    ("GET", "/page"): SchemalessRoute(reason="a page"),
}


def test_a_route_parses_and_answers_the_models_its_table_names() -> None:
    client = _greeter(_TABLE).test_client()

    assert client.post("/greet", json={"name": "Kim"}).get_json() == {"text": "hello Kim"}
    # Flask serves a HEAD with the GET view, so it is the GET operation the table names.
    headed = client.head("/whoami")
    assert (headed.status_code, headed.data) == (200, b"")
    queued = client.post("/greet", json={"name": "later"})
    assert (queued.status_code, queued.get_json()) == (202, {"position": 3})
    refused = client.post("/greet", json={"name": "nobody"})
    assert (refused.status_code, refused.get_json()) == (
        404,
        {"detail": "nobody to greet"},
    )


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ("[1, 2]", "the request body must be a JSON object"),
        ('{"title": "x"}', "field 'name': Field required"),
    ],
)
def test_a_body_off_the_model_is_a_400_naming_the_problem(body: str, detail: str) -> None:
    client = _greeter(_TABLE).test_client()

    response = client.post("/greet", data=body, content_type="application/json")

    assert (response.status_code, response.get_json()) == (400, {"detail": detail})


def test_a_parse_the_table_does_not_name_fails_the_request() -> None:
    table = {
        **_TABLE,
        ("POST", "/greet"): RouteModels(request_model=Greeted, response_model=Greeted),
    }
    client = _greeter(table).test_client()

    with pytest.raises(
        RouteModelsMismatchError,
        match="parses its body as Greeting, but the route table names Greeted",
    ):
        client.post("/greet", json={"name": "Kim"})


def test_a_parse_under_an_entry_whose_view_parses_its_own_body_fails_the_request() -> None:
    table = {
        **_TABLE,
        ("POST", "/greet"): RouteModels(request_model=Greeting, response_model=Greeted, is_body_parsed_by_view=True),
    }
    client = _greeter(table).test_client()

    with pytest.raises(
        RouteModelsMismatchError,
        match="parses its body as Greeting, but the route table says the view parses its body itself",
    ):
        client.post("/greet", json={"name": "Kim"})


def test_an_answer_the_table_does_not_name_fails_the_request() -> None:
    table = {
        **_TABLE,
        ("POST", "/greet"): RouteModels(request_model=Greeting, response_model=Greeted),
    }
    client = _greeter(table).test_client()

    with pytest.raises(RouteModelsMismatchError, match="answers Queued under 202"):
        client.post("/greet", json={"name": "later"})


def test_a_schemaless_route_may_not_answer_a_model() -> None:
    client = _greeter(_TABLE).test_client()

    with pytest.raises(RouteModelsMismatchError, match="declares it schemaless"):
        client.get("/page")


def test_a_route_the_table_leaves_out_may_not_answer_a_model() -> None:
    client = _greeter({key: entry for key, entry in _TABLE.items() if key[1] != "/page"}).test_client()

    with pytest.raises(RouteModelsMismatchError, match="does not list it"):
        client.get("/page")


def test_a_view_that_parses_its_own_body_is_documented_but_not_checked() -> None:
    app = Flask("own", static_folder=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True

    @app.post("/greet")
    def greet() -> Response:
        """Greet, reading the body by hand."""
        body = request.get_json(force=True, silent=True) or {}
        return model_response(Greeted(text=f"hello {body.get('name', '?')}"))

    register_api_schema_route(
        app,
        "/api/schema",
        title="Own",
        description="Parses on its own.",
        route_table={
            ("POST", "/greet"): RouteModels(
                request_model=Greeting,
                response_model=Greeted,
                is_body_parsed_by_view=True,
            )
        },
    )
    client = app.test_client()

    assert client.post("/greet", json={"name": "Kim"}).get_json() == {"text": "hello Kim"}
    assert client.get("/api/schema").get_json()["paths"]["/greet"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/Greeting"}


class RicherFailure(ErrorResponse):
    kind: str


def _handmade(route_table: RouteTable) -> Flask:
    app = Flask("handmade", static_folder=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True

    @app.post("/greet")
    def greet() -> Response:
        """Greet, answering JSON by hand."""
        return jsonify({"text": "hello"})

    @app.post("/skip")
    def skip() -> Response:
        """Succeed without reading the body."""
        return model_response(Greeted(text="unread"))

    @app.get("/richer")
    def richer() -> Response:
        """Refuse with a failure richer than the error shape."""
        return model_response(RicherFailure(detail="no", kind="busy"), 409)

    register_api_schema_route(app, "/api/schema", title="Handmade", description="x", route_table=route_table)
    return app


_HANDMADE_TABLE = {
    ("POST", "/greet"): RouteModels(response_model=Greeted),
    ("POST", "/skip"): RouteModels(request_model=Greeting, response_model=Greeted),
    ("GET", "/richer"): RouteModels(response_model=Greeted),
}


def test_a_modelled_route_that_answers_json_by_hand_fails_the_request() -> None:
    client = _handmade(_HANDMADE_TABLE).test_client()

    with pytest.raises(RouteModelsMismatchError, match="answered JSON without model_response"):
        client.post("/greet", json={})


def test_a_modelled_route_that_succeeds_without_reading_its_body_fails_the_request() -> None:
    client = _handmade(_HANDMADE_TABLE).test_client()

    with pytest.raises(
        RouteModelsMismatchError,
        match="without reading its body through parse_request_body",
    ):
        client.post("/skip", json={"name": "Kim"})


def test_a_failure_richer_than_the_error_shape_must_be_listed() -> None:
    client = _handmade(_HANDMADE_TABLE).test_client()

    with pytest.raises(RouteModelsMismatchError, match="answers RicherFailure under 409"):
        client.get("/richer")

    listed = {
        **_HANDMADE_TABLE,
        ("GET", "/richer"): RouteModels(
            response_model=Greeted,
            alternate_responses=(AlternateResponse(status=409, model=RicherFailure),),
        ),
    }
    refused = _handmade(listed).test_client().get("/richer")
    assert (refused.status_code, refused.get_json()) == (
        409,
        {"detail": "no", "kind": "busy"},
    )


def test_a_table_registered_on_a_blueprint_guards_a_view_on_the_app_too() -> None:
    blueprint = Blueprint("pages", __name__)
    register_api_schema_route(
        blueprint,
        "/api/schema",
        title="Split",
        description="x",
        route_table={
            ("POST", "/greet"): RouteModels(request_model=Greeting, response_model=Greeted),
        },
    )
    app = Flask("split", static_folder=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True
    app.register_blueprint(blueprint)

    @app.post("/greet")
    def greet() -> Response:
        """Greet from the app itself."""
        return model_response(Greeted(text=parse_request_body(Greeting).name))

    client = app.test_client()
    assert client.post("/greet", json={"name": "Kim"}).get_json() == {"text": "Kim"}
    refused = client.post("/greet", json={"title": "x"})
    assert (refused.status_code, refused.get_json()) == (
        400,
        {"detail": "field 'name': Field required"},
    )
