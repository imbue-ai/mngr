"""S3-level object operations against R2 buckets (emptying before destroy).

Cloudflare refuses to delete a non-empty bucket, so every force-destroy path
(the CLI's ``bucket destroy --force``, minds' backup reaper and quota
eviction, the CI env sweep) empties the bucket first over R2's S3-compatible
API and then calls the ordinary destroy. This module is the single shared
implementation of that emptying.
"""

import hashlib
import threading
import time
from typing import Any
from typing import Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.errors import ImbueCloudError

_S3_DELETE_BATCH_SIZE: Final[int] = 1000
# A freshly-minted or freshly-rolled Cloudflare token is not immediately
# accepted by the S3 endpoint; until it propagates every call 401s.
_CREDENTIAL_PROPAGATION_TIMEOUT_SECONDS: Final[float] = 180.0
_CREDENTIAL_PROPAGATION_POLL_SECONDS: Final[float] = 5.0


class R2ObjectDeletionError(ImbueCloudError):
    """Raised when emptying a bucket over the S3 API fails."""


@pure
def derive_s3_secret_from_token_value(token_value: str) -> str:
    """R2 derives the S3 Secret Access Key as the SHA-256 hex digest of the API token value."""
    return hashlib.sha256(token_value.encode("utf-8")).hexdigest()


def make_r2_s3_client(s3_endpoint: str, access_key_id: str, secret_access_key: str) -> Any:
    """An S3 client for R2 from bucket-key material (or a token id + derived secret)."""
    return boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def wait_for_s3_credentials(s3_client: Any, probe_bucket_name: str) -> None:
    """Block until fresh credentials are accepted by R2's S3 endpoint.

    Raises :class:`R2ObjectDeletionError` when the credentials never become
    usable within the propagation timeout.
    """
    deadline = time.monotonic() + _CREDENTIAL_PROPAGATION_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            s3_client.list_objects_v2(Bucket=probe_bucket_name, MaxKeys=1)
            return
        except (ClientError, BotoCoreError) as e:
            last_error = e
            logger.debug("Waiting for S3 credentials for {} to propagate to the R2 endpoint: {}", probe_bucket_name, e)
            threading.Event().wait(timeout=_CREDENTIAL_PROPAGATION_POLL_SECONDS)
    raise R2ObjectDeletionError(f"S3 credentials for {probe_bucket_name} never became usable: {last_error}")


def empty_bucket_via_s3(s3_client: Any, bucket_name: str) -> int:
    """Delete every object in the bucket in batches; returns the deleted count.

    Raises :class:`R2ObjectDeletionError` when listing or deleting fails.
    """
    deleted = 0
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            keys = [{"Key": entry["Key"]} for entry in page.get("Contents", [])]
            for start in range(0, len(keys), _S3_DELETE_BATCH_SIZE):
                batch = keys[start : start + _S3_DELETE_BATCH_SIZE]
                s3_client.delete_objects(Bucket=bucket_name, Delete={"Objects": batch})
                deleted += len(batch)
    except (ClientError, BotoCoreError) as e:
        raise R2ObjectDeletionError(f"Could not empty bucket {bucket_name}: {e}") from e
    return deleted


def empty_bucket_for_destroy(client: ImbueCloudConnectorClient, access_token: SecretStr, name: str) -> int:
    """Empty one of the caller's buckets client-side, ready for the ordinary destroy.

    Rolls the bucket's key for working credentials; when the account is
    storage-quota-downgraded (the rolled key is read-only), a cleanup grant
    temporarily restores write access -- deletion always reduces usage, so
    the grant machinery exists for exactly this. Returns the deleted count.
    """
    material = client.roll_bucket_key(access_token=access_token, name=name)
    if str(material.access) != "readwrite":
        # The grant updates the existing token's policy in place, so the
        # just-rolled credentials become writable without another roll.
        client.create_storage_cleanup_grant(access_token)
    s3_client = make_r2_s3_client(
        s3_endpoint=str(material.s3_endpoint),
        access_key_id=str(material.access_key_id),
        secret_access_key=material.secret_access_key.get_secret_value(),
    )
    wait_for_s3_credentials(s3_client, material.bucket_name)
    return empty_bucket_via_s3(s3_client, material.bucket_name)
