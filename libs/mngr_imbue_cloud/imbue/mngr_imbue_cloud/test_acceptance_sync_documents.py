"""Acceptance test for the account-document sync routes against a real connector.

Targets a deployed dev-tier connector whose coordinates arrive through
environment variables (the acceptance CI job carries no connector, so the
test skips there; an operator runs it by hand against their dev env):

    MNGR_IMBUE_CLOUD_ACCEPTANCE_CONNECTOR_URL   the connector base URL
    MNGR_IMBUE_CLOUD_ACCEPTANCE_EMAIL           a test account's email
    MNGR_IMBUE_CLOUD_ACCEPTANCE_PASSWORD        that account's password

The account signs in headlessly (JSON sign-in stays enabled on dev tiers).
It must be dedicated to this test: the scrub step is the connector's
clear-password flow, which strips every workspace record secret and document
payload the account holds, not just this test's document. Every run uses a
fresh document id, and the documents it leaves behind are tombstones the
connector's retention reaper purges.
"""

import base64
import os
from uuid import uuid4

import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.errors import ImbueCloudDocumentNotFoundError
from imbue.mngr_imbue_cloud.errors import ImbueCloudSyncConflictError
from imbue.mngr_imbue_cloud.wire_types import SyncDocument

_CONNECTOR_URL_ENV_VAR = "MNGR_IMBUE_CLOUD_ACCEPTANCE_CONNECTOR_URL"
_EMAIL_ENV_VAR = "MNGR_IMBUE_CLOUD_ACCEPTANCE_EMAIL"
_PASSWORD_ENV_VAR = "MNGR_IMBUE_CLOUD_ACCEPTANCE_PASSWORD"
_DOCUMENT_KIND = "acceptance_test"


def _signed_in_client() -> tuple[ImbueCloudConnectorClient, SecretStr]:
    """A connector client plus a session token for the configured test account, or skip."""
    coordinates: dict[str, str] = {}
    for env_var in (_CONNECTOR_URL_ENV_VAR, _EMAIL_ENV_VAR, _PASSWORD_ENV_VAR):
        value = os.environ.get(env_var)
        if not value:
            pytest.skip(f"{env_var} is not set; the account-document acceptance test needs a real connector")
        coordinates[env_var] = value
    client = ImbueCloudConnectorClient(base_url=AnyUrl(coordinates[_CONNECTOR_URL_ENV_VAR]))
    signin = client.auth_signin(coordinates[_EMAIL_ENV_VAR], coordinates[_PASSWORD_ENV_VAR])
    assert signin.status == "OK", f"sign-in failed: {signin.status} {signin.message}"
    assert signin.tokens is not None
    return client, SecretStr(str(signin.tokens["access_token"]))


@pytest.mark.acceptance
def test_account_documents_round_trip_through_the_real_connector() -> None:
    client, token = _signed_in_client()
    doc_id = f"acc-{uuid4().hex}"
    payload_b64 = base64.b64encode(b"opaque-acceptance-payload").decode("ascii")

    stored = client.put_sync_document(
        token,
        SyncDocument(
            kind=_DOCUMENT_KIND,
            doc_id=doc_id,
            metadata={"alias": "acceptance", "backend": "aws"},
            encrypted_payload=payload_b64,
            state="active",
            revision=1,
        ),
    )
    assert stored.revision == 1
    assert stored.encrypted_payload == payload_b64
    listed = {document.doc_id: document for document in client.list_sync_documents(token, _DOCUMENT_KIND)}
    assert listed[doc_id].metadata == {"alias": "acceptance", "backend": "aws"}

    with pytest.raises(ImbueCloudSyncConflictError) as conflict:
        client.put_sync_document(
            token,
            SyncDocument(kind=_DOCUMENT_KIND, doc_id=doc_id, metadata={}, state="active", revision=1),
        )
    assert conflict.value.stored_record is not None
    assert conflict.value.stored_record["revision"] == 1
    renamed = client.put_sync_document(
        token,
        SyncDocument(kind=_DOCUMENT_KIND, doc_id=doc_id, metadata={"alias": "renamed"}, state="active", revision=2),
    )
    assert renamed.metadata == {"alias": "renamed"}
    assert renamed.encrypted_payload == payload_b64

    client.scrub_sync_secrets(token)
    scrubbed = {document.doc_id: document for document in client.list_sync_documents(token, _DOCUMENT_KIND)}
    assert scrubbed[doc_id].encrypted_payload is None
    assert scrubbed[doc_id].metadata == {"alias": "renamed"}

    tombstone = client.delete_sync_document(token, _DOCUMENT_KIND, doc_id)
    assert tombstone.state == "deleted"
    assert tombstone.revision == 3
    assert tombstone.deleted_at
    assert client.delete_sync_document(token, _DOCUMENT_KIND, doc_id).revision == 3
    after_delete = {document.doc_id: document for document in client.list_sync_documents(token, _DOCUMENT_KIND)}
    assert after_delete[doc_id].state == "deleted"
    with pytest.raises(ImbueCloudDocumentNotFoundError):
        client.delete_sync_document(token, _DOCUMENT_KIND, f"acc-{uuid4().hex}")
