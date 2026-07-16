"""Sweep R2 buckets whose owning account no longer exists.

Imbue-cloud backups provision one R2 bucket per workspace, named
``<user_id_prefix>--<host_id>`` (see the connector's ``make_bucket_name``).
Nothing ever deleted them: ``minds env destroy`` tears down the Modal env,
Neon DB, SuperTokens app and Cloudflare *tunnels*, but not the buckets a CI
env's test accounts created. Every CI run that exercises imbue-cloud backups
therefore leaked a bucket, forever.

Identifying a leaked bucket safely is the whole problem: **the dev and CI
tiers share one Cloudflare account**, so a developer's own backup buckets sit
right next to the CI leftovers, and deleting by age or by name pattern would
eat a colleague's backups. The rule used here is *positive* rather than
heuristic:

    a bucket is sweepable only when its owner prefix matches NO user in ANY
    SuperTokens app on the core those tiers authenticate against.

CI test accounts live in a per-run ``ci-*`` SuperTokens app that is destroyed
with the env, so their buckets become provably ownerless. A developer's user
persists in their ``dev-*`` app, so their buckets are never candidates. The
sweep fails closed: any error enumerating apps/users, or an empty protected
set, aborts before deleting anything (a transient SuperTokens outage must
never be read as "nobody owns these buckets").

Cloudflare refuses to delete a non-empty bucket, so each bucket is emptied
first over R2's S3-compatible API, using S3 credentials derived from the
account token exactly as the connector does (access key = token id, secret =
SHA-256 of the token value).
"""

import hashlib
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.errors import MindError

_CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
_HTTP_TIMEOUT_SECONDS = 60.0
_S3_DELETE_BATCH_SIZE = 1000
# Buckets younger than this are left alone: an in-flight CI run's workspace
# has a live account, but the app-list read could race its creation.
_MIN_BUCKET_AGE_HOURS = 2
# Buckets that are infrastructure, not per-user backups (they have no owner
# prefix and must never be swept).
_PROTECTED_BUCKET_PREFIXES: tuple[str, ...] = ("minds-lima-images",)
# The connector builds bucket names as ``<owner-prefix>--<slug>``.
_BUCKET_NAME_SEPARATOR = "--"
# The account-token permission group that grants R2 object writes (deletes).
_R2_WRITE_PERMISSION_GROUP_NAME = "Workers R2 Storage Bucket Item Write"
_SWEEP_TOKEN_NAME = "minds-ci-r2-sweep"
# A freshly-minted Cloudflare token is not immediately accepted by the S3
# endpoint (the same propagation lag restic_cli retries around).
_TOKEN_PROPAGATION_TIMEOUT_SECONDS = 180.0
_TOKEN_PROPAGATION_POLL_SECONDS = 5.0


class R2CleanupError(MindError):
    """Raised when the R2 bucket sweep cannot run safely (it then deletes nothing)."""


class R2Bucket(FrozenModel):
    """One R2 bucket as Cloudflare reports it."""

    name: str = Field(description="Full bucket name (<owner_prefix>--<slug>)")
    created_at: datetime = Field(description="Bucket creation time, from the Cloudflare listing")

    @property
    def owner_prefix(self) -> str:
        """The bucket's owner segment, or '' for a bucket that has no owner prefix."""
        if _BUCKET_NAME_SEPARATOR not in self.name:
            return ""
        return self.name.split(_BUCKET_NAME_SEPARATOR, 1)[0]


class CloudflareR2Credentials(FrozenModel):
    """What the sweep needs to enumerate, empty, and delete buckets."""

    account_id: str = Field(description="Cloudflare account holding the R2 buckets")
    api_token: SecretStr = Field(description="Token with Workers R2 Storage: Edit on that account")


class SuperTokensCoreCredentials(FrozenModel):
    """The core whose apps own the accounts that create buckets in this Cloudflare account."""

    connection_uri: str = Field(description="SuperTokens core base URI")
    api_key: SecretStr = Field(description="Core admin api-key")


