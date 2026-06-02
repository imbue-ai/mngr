"""Unit tests for ``_handle_dev_styleguide`` (404-in-production gating)."""

import pytest
from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse

from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.desktop_client.app import _handle_dev_styleguide


def test_dev_styleguide_returns_404_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, DEFAULT_MINDS_ROOT_NAME)
    response = _handle_dev_styleguide()
    assert isinstance(response, JSONResponse)
    assert response.status_code == 404


def test_dev_styleguide_returns_404_when_root_name_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset MINDS_ROOT_NAME falls back to the production root name (per
    bootstrap semantics), so the styleguide should 404 -- the same
    defensive default the create-form helper applies."""
    monkeypatch.delenv(MINDS_ROOT_NAME_ENV_VAR, raising=False)
    response = _handle_dev_styleguide()
    assert isinstance(response, JSONResponse)
    assert response.status_code == 404


def test_dev_styleguide_renders_in_dev_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-josh")
    response = _handle_dev_styleguide()
    assert isinstance(response, HTMLResponse)
    assert response.status_code == 200
    assert b"Minds Styleguide" in response.body


def test_dev_styleguide_renders_in_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging is treated as non-production for the styleguide so the
    catalog stays visible to operators reviewing pre-prod chrome."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-staging")
    response = _handle_dev_styleguide()
    assert isinstance(response, HTMLResponse)
    assert response.status_code == 200
