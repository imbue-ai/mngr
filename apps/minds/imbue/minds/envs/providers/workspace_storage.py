"""Delete an env's workspace stop/start artifacts from the tier's storage bucket.

Stopped imbue_cloud workspaces live as encrypted objects in the tier's OVH
S3-compatible bucket (see docs/workspace-stop-start.md). Per-env-Modal-env
tiers (dev / ci) share their tier's bucket under a per-env key prefix
(``<env>/``, stamped into the ``storage-<env>`` Modal secret at deploy);
shared tiers own the whole bucket. ``minds env destroy`` uses this module
to reclaim exactly the keyspace the env's connector could have written,
so destroyed envs never orphan paid storage.
"""

from collections.abc import Mapping
from typing import Any
from typing import Final

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger

from imbue.minds.errors import MindError


class WorkspaceStorageProviderError(MindError):
    """Raised when an env's workspace-storage artifacts cannot be deleted."""


# The keys the ``storage`` Vault entry must populate before there is a bucket
# to clean. Mirrors the connector's ``read_storage_config`` requirements
# (minus the KEK, which deletion does not need).
_REQUIRED_STORAGE_KEYS: Final[tuple[str, ...]] = (
    "WORKSPACE_STORAGE_S3_ENDPOINT",
    "WORKSPACE_STORAGE_S3_REGION",
    "WORKSPACE_STORAGE_S3_ACCESS_KEY",
    "WORKSPACE_STORAGE_S3_SECRET_KEY",
    "WORKSPACE_STORAGE_BUCKET",
)


def is_workspace_storage_configured(storage_values: Mapping[str, str]) -> bool:
    return all(storage_values.get(key) for key in _REQUIRED_STORAGE_KEYS)


def _make_workspace_storage_client(storage_values: Mapping[str, str]) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=storage_values["WORKSPACE_STORAGE_S3_ENDPOINT"],
        region_name=storage_values["WORKSPACE_STORAGE_S3_REGION"],
        aws_access_key_id=storage_values["WORKSPACE_STORAGE_S3_ACCESS_KEY"],
        aws_secret_access_key=storage_values["WORKSPACE_STORAGE_S3_SECRET_KEY"],
        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
    )


def delete_workspace_storage_prefix(storage_values: Mapping[str, str], prefix: str) -> int:
    """Delete every object under ``prefix`` in the tier's workspace-storage bucket.

    Idempotent: a missing prefix deletes 0 objects. Raises
    ``WorkspaceStorageProviderError`` on any S3 failure so the enclosing
    destroy stays retryable.
    """
    client = _make_workspace_storage_client(storage_values)
    return _delete_prefix_with_client(client, storage_values["WORKSPACE_STORAGE_BUCKET"], prefix)


# Keep in sync with ``imbue.remote_service_connector.storage`` -- the shipped
# connector container and the shipped minds wheel cannot import each other, so
# each carries its own copy of this loop.
def _delete_prefix_with_client(client: Any, bucket: str, prefix: str) -> int:
    deleted_count = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys = [{"Key": entry["Key"]} for entry in page.get("Contents", [])]
            if not keys:
                continue
            response = client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            # Quiet mode reports per-key failures in ``Errors`` rather than
            # raising, so surface them explicitly: a partially-deleted prefix
            # must fail the destroy (keeping it retryable), not silently
            # orphan the remaining paid storage.
            errors = response.get("Errors", [])
            if errors:
                failed_keys = ", ".join(str(error.get("Key")) for error in errors[:5])
                raise WorkspaceStorageProviderError(
                    f"Failed to delete {len(errors)} workspace-storage object(s) under {bucket}/{prefix} "
                    f"(e.g. {failed_keys})"
                )
            deleted_count += len(keys)
    except (ClientError, BotoCoreError) as e:
        raise WorkspaceStorageProviderError(
            f"Failed to delete workspace-storage objects under {bucket}/{prefix}: {e}"
        ) from e
    if deleted_count:
        logger.info("Deleted {} workspace-storage object(s) under {}/{}", deleted_count, bucket, prefix)
    return deleted_count