def _cloudflare_get(credentials: CloudflareR2Credentials, path: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {credentials.api_token.get_secret_value()}"}
    try:
        response = httpx.get(f"{_CLOUDFLARE_API_BASE}{path}", headers=headers, timeout=_HTTP_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise R2CleanupError(f"Cloudflare GET {path} failed: {e}") from e
    return _checked_cloudflare_body(response, f"GET {path}")


def _checked_cloudflare_body(response: httpx.Response, description: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as e:
        raise R2CleanupError(f"Cloudflare {description} returned non-JSON: {response.text[:200]}") from e
    if not isinstance(body, dict) or not body.get("success"):
        raise R2CleanupError(f"Cloudflare {description} failed: {response.text[:300]}")
    return body


def list_r2_buckets(credentials: CloudflareR2Credentials) -> tuple[R2Bucket, ...]:
    """List every R2 bucket in the account."""
    body = _cloudflare_get(credentials, f"/accounts/{credentials.account_id}/r2/buckets?per_page=1000")
    result = body.get("result")
    raw_buckets = result.get("buckets", []) if isinstance(result, dict) else result
    if not isinstance(raw_buckets, list):
        raise R2CleanupError(f"Cloudflare bucket listing had an unexpected shape: {str(result)[:200]}")
    buckets: list[R2Bucket] = []
    for entry in raw_buckets:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        raw_created = str(entry.get("creation_date", ""))
        if not name or not raw_created:
            raise R2CleanupError(f"Cloudflare bucket listing entry is missing name/creation_date: {entry}")
        buckets.append(R2Bucket(name=name, created_at=_parse_cloudflare_timestamp(raw_created)))
    return tuple(buckets)


def _parse_cloudflare_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise R2CleanupError(f"Could not parse a Cloudflare timestamp: {value!r}") from e


def _supertokens_get(credentials: SuperTokensCoreCredentials, path: str) -> dict[str, Any]:
    headers = {"api-key": credentials.api_key.get_secret_value()}
    uri = credentials.connection_uri.rstrip("/")
    try:
        response = httpx.get(f"{uri}{path}", headers=headers, timeout=_HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise R2CleanupError(f"SuperTokens GET {path} failed: {e}") from e
    if not isinstance(body, dict):
        raise R2CleanupError(f"SuperTokens GET {path} returned an unexpected shape")
    return body


def collect_live_owner_prefixes(credentials: SuperTokensCoreCredentials) -> frozenset[str]:
    """Return the bucket-owner prefix of every user in every app on the core.

    These are the accounts that still exist, so their buckets are never
    sweepable. Raises :class:`R2CleanupError` rather than returning a partial
    set: an under-reported protected set would authorize deleting a live
    user's backups.
    """
    apps_body = _supertokens_get(credentials, "/recipe/multitenancy/app/list")
    raw_apps = apps_body.get("apps")
    if not isinstance(raw_apps, list) or not raw_apps:
        raise R2CleanupError("SuperTokens reported no apps; refusing to treat every bucket as ownerless")
    prefixes: set[str] = set()
    for entry in raw_apps:
        app_id = str(entry.get("appId", "")) if isinstance(entry, dict) else ""
        if not app_id:
            raise R2CleanupError(f"SuperTokens app list entry has no appId: {entry}")
        base = "" if app_id == "public" else f"/appid-{app_id}"
        users_body = _supertokens_get(credentials, f"{base}/public/users?limit=500")
        raw_users = users_body.get("users")
        if not isinstance(raw_users, list):
            raise R2CleanupError(f"SuperTokens user list for app {app_id!r} had an unexpected shape")
        for raw_user in raw_users:
            user = raw_user.get("user", raw_user) if isinstance(raw_user, dict) else {}
            user_id = str(user.get("id", "")) if isinstance(user, dict) else ""
            if not user_id:
                raise R2CleanupError(f"SuperTokens user in app {app_id!r} has no id")
            prefixes.add(bucket_owner_prefix_for_user(user_id))
    return frozenset(prefixes)


def bucket_owner_prefix_for_user(user_id: str) -> str:
    """Derive a user's bucket-owner prefix exactly as the connector does.

    The connector authenticates a SuperTokens JWT into an ``AdminAuth`` whose
    ``username`` is the hyphen-stripped first 16 characters of the user id,
    and names buckets ``<username>--<slug>``.
    """
    return user_id.replace("-", "")[:16]


def find_sweepable_buckets(
    buckets: Sequence[R2Bucket],
    live_owner_prefixes: frozenset[str],
    now: datetime,
    min_age_hours: int = _MIN_BUCKET_AGE_HOURS,
) -> tuple[R2Bucket, ...]:
    """Return the buckets whose owning account no longer exists.

    A bucket is swept only when every one of these holds: it carries an owner
    prefix (so it is a per-user backup bucket, not infrastructure), that
    prefix belongs to no live user, and it is older than ``min_age_hours``
    (so an in-flight run's fresh bucket is never yanked out from under it).
    """
    if not live_owner_prefixes:
        raise R2CleanupError("The live-owner set is empty; refusing to sweep (every bucket would look ownerless)")
    cutoff = now - timedelta(hours=min_age_hours)
    sweepable: list[R2Bucket] = []
    for bucket in buckets:
        if any(bucket.name.startswith(prefix) for prefix in _PROTECTED_BUCKET_PREFIXES):
            continue
        owner = bucket.owner_prefix
        if not owner or owner in live_owner_prefixes:
            continue
        if bucket.created_at > cutoff:
            logger.info(
                "Leaving bucket {} alone: created {} (younger than {}h)", bucket.name, bucket.created_at, min_age_hours
            )
            continue
        sweepable.append(bucket)
    return tuple(sweepable)


def _cloudflare_post(credentials: CloudflareR2Credentials, path: str, body: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {credentials.api_token.get_secret_value()}"}
    try:
        response = httpx.post(
            f"{_CLOUDFLARE_API_BASE}{path}", headers=headers, json=body, timeout=_HTTP_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as e:
        raise R2CleanupError(f"Cloudflare POST {path} failed: {e}") from e
    return _checked_cloudflare_body(response, f"POST {path}")


def _cloudflare_delete(credentials: CloudflareR2Credentials, path: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {credentials.api_token.get_secret_value()}"}
    try:
        response = httpx.delete(f"{_CLOUDFLARE_API_BASE}{path}", headers=headers, timeout=_HTTP_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise R2CleanupError(f"Cloudflare DELETE {path} failed: {e}") from e
    return _checked_cloudflare_body(response, f"DELETE {path}")


def _r2_write_permission_group_id(credentials: CloudflareR2Credentials) -> str:
    """Resolve the account-token permission group that grants R2 object writes."""
    body = _cloudflare_get(credentials, f"/accounts/{credentials.account_id}/tokens/permission_groups")
    for group in body.get("result", []):
        if isinstance(group, dict) and group.get("name") == _R2_WRITE_PERMISSION_GROUP_NAME:
            return str(group["id"])
    raise R2CleanupError(f"Cloudflare has no {_R2_WRITE_PERMISSION_GROUP_NAME!r} permission group")


def mint_r2_object_token(credentials: CloudflareR2Credentials) -> tuple[str, SecretStr]:
    """Mint a temporary account token that can write (and so delete) R2 objects.

    The account token in Vault cannot itself be used as an S3 credential: R2's
    S3 access key is a token *id*, and an account-owned token's id is not
    recoverable from its value (``/user/tokens/verify`` only answers for
    user-owned tokens). So mint a short-lived one -- the create response
    carries both the id and the value -- exactly as the connector does when it
    issues per-bucket keys. The caller must revoke it (:func:`revoke_token`).
    """
    policies = [
        {
            "effect": "allow",
            "permission_groups": [{"id": _r2_write_permission_group_id(credentials)}],
            "resources": {f"com.cloudflare.api.account.{credentials.account_id}": "*"},
        }
    ]
    result = _cloudflare_post(
        credentials,
        f"/accounts/{credentials.account_id}/tokens",
        {"name": _SWEEP_TOKEN_NAME, "policies": policies},
    )["result"]
    token_id = str(result.get("id", ""))
    token_value = str(result.get("value", ""))
    if not token_id or not token_value:
        raise R2CleanupError("Cloudflare did not return an id + value for the temporary sweep token")
    return token_id, SecretStr(token_value)


def revoke_token(credentials: CloudflareR2Credentials, token_id: str) -> None:
    """Delete a temporary token minted by :func:`mint_r2_object_token`."""
    _cloudflare_delete(credentials, f"/accounts/{credentials.account_id}/tokens/{token_id}")


def _s3_client(credentials: CloudflareR2Credentials, token_id: str, token_value: SecretStr) -> Any:
    """An S3 client for R2: the access key is the token id, the secret its SHA-256."""
    return boto3.client(
        "s3",
        endpoint_url=f"https://{credentials.account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=token_id,
        aws_secret_access_key=hashlib.sha256(token_value.get_secret_value().encode("utf-8")).hexdigest(),
        region_name="auto",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def wait_for_s3_credentials(s3_client: Any, probe_bucket_name: str) -> None:
    """Block until the freshly-minted token is accepted by R2's S3 endpoint.

    A brand-new Cloudflare token needs a few seconds to propagate; until then
    every S3 call 401s. Without this wait the first buckets in a sweep fail
    with ``Unauthorized`` while the later ones succeed (exactly what the first
    real run did).
    """
    deadline = time.monotonic() + _TOKEN_PROPAGATION_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            s3_client.list_objects_v2(Bucket=probe_bucket_name, MaxKeys=1)
            return
        except (ClientError, BotoCoreError) as e:
            last_error = e
            logger.info("Waiting for the R2 sweep token to propagate to the S3 endpoint...")
            threading.Event().wait(timeout=_TOKEN_PROPAGATION_POLL_SECONDS)
    raise R2CleanupError(f"The R2 sweep token never became usable for S3: {last_error}")


def empty_bucket(s3_client: Any, bucket_name: str) -> int:
    """Delete every object in the bucket (Cloudflare refuses non-empty deletes); returns the count."""
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
        raise R2CleanupError(f"Could not empty bucket {bucket_name}: {e}") from e
    return deleted


def delete_bucket(credentials: CloudflareR2Credentials, bucket_name: str) -> None:
    """Delete an (already emptied) R2 bucket."""
    headers = {"Authorization": f"Bearer {credentials.api_token.get_secret_value()}"}
    try:
        response = httpx.delete(
            f"{_CLOUDFLARE_API_BASE}/accounts/{credentials.account_id}/r2/buckets/{bucket_name}",
            headers=headers,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise R2CleanupError(f"Cloudflare bucket delete for {bucket_name} failed: {e}") from e
    _checked_cloudflare_body(response, f"DELETE bucket {bucket_name}")


def sweep_orphaned_r2_buckets(
    cloudflare: CloudflareR2Credentials,
    supertokens: SuperTokensCoreCredentials,
    *,
    is_dry_run: bool = False,
    min_age_hours: int = _MIN_BUCKET_AGE_HOURS,
) -> tuple[str, ...]:
    """Delete every R2 bucket whose owning account is gone; returns the names swept.

    Fails closed: the live-owner set is collected first, and any error there
    aborts before a single delete.
    """
    live_owner_prefixes = collect_live_owner_prefixes(supertokens)
    logger.info("R2 sweep: {} live account(s) protect their buckets", len(live_owner_prefixes))
    buckets = list_r2_buckets(cloudflare)
    sweepable = find_sweepable_buckets(
        buckets, live_owner_prefixes, now=datetime.now(timezone.utc), min_age_hours=min_age_hours
    )
    if not sweepable:
        logger.info("R2 sweep: no ownerless buckets among {} bucket(s).", len(buckets))
        return ()
    logger.info(
        "R2 sweep: {} of {} bucket(s) have no live owner{}",
        len(sweepable),
        len(buckets),
        " (dry run; deleting nothing)" if is_dry_run else "",
    )
    if is_dry_run:
        for bucket in sweepable:
            logger.info("  would delete {} (created {})", bucket.name, bucket.created_at)
        return tuple(bucket.name for bucket in sweepable)
    token_id, token_value = mint_r2_object_token(cloudflare)
    swept: list[str] = []
    try:
        s3_client = _s3_client(cloudflare, token_id, token_value)
        wait_for_s3_credentials(s3_client, sweepable[0].name)
        for bucket in sweepable:
            try:
                object_count = empty_bucket(s3_client, bucket.name)
                delete_bucket(cloudflare, bucket.name)
            except R2CleanupError as e:
                # One stubborn bucket must not strand the rest of the sweep.
                logger.error("R2 sweep: could not delete {}: {}", bucket.name, e)
                continue
            logger.info("R2 sweep: deleted {} ({} object(s))", bucket.name, object_count)
            swept.append(bucket.name)
    finally:
        # The temporary token grants account-wide R2 writes; never leave it behind.
        revoke_token(cloudflare, token_id)
    return tuple(swept)
