"""Serve a self-describing OpenAPI document for the gateway-reachable Minds API.

``GET /api/schema`` returns an OpenAPI 3.1 document describing every Minds API
route an agent can reach through the latchkey ``minds-api-proxy`` gateway -- i.e.
everything under ``/api/v*`` *except* the cookie-only ``/desktop`` namespace and
the WebDAV ``/files`` mount, which agents cannot reach (so listing them would
only confuse a caller discovering the API). The document is generated at request
time from the live Flask ``url_map``, so the path/method inventory can never
drift from the routes that are actually registered.

Request/response *body* schemas are layered on top: a small registry
(:data:`_ROUTE_MODELS`) maps a route to the pydantic models describing its body
and primary response, and their JSON Schema is hoisted into
``components/schemas``. Routes without a registered model are still listed (with
their summary, path parameters, and security), just without a body schema. The
models are documentation-only -- the handlers still validate requests
themselves -- so keep them in sync when a route's contract changes; the schema
test asserts the path inventory matches the routes, which catches new routes.

Why a hand-built generator rather than a framework like spectree/APIFlask: those
couple schema declaration to *runtime request validation*, which would replace
each handler's bespoke error contract (e.g. the create form's inline
``{error, field}`` and the imbue_cloud sign-up ``redirect_url`` backstop) with
the framework's own 4xx body. Generating the document from pydantic directly
keeps the established contracts untouched and adds no runtime behavior, while
still being fully pydantic-native (the repo's "validation only through pydantic"
rule).
"""

import inspect
import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from flask import Blueprint
from flask import Response
from flask import current_app
from pydantic import BaseModel
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.api_auth import require_api_or_cookie_auth
from imbue.minds.desktop_client.api_models import AgentNotificationRequest
from imbue.minds.desktop_client.api_models import ApiErrorResponse
from imbue.minds.desktop_client.api_models import BugReportRequest
from imbue.minds.desktop_client.api_models import BugReportResponse
from imbue.minds.desktop_client.api_models import CreateOperationStatusResponse
from imbue.minds.desktop_client.api_models import CreateWorkspaceRequest
from imbue.minds.desktop_client.api_models import DestroyOperationStatusResponse
from imbue.minds.desktop_client.api_models import EmptyResponse
from imbue.minds.desktop_client.api_models import EnableSharingRequest
from imbue.minds.desktop_client.api_models import EstablishSshRequest
from imbue.minds.desktop_client.api_models import OkResponse
from imbue.minds.desktop_client.api_models import OperationHandleResponse
from imbue.minds.desktop_client.api_models import PatchWorkspaceRequest
from imbue.minds.desktop_client.api_models import RestartOperationStatusResponse
from imbue.minds.desktop_client.api_models import RestartWorkspaceRequest
from imbue.minds.desktop_client.api_models import SharingReadinessResponse
from imbue.minds.desktop_client.api_models import SharingToggleResponse
from imbue.minds.desktop_client.api_models import SshConnectionResponse
from imbue.minds.desktop_client.api_models import WorkspaceBackupsResponse
from imbue.minds.desktop_client.api_models import WorkspaceLifecycleResponse
from imbue.minds.desktop_client.api_models import WorkspaceListResponse
from imbue.minds.desktop_client.api_models import WorkspaceSummary
from imbue.minds.desktop_client.api_models import WorkspaceVersionResponse
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.responses import make_response

# The path the schema document is served at (version-agnostic: it describes every
# ``/api/v*`` version). Must match the gateway baseline grant in
# ``mngr_latchkey.agent_setup`` (the proxy forwards ``/minds-api-proxy/api/schema``
# here).
API_SCHEMA_PATH: Final[str] = "/api/schema"

_OPENAPI_VERSION: Final[str] = "3.1.0"
_API_TITLE: Final[str] = "Minds desktop client API"
_API_VERSION: Final[str] = "1.0"

