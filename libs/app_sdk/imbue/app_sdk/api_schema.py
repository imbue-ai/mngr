"""An app's self-description: the OpenAPI 3.1 document for its HTTP API, built from its Flask routes.

A workspace app that has an HTTP API serves this document at the path its
manifest names in ``api_schema_path`` (``/api/schema`` by convention), so an
agent reaching the app, from inside the workspace or over the share, learns
what the API offers without reading the app's code. The document is built from
the Flask url map on every request: each rule except Flask's static routes (the
app's and a blueprint's) and the schema route itself is a path, each method an
operation whose summary is the first paragraph of the view's docstring (as one
line) and whose description is the whole of it, and each path parameter a
required string parameter.

The app's route table says what each operation exchanges: a ``RouteModels``
entry names the pydantic models a route validates its JSON body with and
answers, which the document publishes as its request and response schemas, and
a ``SchemalessRoute`` entry says why a route carries no JSON (a page, a file, a
stream, a socket). The table is kept on the app, and ``imbue.app_sdk.routes``
checks every parse and every answer against it at request time, so the
document cannot drift from what the routes do.
"""

import inspect
import re
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from flask import Blueprint
from flask import Flask
from flask import Response
from flask import current_app
from flask import g
from flask import jsonify
from flask import request
from flask.blueprints import BlueprintSetupState
from flask.sansio.app import App
from pydantic import BaseModel
from pydantic import Field
from werkzeug.routing import Rule

from imbue.app_sdk.errors import InvalidRequestBodyError
from imbue.app_sdk.errors import RouteModelsMismatchError
from imbue.app_sdk.errors import RouteTableConflictError
from imbue.app_sdk.frozen_model import FrozenModel

OPENAPI_VERSION: Final[str] = "3.1.0"
DEFAULT_API_SCHEMA_PATH: Final[str] = "/api/schema"
DEFAULT_API_DOCUMENT_VERSION: Final[str] = "1.0"
API_SCHEMA_ENDPOINT: Final[str] = "api_schema"
# Where the app keeps its route table: ``app.extensions[EXTENSION_KEY]``. An app's tests read the table by this
# value.
EXTENSION_KEY: Final[str] = "app_schema"
_DOCUMENT_SETTINGS_KEY: Final[str] = "app_schema_document_settings"
# Set on ``flask.g`` by the helpers, so the answer check knows a modelled route went through them.
PARSED_BODY_FLAG: Final[str] = "app_schema_parsed_body"
ANSWERED_MODEL_FLAG: Final[str] = "app_schema_answered_model"
_JSON_MIMETYPE: Final[str] = "application/json"
_IMPLICIT_METHODS: Final[frozenset[str]] = frozenset({"HEAD", "OPTIONS"})
# The methods an OpenAPI path item may carry as operations; any other (WebDAV's PROPFIND, MKCOL, MOVE, COPY)
# is listed under the path item's ``x-other-methods`` extension instead.
_OPENAPI_METHODS: Final[frozenset[str]] = frozenset({"GET", "PUT", "POST", "DELETE", "PATCH", "TRACE"})
OTHER_METHODS_KEY: Final[str] = "x-other-methods"
# Marks an operation the route table declares schemaless, with the reason.
NO_SCHEMA_KEY: Final[str] = "x-no-schema"
HTTP_OK: Final[int] = 200
HTTP_BAD_REQUEST: Final[int] = 400
_FIRST_NON_SUCCESS_STATUS: Final[int] = 300
_FLASK_PARAM_PATTERN: Final[re.Pattern[str]] = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")
_PARAGRAPH_BREAK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\n[ \t]*\n")


class ErrorResponse(FrozenModel):
    """What every route answers on a refusal or a failure: one line saying what went wrong."""

    detail: str = Field(description="What went wrong, in words for the caller")


class AlternateResponse(FrozenModel):
    """Another answer a route can give: another model, another status, or both.

    Usually a second success; a failure status whose answer is a model richer
    than ``ErrorResponse`` (a relay's 502 listing each delivery) is listed the
    same way, and the document describes it as a failure.
    """

    status: int = Field(description="The HTTP status of this answer")
    model: type[BaseModel] | None = Field(
        default=None, description="The answer's model; None for an answer with no body"
    )


