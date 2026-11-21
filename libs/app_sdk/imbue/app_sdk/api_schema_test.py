from pathlib import Path

import pytest
from flask import Blueprint
from flask import Flask
from flask import Response
from flask import request
from openapi_spec_validator import validate
from pydantic import BaseModel

from imbue.app_sdk.api_schema import AlternateResponse
from imbue.app_sdk.api_schema import EXTENSION_KEY
from imbue.app_sdk.api_schema import NO_SCHEMA_KEY
from imbue.app_sdk.api_schema import OTHER_METHODS_KEY
from imbue.app_sdk.api_schema import RouteModels
from imbue.app_sdk.api_schema import RouteTable
from imbue.app_sdk.api_schema import SchemalessRoute
from imbue.app_sdk.api_schema import build_openapi_document
from imbue.app_sdk.api_schema import flask_rule_to_openapi_path
from imbue.app_sdk.api_schema import merge_route_tables
from imbue.app_sdk.api_schema import register_api_schema_route
from imbue.app_sdk.app_testing import assert_api_schema_describes_app
from imbue.app_sdk.errors import RouteTableConflictError


class NoteBody(BaseModel):
    title: str
    text: str


class NoteRecord(BaseModel):
    id: str
    body: NoteBody


class NoteCreated(BaseModel):
    id: str


_NOTES_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the notes page"),
    ("POST", "/api/notes"): RouteModels(
        request_model=NoteBody,
        response_model=NoteRecord,
        alternate_responses=(AlternateResponse(status=201, model=NoteCreated),),
    ),
    ("GET", "/api/notes/{note_id}"): RouteModels(response_model=NoteRecord),
    ("DELETE", "/api/notes/{note_id}"): RouteModels(success_status=204),
    ("GET", "/dav/{name}"): SchemalessRoute(reason="the notes over WebDAV"),
    ("GET", "/tree/"): SchemalessRoute(reason="the folder tree as text"),
    ("GET", "/tree/{subpath}"): SchemalessRoute(reason="the folder tree as text"),
}


def _notes_app() -> Flask:
    app = Flask("notes", static_folder=None)

    @app.get("/")
    def index() -> Response:
        """The notes page."""
        return Response("notes", mimetype="text/html")

    @app.post("/api/notes")
    def create_note() -> Response:
        """Create a note.

        The body carries a title and the text; the answer is the stored record.
        """
        return Response("{}", mimetype="application/json")

    @app.route("/api/notes/<note_id>", methods=["GET", "DELETE"])
    def one_note(note_id: str) -> Response:
        """One note by id."""
        return Response("{}", mimetype="application/json")

    @app.route("/dav/<path:name>", methods=["PROPFIND", "MKCOL", "GET"])
    def dav(name: str) -> Response:
        """The notes over WebDAV."""
        return Response("")

    @app.route("/tree/", defaults={"subpath": ""})
    @app.route("/tree/<path:subpath>")
    def tree(subpath: str) -> Response:
        """The folder tree, from the root or a subpath."""
        return Response(subpath)

    return app


def _notes_document(app: Flask, route_table: RouteTable) -> dict:
    return build_openapi_document(
        app,
        title="Notes",
        description="Keeps notes.",
        version="1.0",
        route_table=route_table,
        excluded_endpoints=frozenset(),
    )


def test_flask_rules_become_openapi_paths() -> None:
    assert flask_rule_to_openapi_path("/api/notes/<note_id>") == "/api/notes/{note_id}"
    assert flask_rule_to_openapi_path("/files/<path:name>/<int:n>") == "/files/{name}/{n}"