# HTTP methods Flask adds automatically that are not part of the documented API.
_IMPLICIT_METHODS: Final[frozenset[str]] = frozenset({"HEAD", "OPTIONS"})

# Path prefixes that are NOT reachable by an agent through the gateway proxy and
# so are excluded from the published schema: the cookie-only desktop namespace
# and the WebDAV file mount. Everything else under ``/api/`` is proxy-reachable
# (grantable via a ``minds-workspaces`` verb, or baseline-allowed), matching the
# contract documented in ``api_v1``.
_GATEWAY_UNREACHABLE_PREFIXES: Final[tuple[str, ...]] = ("/api/v1/desktop/", "/api/v1/files")


# -- Route -> model registry (models live in ``api_models``) --


class _RouteModels(FrozenModel):
    """The documentation models for a single route + its success status code."""

    request_model: type[BaseModel] | None = Field(default=None, description="Request body model, if any")
    response_model: type[BaseModel] | None = Field(default=None, description="Primary success response model, if any")
    success_status: int = Field(default=200, description="HTTP status of the primary success response")


# Maps (HTTP method, OpenAPI path) -> the body/response models to publish for it.
# Keys use the OpenAPI ``{param}`` path form (see ``_flask_path_to_openapi``).
# Routes absent here are still listed, just without body schemas.
_ROUTE_MODELS: Final[Mapping[tuple[str, str], _RouteModels]] = {
    ("POST", "/api/v1/agents/{agent_id}/notifications"): _RouteModels(
        request_model=AgentNotificationRequest, response_model=OkResponse
    ),
    ("POST", "/api/v1/agents/{agent_id}/report"): _RouteModels(
        request_model=BugReportRequest, response_model=BugReportResponse
    ),
    ("GET", "/api/v1/workspaces"): _RouteModels(response_model=WorkspaceListResponse),
    ("POST", "/api/v1/workspaces"): _RouteModels(
        request_model=CreateWorkspaceRequest, response_model=OperationHandleResponse, success_status=202
    ),
    ("GET", "/api/v1/workspaces/{agent_id}"): _RouteModels(response_model=WorkspaceSummary),
    ("PATCH", "/api/v1/workspaces/{agent_id}"): _RouteModels(request_model=PatchWorkspaceRequest),
    ("GET", "/api/v1/workspaces/{agent_id}/version"): _RouteModels(response_model=WorkspaceVersionResponse),
    ("GET", "/api/v1/workspaces/{agent_id}/backups"): _RouteModels(response_model=WorkspaceBackupsResponse),
    ("POST", "/api/v1/workspaces/{agent_id}/destroy"): _RouteModels(
        response_model=OperationHandleResponse, success_status=202
    ),
    ("POST", "/api/v1/workspaces/{agent_id}/restart"): _RouteModels(
        request_model=RestartWorkspaceRequest, response_model=OperationHandleResponse, success_status=202
    ),
    ("POST", "/api/v1/workspaces/{agent_id}/start"): _RouteModels(response_model=WorkspaceLifecycleResponse),
    ("POST", "/api/v1/workspaces/{agent_id}/stop"): _RouteModels(response_model=WorkspaceLifecycleResponse),
    ("GET", "/api/v1/workspaces/operations/create/{operation_id}"): _RouteModels(
        response_model=CreateOperationStatusResponse
    ),
    ("GET", "/api/v1/workspaces/operations/destroy/{operation_id}"): _RouteModels(
        response_model=DestroyOperationStatusResponse
    ),
    ("GET", "/api/v1/workspaces/operations/restart/{operation_id}"): _RouteModels(
        response_model=RestartOperationStatusResponse
    ),
    ("DELETE", "/api/v1/workspaces/operations/destroy/{operation_id}"): _RouteModels(response_model=EmptyResponse),
    ("POST", "/api/v1/workspaces/{agent_id}/ssh"): _RouteModels(
        request_model=EstablishSshRequest, response_model=SshConnectionResponse
    ),
    ("PATCH", "/api/v1/workspaces/{agent_id}/sharing/{service_name}"): _RouteModels(),
    ("PUT", "/api/v1/workspaces/{agent_id}/sharing/{service_name}"): _RouteModels(
        request_model=EnableSharingRequest, response_model=SharingToggleResponse
    ),
    ("DELETE", "/api/v1/workspaces/{agent_id}/sharing/{service_name}"): _RouteModels(
        response_model=SharingToggleResponse
    ),
    ("GET", "/api/v1/workspaces/{agent_id}/sharing/{service_name}/readiness"): _RouteModels(
        response_model=SharingReadinessResponse
    ),
}

