import json
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from openapi_spec_validator import validate

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_schema import _ROUTE_MODELS
from imbue.minds.desktop_client.api_schema import _flask_path_to_openapi
from imbue.minds.desktop_client.api_schema import _is_gateway_reachable_path
from imbue.minds.desktop_client.api_schema import build_openapi_document
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.conftest import make_resolver_with_data

_TEST_KEY = "test-minds-api-key"


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TEST_KEY}"}


def _schema_client(tmp_path: Path) -> FlaskClient:
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=make_resolver_with_data(),
        http_client=None,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
    )
    return app.test_client()


def test_flask_path_to_openapi_converts_params() -> None:
    assert _flask_path_to_openapi("/api/v1/workspaces") == "/api/v1/workspaces"
    assert _flask_path_to_openapi("/api/v1/workspaces/<agent_id>/ssh") == "/api/v1/workspaces/{agent_id}/ssh"
    assert (
        _flask_path_to_openapi("/api/v1/workspaces/<agent_id>/sharing/<service_name>")
        == "/api/v1/workspaces/{agent_id}/sharing/{service_name}"
    )
    # A typed converter (e.g. the WebDAV mount) keeps only the parameter name.
    assert _flask_path_to_openapi("/static/<path:filename>") == "/static/{filename}"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/v1/workspaces", True),
        ("/api/v1/workspaces/{agent_id}/ssh", True),
        ("/api/schema", True),
        ("/api/v1/desktop/running-workspaces", False),
        ("/api/v1/files/foo", False),
        ("/auth/login", False),
        ("/goto/agent/", False),
    ],
)
def test_is_gateway_reachable_path(path: str, expected: bool) -> None:
    assert _is_gateway_reachable_path(path) is expected


def test_build_openapi_document_is_valid_and_filters_unreachable_routes() -> None:
    rules = [
        ("/api/v1/workspaces", frozenset({"GET", "POST", "HEAD", "OPTIONS"}), (), "api_v1.list"),
        ("/api/v1/workspaces/<agent_id>/ssh", frozenset({"POST", "OPTIONS"}), ("agent_id",), "api_v1.ssh"),
        ("/api/v1/desktop/running-workspaces", frozenset({"GET"}), (), "api_v1.desktop"),
        ("/api/v1/files/x", frozenset({"GET"}), (), "api_v1.files"),
        ("/api/schema", frozenset({"GET"}), (), "api_schema._handle_api_schema"),
        ("/auth/login", frozenset({"POST"}), (), "supertokens.login"),
        ("/static/<path:filename>", frozenset({"GET"}), ("filename",), "static"),
    ]

    document = build_openapi_document(rules, {})

    # Valid OpenAPI 3.1 per the official validator.
    validate(document)

    paths = document["paths"]
    assert "/api/v1/workspaces" in paths
    assert "/api/v1/workspaces/{agent_id}/ssh" in paths
    assert "/api/schema" in paths
    # Cookie-only / non-proxied surfaces are excluded so an agent only sees what it can reach.
    assert "/api/v1/desktop/running-workspaces" not in paths
    assert "/api/v1/files/x" not in paths
    assert "/auth/login" not in paths
    assert "/static/{filename}" not in paths


def test_build_openapi_document_includes_request_and_response_schemas() -> None:
    rules = [
        ("/api/v1/workspaces/<agent_id>/ssh", frozenset({"POST"}), ("agent_id",), "api_v1.ssh"),
        ("/api/v1/workspaces", frozenset({"GET"}), (), "api_v1.list"),
    ]

    document = build_openapi_document(rules, {})
    validate(document)

    ssh_post = document["paths"]["/api/v1/workspaces/{agent_id}/ssh"]["post"]
    assert ssh_post["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EstablishSshRequest"
    }
    # Path parameter is documented.
    assert {"name": "agent_id", "in": "path", "required": True, "schema": {"type": "string"}} in ssh_post["parameters"]
    # The response body model and the always-present error model resolve to real components.
    list_get = document["paths"]["/api/v1/workspaces"]["get"]
    assert list_get["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WorkspaceListResponse"
    }
    schemas = document["components"]["schemas"]
    assert "EstablishSshRequest" in schemas
    assert "WorkspaceListResponse" in schemas
    # Nested model ($defs) is hoisted into components so the $ref resolves.
    assert "WorkspaceSummary" in schemas
    assert "ApiErrorResponse" in schemas


