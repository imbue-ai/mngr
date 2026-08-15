import boto3
import pytest
from botocore.stub import Stubber

from imbue.minds.envs.providers.workspace_storage import WorkspaceStorageProviderError
from imbue.minds.envs.providers.workspace_storage import _delete_prefix_with_client
from imbue.minds.envs.providers.workspace_storage import is_workspace_storage_configured
from imbue.minds.envs.testing import make_workspace_storage_vault_values

_FULL_STORAGE_VALUES = make_workspace_storage_vault_values()


def _make_stubbed_client() -> tuple[object, Stubber]:
    client = boto3.client(
        "s3",
        region_name="us-east-va",
        aws_access_key_id="fake-access-key",
        aws_secret_access_key="fake-secret-key",
    )
    return client, Stubber(client)


def test_is_workspace_storage_configured_true_with_all_keys() -> None:
    assert is_workspace_storage_configured(_FULL_STORAGE_VALUES)


def test_is_workspace_storage_configured_false_when_a_key_is_missing_or_empty() -> None:
    missing = dict(_FULL_STORAGE_VALUES)
    del missing["WORKSPACE_STORAGE_BUCKET"]
    assert not is_workspace_storage_configured(missing)

    # The Vault template ships unpopulated entries as empty exports, so an
    # empty value must read as unconfigured too.
    empty = dict(_FULL_STORAGE_VALUES)
    empty["WORKSPACE_STORAGE_S3_SECRET_KEY"] = ""
    assert not is_workspace_storage_configured(empty)


def test_delete_prefix_deletes_every_listed_object_across_pages() -> None:
    client, stubber = _make_stubbed_client()
    stubber.add_response(
        "list_objects_v2",
        {
            "IsTruncated": True,
            "NextContinuationToken": "token-1",
            "Contents": [{"Key": "dev-a/host-1/gen-1/disk.zst.age"}, {"Key": "dev-a/host-1/gen-1/meta.tar.zst.age"}],
        },
        {"Bucket": "mngr-workspaces-test", "Prefix": "dev-a/"},
    )
    stubber.add_response(
        "delete_objects",
        {},
        {
            "Bucket": "mngr-workspaces-test",
            "Delete": {
                "Objects": [
                    {"Key": "dev-a/host-1/gen-1/disk.zst.age"},
                    {"Key": "dev-a/host-1/gen-1/meta.tar.zst.age"},
                ],
                "Quiet": True,
            },
        },
    )
    stubber.add_response(
        "list_objects_v2",
        {"IsTruncated": False, "Contents": [{"Key": "dev-a/host-2/gen-3/datadisk.zst.age"}]},
        {"Bucket": "mngr-workspaces-test", "Prefix": "dev-a/", "ContinuationToken": "token-1"},
    )
    stubber.add_response(
        "delete_objects",
        {},
        {
            "Bucket": "mngr-workspaces-test",
            "Delete": {"Objects": [{"Key": "dev-a/host-2/gen-3/datadisk.zst.age"}], "Quiet": True},
        },
    )

    with stubber:
        deleted_count = _delete_prefix_with_client(client, "mngr-workspaces-test", "dev-a/")

    assert deleted_count == 3
    stubber.assert_no_pending_responses()


def test_delete_prefix_returns_zero_for_an_empty_prefix() -> None:
    client, stubber = _make_stubbed_client()
    stubber.add_response(
        "list_objects_v2",
        {"IsTruncated": False},
        {"Bucket": "mngr-workspaces-test", "Prefix": "dev-gone/"},
    )

    with stubber:
        deleted_count = _delete_prefix_with_client(client, "mngr-workspaces-test", "dev-gone/")

    assert deleted_count == 0
    stubber.assert_no_pending_responses()


def test_delete_prefix_wraps_s3_errors_in_provider_error() -> None:
    client, stubber = _make_stubbed_client()
    stubber.add_client_error("list_objects_v2", service_error_code="AccessDenied", http_status_code=403)

    with stubber:
        with pytest.raises(WorkspaceStorageProviderError, match="mngr-workspaces-test/dev-a/"):
            _delete_prefix_with_client(client, "mngr-workspaces-test", "dev-a/")


def test_delete_prefix_raises_on_per_key_delete_failures() -> None:
    # Quiet-mode delete_objects reports per-key failures in the response's
    # ``Errors`` list without raising, so the helper must surface them itself:
    # a partially-deleted prefix has to fail the destroy, not orphan storage.
    client, stubber = _make_stubbed_client()
    stubber.add_response(
        "list_objects_v2",
        {"IsTruncated": False, "Contents": [{"Key": "dev-a/host-1/gen-1/disk.zst.age"}]},
        {"Bucket": "mngr-workspaces-test", "Prefix": "dev-a/"},
    )
    stubber.add_response(
        "delete_objects",
        {"Errors": [{"Key": "dev-a/host-1/gen-1/disk.zst.age", "Code": "AccessDenied", "Message": "denied"}]},
        {
            "Bucket": "mngr-workspaces-test",
            "Delete": {"Objects": [{"Key": "dev-a/host-1/gen-1/disk.zst.age"}], "Quiet": True},
        },
    )

    with stubber:
        with pytest.raises(WorkspaceStorageProviderError, match="dev-a/host-1/gen-1/disk.zst.age"):
            _delete_prefix_with_client(client, "mngr-workspaces-test", "dev-a/")