class RouteModels(FrozenModel):
    """The pydantic models one route validates with, published as its request and response schemas."""

    request_model: type[BaseModel] | None = Field(
        default=None,
        description="The JSON request body's model; None for a route that takes no body",
    )
    response_model: type[BaseModel] | None = Field(
        default=None,
        description="The primary success response's model; None for an answer with no body",
    )
    success_status: int = Field(default=HTTP_OK, description="The HTTP status of the primary success response")
    alternate_responses: tuple[AlternateResponse, ...] = Field(
        default=(),
        description="The other answers the route can give (a second success, or a failure whose answer is a "
        "model richer than ErrorResponse); several models under one status are published as a choice between them",
    )
    is_body_parsed_by_view: bool = Field(
        default=False,
        description="The view reads its body itself (a parser whose refusals are its own contract, or a "
        "byte-capped read of the stream); the request model then documents the body's shape without parsing it",
    )


class SchemalessRoute(FrozenModel):
    """An operation that exchanges no JSON by design (a page, a file, a stream, a socket), and why."""

    reason: str = Field(description="What the route serves instead of JSON, for the document's reader")


# Keyed by (HTTP method, OpenAPI path), the path in ``{param}`` form.
RouteTable = Mapping[tuple[str, str], RouteModels | SchemalessRoute]


class _ApiDocumentSettings(FrozenModel):
    """What the schema route publishes about the app itself, kept on the app beside its route table."""

    title: str = Field(description="The document's title: the manifest's display_name")
    description: str = Field(description="The document's description: the manifest's description")
    version: str = Field(description="The document's version")


class _RouteTableInstaller(FrozenModel):
    """Installs a route table, and the document's settings, on the app a blueprint is registered with."""

    route_table: dict[tuple[str, str], RouteModels | SchemalessRoute] = Field(description="The table to keep")
    settings: _ApiDocumentSettings = Field(description="What the schema route publishes about the app")

    def __call__(self, state: BlueprintSetupState) -> None:
        _install_on_app(state.app, self.route_table, self.settings)


def flask_rule_to_openapi_path(flask_rule: str) -> str:
    """``/x/<conv:name>`` as the OpenAPI path ``/x/{name}``."""
    return _FLASK_PARAM_PATTERN.sub(r"{\1}", flask_rule)


def merge_route_tables(*tables: RouteTable) -> dict[tuple[str, str], RouteModels | SchemalessRoute]:
    """One table from several modules' tables; a route two of them name is a conflict, never a silent overwrite."""
    merged: dict[tuple[str, str], RouteModels | SchemalessRoute] = {}
    for table in tables:
        duplicates = sorted(f"{method} {path}" for method, path in table if (method, path) in merged)
        if duplicates:
            raise RouteTableConflictError("these routes are named by two tables: " + ", ".join(duplicates))
        merged.update(table)
    return merged


def is_static_endpoint(endpoint: str) -> bool:
    """Whether ``endpoint`` is Flask's own static-file route: ``static`` on the app, ``<blueprint>.static`` on a
    blueprint."""
    return endpoint.rsplit(".", 1)[-1] == "static"


def _summary_and_description(view_func: Callable[..., object] | None) -> tuple[str | None, str | None]:
    if view_func is None or not view_func.__doc__:
        return None, None
    cleaned = inspect.cleandoc(view_func.__doc__)
    if not cleaned:
        return None, None
    # The summary is the first paragraph as one line, so a first sentence that wraps is not cut short.
    first_paragraph = _PARAGRAPH_BREAK_PATTERN.split(cleaned, maxsplit=1)[0]
    summary = " ".join(line.strip() for line in first_paragraph.splitlines())
    return summary, cleaned


def _operation_id(method: str, openapi_path: str) -> str:
    path_slug = openapi_path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{method.lower()}_{path_slug}" if path_slug else method.lower()


def _schema_reference(model: type[BaseModel]) -> dict[str, object]:
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _json_content(model: type[BaseModel]) -> dict[str, object]:
    return {"application/json": {"schema": _schema_reference(model)}}


def is_success_status(status: int) -> bool:
    return HTTP_OK <= status < _FIRST_NON_SUCCESS_STATUS