def test_the_document_covers_every_route_with_docstrings_parameters_and_models() -> None:
    app = _notes_app()
    document = _notes_document(app, _NOTES_TABLE)
    validate(document)
    assert document["info"] == {
        "title": "Notes",
        "version": "1.0",
        "description": "Keeps notes.",
    }
    assert set(document["paths"]) == {
        "/",
        "/api/notes",
        "/api/notes/{note_id}",
        "/dav/{name}",
        "/tree/",
        "/tree/{subpath}",
    }
    # A rule that fills an argument from ``defaults`` has no such parameter in its path.
    assert "parameters" not in document["paths"]["/tree/"]["get"]
    assert [parameter["name"] for parameter in document["paths"]["/tree/{subpath}"]["get"]["parameters"]] == [
        "subpath"
    ]
    # Methods OpenAPI has no operation for are listed, not dropped and not made into invalid operations.
    assert document["paths"]["/dav/{name}"][OTHER_METHODS_KEY] == ["MKCOL", "PROPFIND"]
    assert set(document["paths"]["/dav/{name}"]) == {"get", OTHER_METHODS_KEY}
    create = document["paths"]["/api/notes"]["post"]
    assert create["summary"] == "Create a note."
    assert create["description"].endswith("the answer is the stored record.")
    assert create["requestBody"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/NoteBody"}
    assert create["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/NoteRecord"
    }
    # An alternate success is listed under its own status, and every operation's default is the error shape.
    assert create["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/NoteCreated"
    }
    assert create["responses"]["default"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert set(document["components"]["schemas"]) == {
        "ErrorResponse",
        "NoteBody",
        "NoteCreated",
        "NoteRecord",
    }
    one = document["paths"]["/api/notes/{note_id}"]
    assert set(one) == {"get", "delete"}
    assert one["get"]["parameters"] == [
        {
            "name": "note_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
    ]
    assert one["get"]["operationId"] == "get_api_notes_note_id"
    # An answer with no body is listed under its status with no content.
    assert "content" not in one["delete"]["responses"]["204"]
    # A schemaless route says why, in the document and on its success.
    page = document["paths"]["/"]["get"]
    assert page[NO_SCHEMA_KEY] == "the notes page"
    assert page["responses"]["200"]["description"] == "the notes page"
    assert set(_notes_document(app, {})["components"]["schemas"]) == {"ErrorResponse"}


def test_a_blank_docstring_describes_nothing() -> None:
    app = Flask("blank", static_folder=None)

    @app.get("/")
    def index() -> Response:
        """ """
        return Response("")

    document = build_openapi_document(
        app,
        title="Blank",
        description="x",
        version="1.0",
        route_table={},
        excluded_endpoints=frozenset(),
    )
    operation = document["paths"]["/"]["get"]
    assert "summary" not in operation
    assert "description" not in operation


def test_the_summary_is_the_first_paragraph_as_one_line() -> None:
    app = Flask("wrapped", static_folder=None)

    @app.get("/")
    def index() -> Response:
        """Register a new note and return its id at once (the
        indexing runs in the background).

        The second paragraph is only the description's.
        """
        return Response("")

    @app.get("/one-liner")
    def one_liner() -> Response:
        """One line."""
        return Response("")

    document = build_openapi_document(
        app,
        title="Wrapped",
        description="x",
        version="1.0",
        route_table={},
        excluded_endpoints=frozenset(),
    )
    assert (
        document["paths"]["/"]["get"]["summary"]
        == "Register a new note and return its id at once (the indexing runs in the background)."
    )
    assert document["paths"]["/"]["get"]["description"].endswith("The second paragraph is only the description's.")
    assert document["paths"]["/one-liner"]["get"]["summary"] == "One line."


def test_the_route_serves_the_document_without_describing_itself(
    notes_manifest_path: Path,
) -> None:
    app = _notes_app()
    register_api_schema_route(
        app,
        "/api/schema",
        title="Notes",
        description="Keeps the person's notes and finds them again.",
        route_table=_NOTES_TABLE,
    )
    client = app.test_client()

    assert_api_schema_describes_app(client, notes_manifest_path)
    assert "/api/schema" not in client.get("/api/schema").get_json()["paths"]
    assert app.extensions[EXTENSION_KEY] == _NOTES_TABLE


def test_the_route_works_on_a_blueprint_and_sees_routes_registered_later(
    notes_manifest_path: Path,
) -> None:
    blueprint = Blueprint("pages", __name__, static_folder="static")

    @blueprint.get("/api/health")
    def health() -> Response:
        """Whether the app is up."""
        return Response("{}", mimetype="application/json")

    register_api_schema_route(
        blueprint,
        "/api/schema",
        title="Notes",
        description="Keeps the person's notes and finds them again.",
        route_table={
            ("GET", "/api/health"): SchemalessRoute(reason="an empty object"),
            ("GET", "/late"): SchemalessRoute(reason="plain text"),
        },
    )
    app = Flask("notes", static_folder=None)
    app.register_blueprint(blueprint)

    @app.get("/late")
    def late() -> Response:
        """A route registered after the document's."""
        return Response("late")

    assert_api_schema_describes_app(app.test_client(), notes_manifest_path)
    assert set(app.test_client().get("/api/schema").get_json()["paths"]) == {
        "/api/health",
        "/late",
    }
    assert EXTENSION_KEY in app.extensions


def test_the_assertion_refuses_a_route_the_table_leaves_out(
    notes_manifest_path: Path,
) -> None:
    app = _notes_app()
    register_api_schema_route(
        app,
        "/api/schema",
        title="Notes",
        description="Keeps the person's notes and finds them again.",
        route_table={key: entry for key, entry in _NOTES_TABLE.items() if key[1] != "/tree/"},
    )

    with pytest.raises(AssertionError, match="not in the route table.*GET /tree/"):
        assert_api_schema_describes_app(app.test_client(), notes_manifest_path)


def test_the_assertion_refuses_a_table_entry_the_app_lacks(
    notes_manifest_path: Path,
) -> None:
    app = _notes_app()
    register_api_schema_route(
        app,
        "/api/schema",
        title="Notes",
        description="Keeps the person's notes and finds them again.",
        route_table={
            **_NOTES_TABLE,
            ("GET", "/gone"): SchemalessRoute(reason="nothing"),
        },
    )

    with pytest.raises(AssertionError, match="routes the app does not have.*GET /gone"):
        assert_api_schema_describes_app(app.test_client(), notes_manifest_path)


def test_the_assertion_refuses_a_modelled_view_that_reads_its_body_raw(
    notes_manifest_path: Path,
) -> None:
    app = Flask("notes", static_folder=None)

    @app.post("/api/notes")
    def create_note() -> Response:
        """Create a note."""
        body = request.get_json(force=True, silent=True) or {}
        return Response(str(body), mimetype="application/json")

    register_api_schema_route(
        app,
        "/api/schema",
        title="Notes",
        description="Keeps the person's notes and finds them again.",
        route_table={("POST", "/api/notes"): RouteModels(request_model=NoteBody, response_model=NoteRecord)},
    )

    with pytest.raises(AssertionError, match="without parse_request_body.*create_note"):
        assert_api_schema_describes_app(app.test_client(), notes_manifest_path)


def test_several_models_under_one_status_are_published_as_a_choice() -> None:
    app = Flask("choice", static_folder=None)

    @app.post("/api/notes")
    def create_note() -> Response:
        """Create a note, or say it was queued."""
        return Response("{}", mimetype="application/json")

    document = build_openapi_document(
        app,
        title="Choice",
        description="x",
        version="1.0",
        route_table={
            ("POST", "/api/notes"): RouteModels(
                response_model=NoteRecord,
                alternate_responses=(
                    AlternateResponse(status=200, model=NoteCreated),
                    AlternateResponse(status=204),
                    AlternateResponse(status=502, model=NoteRecord),
                ),
            )
        },
        excluded_endpoints=frozenset(),
    )
    validate(document)
    responses = document["paths"]["/api/notes"]["post"]["responses"]
    assert responses["200"]["content"]["application/json"]["schema"] == {
        "oneOf": [
            {"$ref": "#/components/schemas/NoteRecord"},
            {"$ref": "#/components/schemas/NoteCreated"},
        ]
    }
    assert "content" not in responses["204"]
    # A failure status that answers a model is listed with its schema, and not called a success.
    assert responses["502"]["description"] == "Failure"
    assert responses["502"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/NoteRecord"}
    assert responses["200"]["description"] == responses["204"]["description"] == "Success"


def test_merging_route_tables_refuses_a_route_two_of_them_name() -> None:
    first = {
        ("GET", "/a"): SchemalessRoute(reason="a"),
        ("GET", "/b"): SchemalessRoute(reason="b"),
    }
    second = {("GET", "/c"): SchemalessRoute(reason="c")}
    assert set(merge_route_tables(first, second)) == {
        ("GET", "/a"),
        ("GET", "/b"),
        ("GET", "/c"),
    }
    with pytest.raises(RouteTableConflictError, match="GET /b"):
        merge_route_tables(first, {("GET", "/b"): SchemalessRoute(reason="again")})