# Always published so the ``default`` error response on every operation can $ref it.
_ALWAYS_INCLUDED_MODELS: Final[tuple[type[BaseModel], ...]] = (ApiErrorResponse,)

_SECURITY_SCHEMES: Final[Mapping[str, object]] = {
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "description": "Central minds API key, injected by the latchkey gateway's minds-api-proxy. Agents never hold it directly.",
    },
    "cookieAuth": {
        "type": "apiKey",
        "in": "cookie",
        "name": SESSION_COOKIE_NAME,
        "description": "Desktop-client signed session cookie (used by the browser UI).",
    },
}

_FLASK_PARAM_RE: Final[re.Pattern[str]] = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


def _flask_path_to_openapi(flask_rule: str) -> str:
    """Convert a Flask rule (``/x/<conv:name>``) to an OpenAPI path (``/x/{name}``)."""
    return _FLASK_PARAM_RE.sub(r"{\1}", flask_rule)


def _is_gateway_reachable_path(openapi_path: str) -> bool:
    """Whether a path is reachable by an agent through the minds-api-proxy gateway.

    Everything under ``/api/`` is proxy-reachable except the cookie-only desktop
    namespace and the WebDAV files mount.
    """
    if not openapi_path.startswith("/api/"):
        return False
    return not any(openapi_path.startswith(prefix) for prefix in _GATEWAY_UNREACHABLE_PREFIXES)


def _component_schemas_from_models(models: Sequence[type[BaseModel]]) -> dict[str, object]:
    """Build the ``components/schemas`` map from pydantic models, hoisting nested ``$defs``."""
    components: dict[str, object] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        nested_defs = schema.pop("$defs", {})
        for nested_name, nested_schema in nested_defs.items():
            components[nested_name] = nested_schema
        components[model.__name__] = schema
    return components


def _docstring_summary_and_description(view_func: Callable[..., object] | None) -> tuple[str | None, str | None]:
    """Return (summary, description) from a view function's docstring, if present."""
    if view_func is None:
        return None, None
    doc = view_func.__doc__
    if not doc:
        return None, None
    # cleandoc dedents the body the way Python docstring tooling does, so the
    # published description doesn't carry the source's indentation.
    cleaned = inspect.cleandoc(doc)
    summary = cleaned.splitlines()[0].strip()
    return summary, cleaned


def _operation_id(method: str, openapi_path: str) -> str:
    """A stable operationId from the method + path (param braces stripped)."""
    path_slug = openapi_path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{method.lower()}_{path_slug}"


def _build_operation(
    *,
    method: str,
    openapi_path: str,
    path_parameter_names: Sequence[str],
    view_func: Callable[..., object] | None,
) -> dict[str, object]:
    """Build one OpenAPI operation object for a (method, path)."""
    summary, description = _docstring_summary_and_description(view_func)
    models = _ROUTE_MODELS.get((method, openapi_path))
    success_status = str(models.success_status) if models is not None else "200"

    # Build the responses block first (every operation gets a ``default`` error
    # response plus its primary success response) so we never have to read a
    # value back out of the loosely-typed operation dict.
    responses: dict[str, object] = {
        "default": {
            "description": "Error",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ApiErrorResponse"}}},
        }
    }
    if models is not None and models.response_model is not None:
        responses[success_status] = {
            "description": "Success",
            "content": {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{models.response_model.__name__}"}}
            },
        }
    else:
        responses[success_status] = {"description": "Success"}

    operation: dict[str, object] = {"operationId": _operation_id(method, openapi_path), "responses": responses}
    if summary is not None:
        operation["summary"] = summary
    if description is not None:
        operation["description"] = description
    if path_parameter_names:
        operation["parameters"] = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in path_parameter_names
        ]
    if models is not None and models.request_model is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{models.request_model.__name__}"}}
            },
        }
    return operation