def test_api_schema_route_returns_valid_openapi_for_the_real_routes(tmp_path: Path) -> None:
    client = _schema_client(tmp_path)

    response = client.get("/api/schema", headers=_auth_header())

    assert response.status_code == 200
    document = json.loads(response.data)
    validate(document)
    paths = document["paths"]
    # Real registered gateway routes appear; the cookie-only desktop namespace does not.
    assert "/api/v1/workspaces" in paths
    assert "/api/v1/workspaces/{agent_id}/ssh" in paths
    assert "/api/schema" in paths
    assert not any(path.startswith("/api/v1/desktop/") for path in paths)
    assert not any(path.startswith("/api/v1/files") for path in paths)


def test_api_schema_route_matches_registered_gateway_routes(tmp_path: Path) -> None:
    client = _schema_client(tmp_path)
    app = client.application

    response = client.get("/api/schema", headers=_auth_header())
    document = json.loads(response.data)
    documented_paths = set(document["paths"].keys())

    # Every gateway-reachable rule in the live url_map is documented (no drift),
    # and nothing unreachable leaks in.
    expected_paths: set[str] = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        openapi_path = _flask_path_to_openapi(rule.rule)
        if _is_gateway_reachable_path(openapi_path):
            expected_paths.add(openapi_path)
    assert documented_paths == expected_paths


def test_published_response_models_match_the_enforced_ones(tmp_path: Path) -> None:
    """The hand-built schema's response models must equal what the handlers actually enforce.

    Each handler enforces its response by returning a pydantic model instance with
    ``@API_SPEC.validate(resp=...)``; the published doc documents responses from
    the separate ``_ROUTE_MODELS`` registry. This test ties the two together so
    the documented contract can never silently drift from the enforced one -- the
    guarantee that justifies keeping the hand-built generator rather than
    re-sourcing the doc from spectree.
    """
    client = _schema_client(tmp_path)
    app = client.application

    checked_route_count = 0
    for rule in app.url_map.iter_rules():
        openapi_path = _flask_path_to_openapi(rule.rule)
        if not _is_gateway_reachable_path(openapi_path):
            continue
        view_func = app.view_functions[rule.endpoint]
        # spectree injects the enforced response models onto the decorated view's
        # ``__dict__`` as ``resp`` (functools.wraps copies it up through the auth
        # wrapper); routes registered via a lambda wrapper (the lifecycle
        # start/stop pair) don't surface it and are covered by their own tests.
        enforced = view_func.__dict__.get("resp")
        if enforced is None:
            continue
        enforced_success_models = {
            model.__name__ for code, model in enforced.code_models.items() if code != "HTTP_422"
        }
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            documented = _ROUTE_MODELS.get((method, openapi_path))
            documented_name = (
                documented.response_model.__name__
                if documented is not None and documented.response_model is not None
                else None
            )
            assert documented_name is not None and documented_name in enforced_success_models, (
                f"{method} {openapi_path}: documented response {documented_name!r} does not match "
                f"the enforced model(s) {enforced_success_models}"
            )
            checked_route_count += 1

    # Guard against the introspection silently matching nothing (e.g. if the
    # ``resp`` attribute name changes upstream).
    assert checked_route_count >= 10


def test_api_schema_route_requires_auth(tmp_path: Path) -> None:
    client = _schema_client(tmp_path)

    response = client.get("/api/schema")

    assert response.status_code == 401
