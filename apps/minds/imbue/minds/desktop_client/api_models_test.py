import pytest
from pydantic import ValidationError

from imbue.minds.desktop_client.api_models import AwsCloudAccountCreateRequest
from imbue.minds.desktop_client.api_models import AzureCloudAccountCreateRequest
from imbue.minds.desktop_client.api_models import BugReportRequest
from imbue.minds.desktop_client.api_models import CloudAccountCreateRequest
from imbue.minds.desktop_client.api_models import CreateWorkspaceRequest
from imbue.minds.desktop_client.api_models import GcpCloudAccountCreateRequest
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


def test_cloud_account_create_discriminates_on_backend() -> None:
    # The flat body is routed to the right per-backend variant by ``backend``,
    # and another backend's fields (sent by mistake) are ignored, not rejected
    # (semantic per-backend requiredness stays in the handler).
    aws = CloudAccountCreateRequest.model_validate(
        {"backend": "aws", "alias": "mine", "region": "us-east-1", "aws_access_key_id": "AKIA", "aws_secret_access_key": "s"}
    )
    assert isinstance(aws.root, AwsCloudAccountCreateRequest)
    assert aws.root.aws_access_key_id == "AKIA"

    gcp = CloudAccountCreateRequest.model_validate(
        {"backend": "gcp", "alias": "g", "region": "us-central1-a", "gcp_service_account_key_json": "{}"}
    )
    assert isinstance(gcp.root, GcpCloudAccountCreateRequest)

    azure = CloudAccountCreateRequest.model_validate(
        {
            "backend": "azure",
            "alias": "z",
            "region": "eastus",
            "azure_subscription_id": "sub",
            "azure_tenant_id": "t",
            "azure_client_id": "c",
            "azure_client_secret": "sec",
        }
    )
    assert isinstance(azure.root, AzureCloudAccountCreateRequest)

    # An aws body carrying a stray gcp field validates as aws (extra ignored).
    mixed = CloudAccountCreateRequest.model_validate(
        {"backend": "aws", "alias": "x", "region": "us-east-1", "gcp_service_account_key_json": "{}"}
    )
    assert isinstance(mixed.root, AwsCloudAccountCreateRequest)


def test_cloud_account_create_rejects_unknown_backend_and_missing_shared_fields() -> None:
    with pytest.raises(ValidationError):
        CloudAccountCreateRequest.model_validate({"backend": "digitalocean", "alias": "x", "region": "r"})
    # ``alias``/``region`` are shared-and-required on every variant.
    with pytest.raises(ValidationError):
        CloudAccountCreateRequest.model_validate({"backend": "aws", "region": "us-east-1"})


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
