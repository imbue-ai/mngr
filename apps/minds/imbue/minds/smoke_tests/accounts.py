"""The connector and pool-DB calls the smoke suite's accounts and cleanup need.

Every request carries the smoke User-Agent, so the connector's access log (and
the download events it records) can tell a smoke run apart from a real user.

The owner routes (sign in, list leases, release, unshare) are what a smoke
test's own account can do; the admin routes (test signup, account lookup,
operator release) and the pool-DB read are what only the setup and teardown
jobs hold credentials for. Both live here because teardown drives them in one
pass: release what the run leased, then delete the account that leased it.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final
from urllib.parse import quote

import httpx2
import psycopg2
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.session_store import derive_user_id_prefix
from imbue.minds.envs.primitives import VaultSecretNotFoundError
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.smoke_tests.data_types import SmokeAccountCredentials
from imbue.minds.smoke_tests.data_types import SmokeAccountDeletionReport
from imbue.minds.smoke_tests.primitives import PERSISTENT_ACCOUNT_EMAIL_VAULT_KEY
from imbue.minds.smoke_tests.primitives import PERSISTENT_ACCOUNT_PASSWORD_VAULT_KEY
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_EMAIL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeAccountError
from imbue.minds.smoke_tests.primitives import SmokeAccountExistsError
from imbue.minds.smoke_tests.primitives import SmokeSignInRefusedError
from imbue.minds.smoke_tests.primitives import persistent_account_vault_dir

# Sent on every request a smoke run makes.
SMOKE_USER_AGENT: Final[str] = "imbue-studio-smoke-test/1"

_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0
# A release destroys a slice VM synchronously, so it is the slowest owner call.
_RELEASE_TIMEOUT_SECONDS: Final[float] = 900.0
_POOL_DB_CONNECT_TIMEOUT_SECONDS: Final[int] = 30
# The ``state`` a share row carries while the workspace is actually shared.
_ACTIVE_SHARE_STATE: Final[str] = "active"
# What ``POST /auth/signin`` answers for an unknown address or a wrong password.
_WRONG_CREDENTIALS_STATUS: Final[str] = "WRONG_CREDENTIALS"


@pure
def origin_of(url: AnyUrl) -> str:
    """The URL without its trailing slash, ready to have a route path appended."""
    return str(url).rstrip("/")


@pure
def bearer_header(token: SecretStr) -> dict[str, str]:
    """The Authorization header for a bearer token (an access token or the admin key)."""
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


@pure
def smoke_headers(token: SecretStr | None) -> dict[str, str]:
    """The headers every smoke request carries, with a bearer when one applies."""
    headers = {"User-Agent": SMOKE_USER_AGENT}
    if token is None:
        return headers
    return {**headers, **bearer_header(token)}


@pure
def persistent_account_credentials_from_env_or_none(environ: Mapping[str, str]) -> SmokeAccountCredentials | None:
    """The persistent account's credentials as the CI test job exports them, or None when the environment has none."""
    email = environ.get(SMOKE_ACCOUNT_EMAIL_ENV_VAR, "")
    password = environ.get(SMOKE_ACCOUNT_PASSWORD_ENV_VAR, "")
    if not email or not password:
        return None
    return SmokeAccountCredentials(email=NonEmptyStr(email), password=SecretStr(password))


def read_persistent_account_credentials_or_none(tier: str) -> SmokeAccountCredentials | None:
    """The tier's stored persistent-account credentials, or None when the tier has none yet."""
    vault_dir = persistent_account_vault_dir(tier)
    try:
        values = read_vault_kv(vault_dir)
    except VaultSecretNotFoundError:
        return None
    email = values.get(PERSISTENT_ACCOUNT_EMAIL_VAULT_KEY, "")
    password = values.get(PERSISTENT_ACCOUNT_PASSWORD_VAULT_KEY, "")
    if not email or not password:
        return None
    return SmokeAccountCredentials(email=NonEmptyStr(email), password=SecretStr(password))


