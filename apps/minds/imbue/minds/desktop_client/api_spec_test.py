"""Tests for the spectree hook that shapes /api/v1 request-validation failures."""

import json

import pytest
from flask import Flask
from pydantic import BaseModel
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from imbue.minds.desktop_client.api_spec import API_SPEC
from imbue.minds.desktop_client.api_spec import _emit_custom_validation_error


class _Body(BaseModel):
    name: str


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        _Body.model_validate({})
    return caught.value


# spectree owns this hook's signature and has grown it: 2.x calls it with four
# positional arguments, 3.0.0 appended `model_adapter` for five. It calls the
# hook on EVERY validated request, so an arity it was not written for is a 500
# on every /api/v1 endpoint rather than an edge case.
_ARITIES = pytest.mark.parametrize("extra", [(), (object(),)], ids=["spectree_2x", "spectree_3x"])


@_ARITIES
def test_the_hook_returns_quietly_when_validation_succeeded(extra: tuple[object, ...]) -> None:
    _emit_custom_validation_error(object(), None, None, None, *extra)


@_ARITIES
def test_the_hook_aborts_rather_than_raising_type_error_on_a_validation_failure(extra: tuple[object, ...]) -> None:
    # Without tolerance for the extra argument this raises TypeError instead,
    # which spectree turns into a 500.
    with pytest.raises(HTTPException):
        _emit_custom_validation_error(object(), None, _validation_error(), None, *extra)


def test_a_validated_route_answers_the_stable_422_body() -> None:
    """The contract, exercised through the spectree version actually installed."""
    app = Flask(__name__)

    @app.post("/probe")
    @API_SPEC.validate(json=_Body)
    def _probe() -> tuple[str, int]:
        return "ok", 200

    client = app.test_client()

    assert client.post("/probe", json={"name": "ada"}).status_code == 200

    rejected = client.post("/probe", json={})
    assert rejected.status_code == 422
    assert json.loads(rejected.get_data())["errors"] == [{"field": "name", "message": "Field required"}]
