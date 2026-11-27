"""How a route reads its JSON body and answers JSON, checked against the app's route table.

``parse_request_body`` validates the body with the model the table names for
the route and ``model_response`` answers the model the table names, so the
OpenAPI document and the running routes cannot disagree: a route that parses
or answers something the table does not say fails its first request with
``RouteModelsMismatchError``, a programming error, not a 400; so does a
modelled route that answers JSON around ``model_response`` or succeeds without
reading its body through ``parse_request_body``, which the check the app runs
on every answer catches. An ``ErrorResponse`` is allowed on every route, since
every operation's default response is one.
"""

from typing import TypeVar

from flask import Response
from flask import current_app
from flask import g
from flask import jsonify
from flask import request
from pydantic import BaseModel
from pydantic import ValidationError

from imbue.app_sdk.api_schema import ANSWERED_MODEL_FLAG
from imbue.app_sdk.api_schema import EXTENSION_KEY
from imbue.app_sdk.api_schema import ErrorResponse
from imbue.app_sdk.api_schema import HTTP_OK
from imbue.app_sdk.api_schema import PARSED_BODY_FLAG
from imbue.app_sdk.api_schema import RouteModels
from imbue.app_sdk.api_schema import RouteTable
from imbue.app_sdk.api_schema import current_route_key
from imbue.app_sdk.errors import InvalidRequestBodyError
from imbue.app_sdk.errors import RouteModelsMismatchError

_RequestModel = TypeVar("_RequestModel", bound=BaseModel)


def describe_validation_error(error: ValidationError) -> str:
    """One line naming the first failing field and why, for logs and a 400's detail."""
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "<root>"
    return f"field {location!r}: {first['msg']}"


def registered_route_table() -> RouteTable | None:
    """The route table ``register_api_schema_route`` kept on the current app, or None when it registered none."""
    return current_app.extensions.get(EXTENSION_KEY)


def _matched_route_key() -> tuple[str, str]:
    key = current_route_key()
    if key is None:
        raise RouteModelsMismatchError("no route matched this request")
    return key


def _route_models_or_raise() -> RouteModels | None:
    """The current route's ``RouteModels``; None when the app keeps no table; a route the table leaves out or
    declares schemaless is a mismatch."""
    table = registered_route_table()
    if table is None:
        return None
    key = _matched_route_key()
    entry = table.get(key)
    if not isinstance(entry, RouteModels):
        raise RouteModelsMismatchError(
            f"{key[0]} {key[1]} exchanges JSON but the route table "
            + ("declares it schemaless" if entry is not None else "does not list it")
        )
    return entry


def parse_request_body(model: type[_RequestModel]) -> _RequestModel:
    """The current request's body as ``model``, which the route table must name as the route's request model.

    A body that is not a JSON object or not the model's shape raises
    ``InvalidRequestBodyError``, answered as a 400 by the handler
    ``register_api_schema_route`` installed.
    """
    entry = _route_models_or_raise()
    if entry is not None and not _is_parse_named(entry, model):
        method, path = _matched_route_key()
        raise RouteModelsMismatchError(
            f"{method} {path} parses its body as {model.__name__}, but the route table "
            + _parse_the_table_names(entry)
        )
    g.setdefault(PARSED_BODY_FLAG, True)
    # force=True: a script and curl alike may post without a JSON content type.
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise InvalidRequestBodyError("the request body must be a JSON object")
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise InvalidRequestBodyError(describe_validation_error(e)) from e


def _is_answer_named(entry: RouteModels, model: type[BaseModel], status: int) -> bool:
    if entry.response_model is model and entry.success_status == status:
        return True
    return any(alternate.model is model and alternate.status == status for alternate in entry.alternate_responses)


def _is_parse_named(entry: RouteModels, model: type[BaseModel]) -> bool:
    return entry.request_model is model and not entry.is_body_parsed_by_view


def _parse_the_table_names(entry: RouteModels) -> str:
    if entry.is_body_parsed_by_view:
        return "says the view parses its body itself"
    if entry.request_model is None:
        return "names no request model"
    return f"names {entry.request_model.__name__}"


def model_response(model: BaseModel, status: int = HTTP_OK) -> Response:
    """``model`` as the JSON answer under ``status``; the route table must name that model and status for the route.

    An ``ErrorResponse`` itself needs no entry: it is every operation's default
    response. A model extending it is a richer failure and must be listed as one
    of the route's answers like any other.
    """
    if type(model) is not ErrorResponse:
        entry = _route_models_or_raise()
        if entry is not None and not _is_answer_named(entry, type(model), status):
            method, path = _matched_route_key()
            raise RouteModelsMismatchError(
                f"{method} {path} answers {type(model).__name__} under {status}, which the route table does not name"
            )
    response = jsonify(model.model_dump(mode="json"))
    response.status_code = status
    g.setdefault(ANSWERED_MODEL_FLAG, True)
    return response


def error_response(detail: str, status: int) -> Response:
    """A refusal or a failure as the ``ErrorResponse`` every route answers them with."""
    return model_response(ErrorResponse(detail=detail), status)