def create_test_account(
    *,
    connector_url: AnyUrl,
    admin_api_key: SecretStr,
    email: str,
    password: SecretStr,
    is_verified: bool,
    plan: str,
) -> tuple[NonEmptyStr, SecretStr]:
    """Mint an account through ``POST /admin/test-signup``; return its user id and access token.

    The operator-trust path past the signup form's Turnstile and waitlist,
    which is why the suite can create accounts on a tier whose browser signup
    automation cannot drive. Raises :class:`SmokeAccountExistsError` when the
    address is already taken, which is the signal the persistent-account
    command treats as "already created".
    """
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{origin_of(connector_url)}/admin/test-signup",
            headers=smoke_headers(admin_api_key),
            json={
                "email": email,
                "password": password.get_secret_value(),
                "verified": is_verified,
                "plan": plan,
            },
        )
    if response.status_code != 200:
        raise SmokeAccountError(
            f"POST /admin/test-signup for {email} answered {response.status_code}: {response.text[:300]!r}"
        )
    body = response.json()
    status = body.get("status")
    if status == "EMAIL_ALREADY_EXISTS":
        raise SmokeAccountExistsError(f"An account already exists at {email}.")
    if status != "OK":
        raise SmokeAccountError(f"POST /admin/test-signup for {email} answered a non-OK body: {body!r}")
    return NonEmptyStr(str(body["user"]["user_id"])), SecretStr(str(body["tokens"]["access_token"]))


def resolve_account_user_id_or_none(
    *, connector_url: AnyUrl, admin_api_key: SecretStr, email: str
) -> NonEmptyStr | None:
    """The user id the connector resolves ``email`` to, or None when no such account exists."""
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.get(
            f"{origin_of(connector_url)}/admin/accounts/{quote(email, safe='@')}",
            headers=smoke_headers(admin_api_key),
        )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise SmokeAccountError(
            f"GET /admin/accounts/{email} answered {response.status_code}: {response.text[:300]!r}"
        )
    return NonEmptyStr(str(response.json()["user_id"]))


def sign_in(*, connector_url: AnyUrl, credentials: SmokeAccountCredentials) -> SecretStr:
    """Sign the account in through ``POST /auth/signin``; return its access token.

    Raises :class:`SmokeSignInRefusedError` when the connector refuses the
    credentials, which is also how a deleted account answers -- the signal a
    teardown reads as "this account is already gone".
    """
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{origin_of(connector_url)}/auth/signin",
            headers=smoke_headers(None),
            json={"email": str(credentials.email), "password": credentials.password.get_secret_value()},
        )
    if response.status_code != 200:
        raise SmokeAccountError(
            f"POST /auth/signin for {credentials.email} answered {response.status_code}: {response.text[:300]!r}"
        )
    body = response.json()
    status = body.get("status")
    if status == _WRONG_CREDENTIALS_STATUS:
        raise SmokeSignInRefusedError(f"The connector refused the credentials of {credentials.email}.")
    if status != "OK":
        raise SmokeAccountError(f"POST /auth/signin for {credentials.email} answered a non-OK body: {body!r}")
    return SecretStr(str(body["tokens"]["access_token"]))


@pure
def leased_host_db_ids_of(listing: Sequence[Mapping[str, Any]]) -> list[str]:
    """The pool-row ids in a ``GET /hosts`` listing.

    ``host_db_id`` is the id the release routes take, which is why the listing
    is read for its ids rather than for the workspaces themselves.
    """
    return [str(entry["host_db_id"]) for entry in listing]


@pure
def leased_host_db_id_for_host_id(listing: Sequence[Mapping[str, Any]], host_id: str) -> str | None:
    """The pool-row id of the leased row whose host coordinate is ``host_id``, or None when none is."""
    for entry in listing:
        if str(entry.get("host_id", "")) == host_id:
            return str(entry["host_db_id"])
    return None


def list_leased_hosts(*, connector_url: AnyUrl, bearer: SecretStr) -> list[dict[str, Any]]:
    """Every workspace the signed-in account currently leases, as the connector lists it."""
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.get(f"{origin_of(connector_url)}/hosts", headers=smoke_headers(bearer))
    if response.status_code != 200:
        raise SmokeAccountError(f"GET /hosts answered {response.status_code}: {response.text[:300]!r}")
    return [entry for entry in response.json() if isinstance(entry, dict)]


