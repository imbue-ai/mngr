import pytest
from pydantic import ValidationError

from imbue.minds.desktop_client.api_models import BugReportRequest
from imbue.minds.desktop_client.api_models import CreateWorkspaceRequest
from imbue.minds.desktop_client.api_models import SetProviderEnabledRequest
from imbue.minds.desktop_client.api_models import WorkspaceSummary


def test_request_models_ignore_extra_keys() -> None:
    # Request bodies must ignore unknown keys: handlers forward the raw body
    # (with extras) onward, so rejecting extras would 422 previously-valid calls.
    model = BugReportRequest.model_validate({"description": "boom", "stack": "trace", "extra": 1})
    assert model.description == "boom"


def test_response_models_forbid_extra_keys() -> None:
    # Response/doc models stay strict so a drifted field is caught.
    with pytest.raises(ValidationError):
        WorkspaceSummary.model_validate({"agent_id": "a", "not_a_field": 1})


def test_set_provider_enabled_rejects_non_bool() -> None:
    # StrictBool: a truthy int/str must not be coerced (matches the prior
    # isinstance(enabled, bool) check on the route).
    assert SetProviderEnabledRequest.model_validate({"enabled": True}).enabled is True
    for bad_value in (1, "yes"):
        with pytest.raises(ValidationError):
            SetProviderEnabledRequest.model_validate({"enabled": bad_value})


def test_bug_report_requires_nonempty_description() -> None:
    with pytest.raises(ValidationError):
        BugReportRequest.model_validate({"description": ""})


def test_create_workspace_requires_git_url() -> None:
    with pytest.raises(ValidationError):
        CreateWorkspaceRequest.model_validate({})
    # All other create fields are optional (the form may omit the advanced ones).
    assert CreateWorkspaceRequest.model_validate({"git_url": "https://example/repo"}).git_url == "https://example/repo"