def build_openapi_document(
    url_rules: Sequence[tuple[str, frozenset[str], Sequence[str], str]],
    view_func_by_endpoint: Mapping[str, Callable[..., object]],
    # An OpenAPI 3.1 document: deeply-nested, loosely-typed JSON, so the value type
    # is ``Any`` rather than a giant TypedDict.
) -> dict[str, Any]:
    """Build the OpenAPI 3.1 document for the gateway-reachable API.

    ``url_rules`` is a sequence of ``(flask_rule, methods, argument_names,
    endpoint)`` extracted from the Flask ``url_map`` -- passed in (rather than
    reaching for ``current_app``) so this stays a pure function that is trivial
    to unit-test.
    """
    paths: dict[str, dict[str, object]] = {}
    referenced_models: list[type[BaseModel]] = list(_ALWAYS_INCLUDED_MODELS)

    for flask_rule, methods, argument_names, endpoint in url_rules:
        if endpoint == "static":
            continue
        openapi_path = _flask_path_to_openapi(flask_rule)
        if not _is_gateway_reachable_path(openapi_path):
            continue
        view_func = view_func_by_endpoint.get(endpoint)
        for method in sorted(methods - _IMPLICIT_METHODS):
            operation = _build_operation(
                method=method,
                openapi_path=openapi_path,
                path_parameter_names=tuple(argument_names),
                view_func=view_func,
            )
            paths.setdefault(openapi_path, {})[method.lower()] = operation
            models = _ROUTE_MODELS.get((method, openapi_path))
            if models is not None:
                if models.request_model is not None:
                    referenced_models.append(models.request_model)
                if models.response_model is not None:
                    referenced_models.append(models.response_model)

    return {
        "openapi": _OPENAPI_VERSION,
        "info": {
            "title": _API_TITLE,
            "version": _API_VERSION,
            "description": (
                "The Minds desktop client REST API reachable by in-workspace agents through the "
                "latchkey minds-api-proxy gateway. Every route accepts either the gateway-injected "
                "bearer key or the desktop session cookie."
            ),
        },
        "security": [{"bearerAuth": []}, {"cookieAuth": []}],
        "components": {
            "securitySchemes": dict(_SECURITY_SCHEMES),
            "schemas": _component_schemas_from_models(referenced_models),
        },
        "paths": paths,
    }


def _extract_url_rules() -> list[tuple[str, frozenset[str], Sequence[str], str]]:
    """Snapshot the live Flask url_map into the tuples ``build_openapi_document`` consumes."""
    rules: list[tuple[str, frozenset[str], Sequence[str], str]] = []
    for rule in current_app.url_map.iter_rules():
        methods = frozenset(rule.methods or frozenset())
        rules.append((rule.rule, methods, tuple(sorted(rule.arguments)), rule.endpoint))
    return rules


@require_api_or_cookie_auth
def _handle_api_schema() -> Response:
    """Return the OpenAPI 3.1 document for the gateway-reachable Minds API."""
    document = build_openapi_document(_extract_url_rules(), dict(current_app.view_functions))
    return make_response(content=json.dumps(document), media_type="application/json")


def create_api_schema_blueprint() -> Blueprint:
    """Create the blueprint serving ``GET /api/schema``."""
    blueprint = Blueprint("api_schema", __name__)
    blueprint.add_url_rule(API_SCHEMA_PATH, view_func=_handle_api_schema, methods=["GET"])
    return blueprint