def list_leased_host_db_ids(*, connector_url: AnyUrl, bearer: SecretStr) -> list[str]:
    """The pool-row ids of every workspace the signed-in account currently leases."""
    return leased_host_db_ids_of(list_leased_hosts(connector_url=connector_url, bearer=bearer))


@pure
def active_share_host_ids_of(payload: Mapping[str, Any]) -> list[str]:
    """The host ids of the shares a ``GET /shares`` payload reports as still on.

    An inactive row is a share the owner already turned off; its relay token is
    already gone, so teardown has nothing left to do for it.
    """
    shares = payload.get("shares", [])
    return [str(share["host_id"]) for share in shares if str(share.get("state", "")) == _ACTIVE_SHARE_STATE]


def list_active_share_host_ids(*, connector_url: AnyUrl, bearer: SecretStr) -> list[str]:
    """Every workspace the signed-in account still has sharing on.

    How teardown finds a share to turn off when the run recorded no manifest
    (a test process that died before writing one still shared a workspace).
    """
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.get(f"{origin_of(connector_url)}/shares", headers=smoke_headers(bearer))
    if response.status_code != 200:
        raise SmokeAccountError(f"GET /shares answered {response.status_code}: {response.text[:300]!r}")
    return active_share_host_ids_of(response.json())


def release_host(*, connector_url: AnyUrl, bearer: SecretStr, host_db_id: str) -> str:
    """Release one leased workspace as its owner; return the status the connector reported.

    Idempotent: a row that is already gone answers ``already_released``, which
    is a success here -- teardown runs over whatever the test may already have
    cleaned up.
    """
    with httpx2.Client(timeout=_RELEASE_TIMEOUT_SECONDS) as client:
        response = client.post(f"{origin_of(connector_url)}/hosts/{host_db_id}/release", headers=smoke_headers(bearer))
    if response.status_code != 200:
        raise SmokeAccountError(
            f"POST /hosts/{host_db_id}/release answered {response.status_code}: {response.text[:300]!r}"
        )
    return str(response.json().get("status", ""))


def release_host_as_admin(*, connector_url: AnyUrl, admin_api_key: SecretStr, host_db_id: str) -> str:
    """Release one workspace as the operator, whoever owns it; return the reported status.

    The path for a stale account's leases: nobody holds that account's
    password any more, and the deletion tool refuses an account that still
    holds a pool row.
    """
    with httpx2.Client(timeout=_RELEASE_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{origin_of(connector_url)}/admin/workspaces/{host_db_id}/release",
            headers=smoke_headers(admin_api_key),
        )
    if response.status_code != 200:
        raise SmokeAccountError(
            f"POST /admin/workspaces/{host_db_id}/release answered {response.status_code}: {response.text[:300]!r}"
        )
    return str(response.json().get("status", ""))


def disable_share(*, connector_url: AnyUrl, bearer: SecretStr, host_id: str) -> bool:
    """Turn sharing off for one workspace as its owner; False when it had no share at all.

    A workspace the account never shared has no share row, which answers 404 --
    nothing left for teardown to do. One that is already off still has its row,
    so turning it off again is the connector's own idempotent path.
    """
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.delete(f"{origin_of(connector_url)}/shares/{host_id}", headers=smoke_headers(bearer))
    if response.status_code == 404:
        return False
    if response.status_code != 200:
        raise SmokeAccountError(f"DELETE /shares/{host_id} answered {response.status_code}: {response.text[:300]!r}")
    return True


def list_leased_host_db_ids_by_user_prefix(*, dsn: SecretStr, user_id: str) -> list[str]:
    """The pool-row ids naming ``user_id``, read straight from the tier's pool DB.

    For an account the teardown cannot sign in as. Matches on ``leased_to_user``
    alone, with no status filter, exactly as ``scripts/delete_accounts.py``
    does: a released row is deleted and an available row's ``leased_to_user``
    is NULL, so any surviving row naming the user is one the deletion tool
    would refuse the account for, whatever lifecycle status it is in.
    """
    connection = psycopg2.connect(dsn.get_secret_value(), connect_timeout=_POOL_DB_CONNECT_TIMEOUT_SECONDS)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM pool_hosts WHERE leased_to_user = %s",
                (str(derive_user_id_prefix(user_id)),),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