def _success_responses(models: RouteModels) -> dict[str, object]:
    """One response per listed status (described as a success or a failure by its class); a status several models
    answer offers the choice between them."""
    models_by_status: dict[int, list[type[BaseModel]]] = {models.success_status: []}
    if models.response_model is not None:
        models_by_status[models.success_status].append(models.response_model)
    for alternate in models.alternate_responses:
        answers = models_by_status.setdefault(alternate.status, [])
        if alternate.model is not None:
            answers.append(alternate.model)
    responses: dict[str, object] = {}
    for status, answers in models_by_status.items():
        response: dict[str, object] = {"description": "Success" if is_success_status(status) else "Failure"}
        if len(answers) == 1:
            response["content"] = _json_content(answers[0])
        elif answers:
            response["content"] = {
                "application/json": {"schema": {"oneOf": [_schema_reference(model) for model in answers]}}
            }
        else:
            pass
        responses[str(status)] = response
    return responses


def _build_operation(
    method: str,
    openapi_path: str,
    path_parameter_names: Sequence[str],
    view_func: Callable[..., object] | None,
    entry: RouteModels | SchemalessRoute | None,
) -> dict[str, object]:
    summary, description = _summary_and_description(view_func)
    responses: dict[str, object]
    if isinstance(entry, RouteModels):
        responses = _success_responses(entry)
    elif isinstance(entry, SchemalessRoute):
        responses = {str(HTTP_OK): {"description": entry.reason}}
    else:
        responses = {str(HTTP_OK): {"description": "Success"}}
    responses["default"] = {"description": "Error", "content": _json_content(ErrorResponse)}
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
    if isinstance(entry, RouteModels) and entry.request_model is not None:
        operation["requestBody"] = {"required": True, "content": _json_content(entry.request_model)}
    if isinstance(entry, SchemalessRoute):
        operation[NO_SCHEMA_KEY] = entry.reason
    return operation


def _path_parameter_names(arguments: set[str], openapi_path: str) -> tuple[str, ...]:
    """The rule's arguments that appear in the path; one a rule fills from ``defaults`` is not a path parameter."""
    return tuple(name for name in sorted(arguments) if f"{{{name}}}" in openapi_path)


def _models_of(entry: RouteModels) -> list[type[BaseModel]]:
    models = [model for model in (entry.request_model, entry.response_model) if model is not None]
    models.extend(alternate.model for alternate in entry.alternate_responses if alternate.model is not None)
    return models


def _component_schemas(models: Sequence[type[BaseModel]]) -> dict[str, object]:
    """The ``components/schemas`` map for ``models``, with each model's nested ``$defs`` hoisted beside it."""
    components: dict[str, object] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for nested_name, nested_schema in schema.pop("$defs", {}).items():
            components[nested_name] = nested_schema
        components[model.__name__] = schema
    return components


def build_openapi_document(
    app: Flask,
    title: str,
    description: str,
    version: str,
    route_table: RouteTable,
    excluded_endpoints: frozenset[str],
    # An OpenAPI document is deeply nested, loosely typed JSON, so the value type is Any rather than a TypedDict.
) -> dict[str, Any]:
    """The OpenAPI document for every route of ``app`` except Flask's static routes and ``excluded_endpoints``."""
    operations_by_path: dict[str, dict[str, object]] = {}
    other_methods_by_path: dict[str, list[str]] = {}
    referenced_models: list[type[BaseModel]] = [ErrorResponse]
    for rule in sorted(app.url_map.iter_rules(), key=_rule_sort_key):
        if is_static_endpoint(rule.endpoint) or rule.endpoint in excluded_endpoints:
            continue
        openapi_path = flask_rule_to_openapi_path(rule.rule)
        view_func = app.view_functions.get(rule.endpoint)
        operations = operations_by_path.setdefault(openapi_path, {})
        for method in sorted((rule.methods or frozenset()) - _IMPLICIT_METHODS):
            if method not in _OPENAPI_METHODS:
                other_methods_by_path.setdefault(openapi_path, []).append(method)
                continue
            entry = route_table.get((method, openapi_path))
            operations[method.lower()] = _build_operation(
                method,
                openapi_path,
                _path_parameter_names(rule.arguments, openapi_path),
                view_func,
                entry,
            )
            if isinstance(entry, RouteModels):
                referenced_models.extend(_models_of(entry))
    paths: dict[str, dict[str, object]] = {
        openapi_path: (
            {**operations, OTHER_METHODS_KEY: other_methods_by_path[openapi_path]}
            if openapi_path in other_methods_by_path
            else operations
        )
        for openapi_path, operations in operations_by_path.items()
    }
    return {
        "openapi": OPENAPI_VERSION,
        "info": {"title": title, "version": version, "description": description},
        "paths": paths,
        "components": {"schemas": _component_schemas(referenced_models)},
    }


