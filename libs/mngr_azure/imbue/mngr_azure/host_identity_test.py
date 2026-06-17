"""Unit tests for ``BlobStateHostIdentity`` (managed identity + scoped role assignment)."""

import pytest

from imbue.mngr_azure.state_bucket import BlobStateHostIdentity
from imbue.mngr_azure.state_bucket import BlobStateHostIdentityError
from imbue.mngr_azure.state_bucket import STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID
from imbue.mngr_azure.state_bucket import host_identity_name_for_account
from imbue.mngr_azure.testing import FakeAuthorizationClient
from imbue.mngr_azure.testing import FakeManagedServiceIdentityClient
from imbue.mngr_azure.testing import _StubbedBlobStateHostIdentity

_SUBSCRIPTION = "sub-123"
_RESOURCE_GROUP = "mngr"
_REGION = "westus"
_ACCOUNT = "mngrstabc123"


def _identity(msi: FakeManagedServiceIdentityClient, authorization: FakeAuthorizationClient) -> BlobStateHostIdentity:
    return _StubbedBlobStateHostIdentity(
        credential=None,
        subscription_id=_SUBSCRIPTION,
        resource_group=_RESOURCE_GROUP,
        region=_REGION,
        account_name=_ACCOUNT,
        fake_msi_client=msi,
        fake_authorization_client=authorization,
    )


def test_identity_name_is_deterministic_from_account() -> None:
    assert host_identity_name_for_account(_ACCOUNT) == f"mngrid-{_ACCOUNT}"


def test_host_identity_exists_false_before_create() -> None:
    assert _identity(FakeManagedServiceIdentityClient(), FakeAuthorizationClient()).host_identity_exists() is False


def test_ensure_host_identity_creates_identity_and_scoped_role_assignment() -> None:
    msi = FakeManagedServiceIdentityClient()
    authorization = FakeAuthorizationClient()
    identity = _identity(msi, authorization)
    resource_id = identity.ensure_host_identity()

    assert identity.host_identity_exists() is True
    assert resource_id.endswith(f"/userAssignedIdentities/{host_identity_name_for_account(_ACCOUNT)}")
    # Exactly one role assignment was created, scoped to JUST the storage account
    # (least privilege -- never the resource group or subscription), granting the
    # built-in Storage Blob Data Contributor role.
    assert len(authorization.role_assignments.created) == 1
    scope, _name, parameters = authorization.role_assignments.created[0]
    assert scope == (
        f"/subscriptions/{_SUBSCRIPTION}/resourceGroups/{_RESOURCE_GROUP}"
        f"/providers/Microsoft.Storage/storageAccounts/{_ACCOUNT}"
    )
    assert STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID in parameters.role_definition_id


def test_ensure_host_identity_is_idempotent_on_assignment_name() -> None:
    msi = FakeManagedServiceIdentityClient()
    authorization = FakeAuthorizationClient()
    identity = _identity(msi, authorization)
    first = identity.ensure_host_identity()
    second = identity.ensure_host_identity()
    assert first == second
    # The deterministic assignment name is stable across runs (idempotent target).
    names = {name for _scope, name, _params in authorization.role_assignments.created}
    assert len(names) == 1


def test_get_host_identity_client_id_returns_none_when_absent() -> None:
    assert (
        _identity(FakeManagedServiceIdentityClient(), FakeAuthorizationClient()).get_host_identity_client_id() is None
    )


def test_get_host_identity_client_id_returns_client_id_after_create() -> None:
    msi = FakeManagedServiceIdentityClient()
    identity = _identity(msi, FakeAuthorizationClient())
    identity.ensure_host_identity()
    assert identity.get_host_identity_client_id() == msi.user_assigned_identities.client_id


def test_delete_host_identity_removes_it_and_is_idempotent() -> None:
    msi = FakeManagedServiceIdentityClient()
    authorization = FakeAuthorizationClient()
    identity = _identity(msi, authorization)
    identity.ensure_host_identity()
    created_assignment = authorization.role_assignments.created[0]
    identity.delete_host_identity()
    assert identity.host_identity_exists() is False
    # The scoped role assignment is deleted explicitly (not left for Azure's
    # stale-principal reaping), targeting the same scope + deterministic name.
    assert authorization.role_assignments.deleted == [(created_assignment[0], created_assignment[1])]
    # A second delete is a no-op (missing identity tolerated).
    identity.delete_host_identity()


def test_ensure_host_identity_raises_when_principal_missing() -> None:
    msi = FakeManagedServiceIdentityClient()
    msi.user_assigned_identities.principal_id = ""
    identity = _identity(msi, FakeAuthorizationClient())
    with pytest.raises(BlobStateHostIdentityError):
        identity.ensure_host_identity()