# The header ``POST /account/deletion-token`` names and ``DELETE /account``
# requires. Declared here rather than imported: apps/minds and the connector are
# separate deployables sharing no package (``account_deletion_tokens.py`` holds
# the connector's copy), so a test pins this literal against drift.
ACCOUNT_DELETION_TOKEN_HEADER: Final[str] = "X-Imbue-Deletion-Token"


def mint_account_deletion_token(*, connector_url: AnyUrl, bearer: SecretStr) -> SecretStr:
    """Mint the short-lived token ``DELETE /account`` requires, as the signed-in account."""
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.post(f"{origin_of(connector_url)}/account/deletion-token", headers=smoke_headers(bearer))
    if response.status_code != 200:
        raise SmokeAccountError(
            f"POST /account/deletion-token answered {response.status_code}: {response.text[:300]!r}"
        )
    token = response.json().get("token", "")
    if not token:
        raise SmokeAccountError(f"POST /account/deletion-token returned no token: {response.text[:300]!r}")
    return SecretStr(str(token))


def delete_own_account(*, connector_url: AnyUrl, bearer: SecretStr, deletion_token: SecretStr) -> Mapping[str, Any]:
    """Delete the signed-in account through the route a user would use.

    The route takes the account from the session alone, so this can only delete
    the account ``bearer`` belongs to. It marks the account deleted and starts
    the grace period; :func:`admin_finalize_account` is what removes it.
    """
    headers = {**smoke_headers(bearer), ACCOUNT_DELETION_TOKEN_HEADER: deletion_token.get_secret_value()}
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.delete(f"{origin_of(connector_url)}/account", headers=headers)
    if response.status_code != 200:
        raise SmokeAccountError(f"DELETE /account answered {response.status_code}: {response.text[:300]!r}")
    return dict(response.json())


def admin_delete_account(*, connector_url: AnyUrl, admin_api_key: SecretStr, email: str) -> Mapping[str, Any]:
    """The operator delete step, for an account the suite holds no password for."""
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{origin_of(connector_url)}/admin/accounts/{quote(email, safe='@')}/delete",
            headers=smoke_headers(admin_api_key),
        )
    if response.status_code != 200:
        raise SmokeAccountError(
            f"POST /admin/accounts/{email}/delete answered {response.status_code}: {response.text[:300]!r}"
        )
    return dict(response.json())


def admin_finalize_account(
    *, connector_url: AnyUrl, admin_api_key: SecretStr, email: str
) -> SmokeAccountDeletionReport:
    """End an already-deleted account's grace period now, so it is really gone."""
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{origin_of(connector_url)}/admin/accounts/{quote(email, safe='@')}/finalize",
            headers=smoke_headers(admin_api_key),
        )
    if response.status_code != 200:
        raise SmokeAccountError(
            f"POST /admin/accounts/{email}/finalize answered {response.status_code}: {response.text[:300]!r}"
        )
    return parse_finalize_answer(response.json())


@pure
def parse_finalize_answer(payload: Mapping[str, Any]) -> SmokeAccountDeletionReport:
    """The finalize step's answer, refused unless it records an account that is now gone."""
    finalized_at = str(payload.get("finalized_at") or "")
    user_id = str(payload.get("user_id") or "")
    if not finalized_at or not user_id:
        raise SmokeAccountError(f"The finalize step did not report a finalized account: {dict(payload)!r}")
    raw_rows = payload.get("rows_deleted") or {}
    if not isinstance(raw_rows, Mapping):
        raise SmokeAccountError(f"The finalize step reported rows_deleted that is not a mapping: {raw_rows!r}")
    return SmokeAccountDeletionReport(
        user_id=NonEmptyStr(user_id),
        finalized_at=NonEmptyStr(finalized_at),
        rows_deleted_by_table={str(table): int(count) for table, count in raw_rows.items()},
        is_litellm_user_deleted=bool(payload.get("litellm_user_deleted")),
    )