def _rule_sort_key(rule: Rule) -> tuple[str, str]:
    return rule.rule, rule.endpoint


def _answer_invalid_body(error: InvalidRequestBodyError) -> Response:
    # The access line alone would not say what the caller sent wrong.
    current_app.logger.warning("%s %s refused its body: %s", request.method, request.path, error)
    response = jsonify(ErrorResponse(detail=str(error)).model_dump())
    response.status_code = HTTP_BAD_REQUEST
    g.setdefault(ANSWERED_MODEL_FLAG, True)
    return response


def current_route_key() -> tuple[str, str] | None:
    """The ``(method, path)`` the table keys the current request's route by; None when no rule matched.

    A HEAD request runs the GET view (werkzeug adds HEAD to every rule that
    allows GET), so it is looked up as the GET operation it is served by.
    """
    if request.url_rule is None:
        return None
    method = "GET" if request.method == "HEAD" else request.method
    return method, flask_rule_to_openapi_path(request.url_rule.rule)


def _check_modelled_route_answered_through_the_helpers(response: Response) -> Response:
    """A modelled route's JSON answer came from ``model_response``, and a success read its body through
    ``parse_request_body``; anything else is a route the table cannot vouch for."""
    table: RouteTable | None = current_app.extensions.get(EXTENSION_KEY)
    key = current_route_key()
    if table is None or key is None:
        return response
    entry = table.get(key)
    if not isinstance(entry, RouteModels):
        return response
    method, path = key
    if response.mimetype == _JSON_MIMETYPE and not g.get(ANSWERED_MODEL_FLAG, False):
        raise RouteModelsMismatchError(f"{method} {path} answered JSON without model_response")
    if (
        entry.request_model is not None
        and not entry.is_body_parsed_by_view
        and is_success_status(response.status_code)
        and not g.get(PARSED_BODY_FLAG, False)
    ):
        raise RouteModelsMismatchError(
            f"{method} {path} succeeded without reading its body through parse_request_body"
        )
    return response


def _install_on_app(app: App, route_table: RouteTable, settings: _ApiDocumentSettings) -> None:
    app.extensions[EXTENSION_KEY] = route_table
    app.extensions[_DOCUMENT_SETTINGS_KEY] = settings
    app.register_error_handler(InvalidRequestBodyError, _answer_invalid_body)
    app.after_request(_check_modelled_route_answered_through_the_helpers)


def _serve_api_schema() -> Response:
    """The OpenAPI 3.1 document describing this app's HTTP API."""
    settings: _ApiDocumentSettings = current_app.extensions[_DOCUMENT_SETTINGS_KEY]
    # The route's own endpoint, whatever name the blueprint carrying it was registered under.
    excluded_endpoints = frozenset({request.endpoint}) if request.endpoint is not None else frozenset()
    document = build_openapi_document(
        current_app,
        title=settings.title,
        description=settings.description,
        version=settings.version,
        route_table=current_app.extensions[EXTENSION_KEY],
        excluded_endpoints=excluded_endpoints,
    )
    return jsonify(document)


def register_api_schema_route(
    target: Flask | Blueprint,
    path: str,
    title: str,
    description: str,
    route_table: RouteTable,
    version: str = DEFAULT_API_DOCUMENT_VERSION,
) -> None:
    """Serve the app's OpenAPI document at ``path`` on ``target`` (the app, or a blueprint it registers).

    ``title`` and ``description`` are the manifest's ``display_name`` and
    ``description``, so the document says the same thing the launcher does.
    ``route_table`` says what every route exchanges; it is kept on the app, so
    a parse or an answer that disagrees with it fails at request time, as does
    a modelled route that answers JSON around ``model_response`` or succeeds
    without reading its body through ``parse_request_body``. A body that is not
    the shape its route's model requires is answered as a 400 ``ErrorResponse``.
    The document is built from the running app's url map on each request, so a
    route registered after this call is still described.
    """
    table = dict(route_table)
    settings = _ApiDocumentSettings(title=title, description=description, version=version)
    if isinstance(target, Flask):
        _install_on_app(target, table, settings)
    else:
        target.record_once(_RouteTableInstaller(route_table=table, settings=settings))
    target.add_url_rule(path, endpoint=API_SCHEMA_ENDPOINT, view_func=_serve_api_schema, methods=["GET"])
