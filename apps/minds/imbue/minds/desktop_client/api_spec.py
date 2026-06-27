"""The shared spectree instance that validates ``/api/v1`` requests.

``@API_SPEC.validate(json=..., query=..., resp=...)`` on a handler validates the
request against pydantic models before the handler runs (and, when ``resp`` is
given, the response after). Validation runs per-call without ``register()``, so
no spectree documentation endpoints are exposed; the published OpenAPI is served
separately by :mod:`api_schema`.

On a request-validation failure the :func:`_emit_custom_validation_error` hook
short-circuits with the project's stable error contract -- HTTP 422 with
``{"errors": [{"field": "<dotted pydantic loc>", "message": "..."}]}`` -- instead
of spectree's bare default body, so every consumer parses one uniform shape.
"""

import json
from typing import Any

from flask import abort
from pydantic import BaseModel
from spectree import Response as SpecResponse
from spectree import SpecTree

from imbue.minds.desktop_client.responses import make_response


def json_response_model(model: type[BaseModel], *, status_code: int = 200) -> SpecResponse:
    """Build a spectree ``Response`` declaring ``model`` as the body for ``status_code``.

    Handlers that return the model *instance* (not a hand-built ``Response``) get
    it validated + serialized by spectree, so the documented and the enforced
    response contract are the same object. Error responses on other status codes
    are returned by the handler directly and pass through unvalidated.
    """
    return SpecResponse(**{f"HTTP_{status_code}": model})


def _emit_custom_validation_error(
    req: Any,
    resp: Any,
    req_validation_error: Any,
    instance: Any,
) -> None:
    """spectree ``before`` hook: turn a request-validation failure into the stable 422 body."""
    if req_validation_error:
        errors = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "message": str(error.get("msg", "")),
            }
            for error in req_validation_error.errors()
        ]
        abort(make_response(content=json.dumps({"errors": errors}), media_type="application/json", status_code=422))


API_SPEC: SpecTree = SpecTree(
    "flask",
    before=_emit_custom_validation_error,
    validation_error_status=422,
    title="Minds desktop client API",
    version="1.0",
    openapi_version="3.1.0",
)
