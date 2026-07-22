"""Helpers test bodies + fixtures share across the deployment_tests suite.

Lives next to ``data_types.py`` rather than inside ``conftest.py`` because
pytest treats ``conftest.py`` as an implementation detail (loaded by
auto-discovery, not normally imported from). Anything a test body
``imports`` should be in a regular module.
"""

import json
import os
import re
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from typing import Final

import httpx
from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.cli._activated_env import MODAL_PROFILE_ENV_VAR
from imbue.minds.cli._activated_env import modal_profile_for_tier_or_none
from imbue.minds.cli._activated_env import tier_for_env_name
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.primitives import SharedEnvRole
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import delete_vault_kv
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.envs.vault_reader import write_vault_kv
from imbue.minds.errors import MindError

_SUPERTOKENS_TENANT_ID: Final[str] = "public"
_CONNECTOR_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0
_SUPERTOKENS_ADMIN_TIMEOUT_SECONDS: Final[float] = 30.0
_ENV_READY_TIMEOUT_SECONDS: Final[float] = 60.0
_ENV_READY_POLL_INTERVAL_SECONDS: Final[float] = 1.0
_MODAL_ENV_LIST_TIMEOUT_SECONDS: Final[float] = 30.0
_NEON_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0
_SUPERTOKENS_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0

# Per-env dynamic-secret handoff. A freshly-deployed ci env mints its own
# SuperTokens app + Neon project, so those secrets are not static Vault
# values -- the env-build step writes them to a per-env Vault path that the
# (possibly separate-machine) test runner + destroy/sweep jobs read back. The
# path is keyed by the env name (not a run id) so that any job that knows the
# env -- including the leaked-env sweep, which discovers envs by Modal
# enumeration and never sees the original run -- can reconstruct the secrets.
# It stays under ``.../runs/`` so the existing ``minds/ci/runs/*`` Vault policy
# covers it without a terraform change.
RUN_SECRETS_VAULT_ROOT: Final[str] = "secrets/minds/ci/runs"
# The four per-env secret keys the ``shared_env`` fixture hands to a test
# (matches the leaf names written into ``~/.minds-<env>/secrets.toml``).
SHARED_ENV_SECRET_KEYS: Final[tuple[str, ...]] = (
    "SUPERTOKENS_CONNECTION_URI",
    "SUPERTOKENS_API_KEY",
    "NEON_HOST_POOL_DSN",
    "NEON_LITELLM_DSN",
)
# Static Vault path holding the fixed CI test-user credentials (an
# ``@imbue.com`` email, paid out of the box via the ci tier's seeded
# ``paid_domains``). Created during env-build, logged in by the test.
CI_PAID_ACCOUNTS_VAULT_PATH: Final[VaultPath] = VaultPath("secrets/minds/ci/paid-accounts")
CI_TEST_USER_EMAIL_KEY: Final[str] = "CI_TEST_USER_EMAIL"
CI_TEST_USER_PASSWORD_KEY: Final[str] = "CI_TEST_USER_PASSWORD"


def env_secrets_vault_path(*, env_name: DevEnvName, role: SharedEnvRole) -> VaultPath:
    """Vault directory holding one env's per-env secrets for ``role``.

    Keyed by the env name so every job that knows the env (build, test,
    destroy, sweep) resolves the same path without needing the originating run.
    """
    return VaultPath(f"{RUN_SECRETS_VAULT_ROOT}/{env_name}/shared-{role}")


def publish_shared_env_secrets(*, env_name: DevEnvName, role: SharedEnvRole, secrets: Mapping[str, str]) -> None:
    """Write a freshly-deployed env's per-env secrets to its per-env Vault path."""
    write_vault_kv(env_secrets_vault_path(env_name=env_name, role=role), dict(secrets))


def read_shared_env_secrets(*, env_name: DevEnvName, role: SharedEnvRole) -> dict[str, str]:
    """Read back the per-env secrets a prior :func:`publish_shared_env_secrets` wrote."""
    return read_vault_kv(env_secrets_vault_path(env_name=env_name, role=role))


def delete_shared_env_secrets(*, env_name: DevEnvName, role: SharedEnvRole) -> None:
    """Delete an env's per-env secrets from Vault. Idempotent against already-gone."""
    delete_vault_kv(env_secrets_vault_path(env_name=env_name, role=role))


def read_ci_test_user_credentials() -> tuple[NonEmptyStr, SecretStr]:
    """Read the fixed ``(email, password)`` for the CI test user from Vault."""
    kv = read_vault_kv(CI_PAID_ACCOUNTS_VAULT_PATH)
    email = kv.get(CI_TEST_USER_EMAIL_KEY)
    password = kv.get(CI_TEST_USER_PASSWORD_KEY)
    if not email or not password:
        raise MindError(
            f"Vault path {CI_PAID_ACCOUNTS_VAULT_PATH!r} is missing "
            f"{CI_TEST_USER_EMAIL_KEY}/{CI_TEST_USER_PASSWORD_KEY}; populate them per the Phase 0 runbook."
        )
    return NonEmptyStr(email), SecretStr(password)


class MintedLiteLLMKey(FrozenModel):
    """A LiteLLM virtual key minted through a shared env's connector."""

    key: SecretStr = Field(description="The minted LiteLLM virtual key")
    base_url: NonEmptyStr = Field(description="The litellm proxy base URL returned with the key (no trailing slash)")


def signin_and_mint_litellm_key(
    *,
    connector_url: str,
    email: str,
    password: str,
    key_alias: str,
    max_budget: float,
    budget_duration: str,
    timeout_seconds: float = _CONNECTOR_HTTP_TIMEOUT_SECONDS,
) -> MintedLiteLLMKey:
    """Sign in to a shared env's connector and mint a LiteLLM key.

    The exact product path the litellm deployment tests exercise: POST
    ``/auth/signin`` for an access token, then POST ``/keys/create`` (which
    runs the paid-account gate) for the key + proxy base URL.
    """
    base = connector_url.rstrip("/")
    with httpx.Client(timeout=timeout_seconds) as client:
        signin = client.post(f"{base}/auth/signin", json={"email": email, "password": password})
        signin.raise_for_status()
        signin_json = signin.json()
        assert signin_json.get("status") == "OK", f"connector /auth/signin returned non-OK: {signin_json!r}"
        access_token = signin_json["tokens"]["access_token"]
        key_response = client.post(
            f"{base}/keys/create",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"key_alias": key_alias, "max_budget": max_budget, "budget_duration": budget_duration},
        )
        assert key_response.status_code == 200, (
            f"connector /keys/create failed ({key_response.status_code}); the paid-account gate or "
            f"litellm wiring is broken: {key_response.text[:400]!r}"
        )
        key_material = key_response.json()
    return MintedLiteLLMKey(
        key=SecretStr(str(key_material["key"])),
        base_url=NonEmptyStr(str(key_material["base_url"]).rstrip("/")),
    )


def create_verified_user_via_admin_api(
    *,
    connection_uri: SecretStr,
    api_key: SecretStr,
    connector_url: AnyUrl,
    email: NonEmptyStr,
    password: SecretStr,
) -> tuple[NonEmptyStr, SecretStr]:
    """Create a user via the SuperTokens admin API, mark email verified, sign in.

    Three HTTP round-trips against the SuperTokens core (auth'd with the
    ``api-key`` header) plus one signin call against the deployed
    connector to mint a real session JWT:

    1. ``POST <core>/<tenant>/recipe/signup`` -- creates the emailpassword user.
    2. ``POST <core>/<tenant>/recipe/user/email/verify/token`` -- mints a verify token.
    3. ``POST <core>/<tenant>/recipe/user/email/verify`` -- consumes it (the
       connector's email-verification gate is ``REQUIRED``).
    4. ``POST <connector_url>/auth/signin`` -- signs in to receive a session JWT.

    Returns ``(user_id, access_token)``.
    """
    headers = {"api-key": api_key.get_secret_value(), "rid": "emailpassword"}
    base = str(connection_uri.get_secret_value()).rstrip("/")
    with httpx.Client(timeout=_SUPERTOKENS_ADMIN_TIMEOUT_SECONDS) as client:
        signup = client.post(
            f"{base}/{_SUPERTOKENS_TENANT_ID}/recipe/signup",
            headers=headers,
            json={"email": str(email), "password": password.get_secret_value()},
        )
        signup.raise_for_status()
        signup_json = signup.json()
        if signup_json.get("status") != "OK":
            raise MindError(f"SuperTokens admin signup for {email!r} returned non-OK: {signup_json!r}")
        user_id = NonEmptyStr(signup_json["recipeUserId"])

        verify_headers = {"api-key": api_key.get_secret_value(), "rid": "emailverification"}
        token_resp = client.post(
            f"{base}/{_SUPERTOKENS_TENANT_ID}/recipe/user/email/verify/token",
            headers=verify_headers,
            json={"userId": str(user_id), "email": str(email)},
        )
        token_resp.raise_for_status()
        token_json = token_resp.json()
        if token_json.get("status") != "OK":
            raise MindError(f"SuperTokens admin verify-token mint for {email!r} returned non-OK: {token_json!r}")
        verification_token = token_json["token"]

        verify_resp = client.post(
            f"{base}/{_SUPERTOKENS_TENANT_ID}/recipe/user/email/verify",
            headers=verify_headers,
            json={"method": "token", "token": verification_token},
        )
        verify_resp.raise_for_status()
        verify_json = verify_resp.json()
        if verify_json.get("status") != "OK":
            raise MindError(f"SuperTokens admin email-verify for {email!r} returned non-OK: {verify_json!r}")

        signin_resp = client.post(
            f"{str(connector_url).rstrip('/')}/auth/signin",
            json={"email": str(email), "password": password.get_secret_value()},
        )
        signin_resp.raise_for_status()
        signin_json = signin_resp.json()
        if signin_json.get("status") != "OK":
            raise MindError(f"Connector /auth/signin for {email!r} returned non-OK: {signin_json!r}")
        access_token = signin_json["tokens"]["access_token"]
        return user_id, SecretStr(access_token)


def build_minds_env_subprocess_env(name: DevEnvName) -> dict[str, str]:
    """Build the env dict for a ``minds env deploy/destroy`` subprocess targeting ``name``.

    Mirrors what ``minds env activate --deploy <name>`` exports (without
    going through the print-shell-vars indirection): MINDS_ROOT_NAME,
    MNGR_HOST_DIR, MNGR_PREFIX, MINDS_CLIENT_CONFIG_PATH, and (for tiers
    with a committed ``modal_workspace``) MODAL_PROFILE. The
    MODAL_PROFILE lookup goes through the same
    ``modal_profile_for_tier_or_none`` helper ``minds env activate``
    itself uses, so a separated CI Modal workspace (planned)
    automatically lands here without having to update a test-only
    hardcoded constant. Including MODAL_PROFILE is required for the
    subprocess deploy/destroy to satisfy the deploy-mode activation
    gate enforced by ``require_deploy_mode_activation``.

    Inherits VAULT_TOKEN / VAULT_ADDR / VAULT_NAMESPACE / ANTHROPIC_API_KEY
    from the parent process unchanged so the subprocess can read Vault +
    talk to Anthropic without further wiring.
    """
    root_name = root_name_for_env_name(str(name))
    env = dict(os.environ)
    env[MINDS_ROOT_NAME_ENV_VAR] = root_name
    env["MNGR_HOST_DIR"] = str(mngr_host_dir_for(root_name))
    env["MNGR_PREFIX"] = mngr_prefix_for(root_name)
    env["MINDS_CLIENT_CONFIG_PATH"] = str(client_config_file(name))
    modal_profile = modal_profile_for_tier_or_none(tier_for_env_name(str(name)))
    if modal_profile is not None:
        env[MODAL_PROFILE_ENV_VAR] = modal_profile
    return env


def wait_for_env_ready(env: SharedEnvHandle, timeout_seconds: float = _ENV_READY_TIMEOUT_SECONDS) -> None:
    """Poll both the connector and the litellm proxy until they return 200 (or raise).

    Tests should call this at the very start of their body so they can
    rely on the env being awake before any other assertion. Mirrors
    what the deploy-side ``await_apps_healthy`` does: poll the
    connector's ``/health/liveness`` AND the litellm proxy's
    ``/health/liveness`` with the same cold-boot tolerance. Either
    endpoint timing out / 4xx-ing during the swap window counts as
    "keep waiting" rather than a hard failure.
    """
    connector_url = str(env.urls.connector_url).rstrip("/")
    litellm_proxy_url = str(env.urls.litellm_proxy_url).rstrip("/")
    _wait_for_url_alive(
        url=f"{connector_url}/health/liveness",
        expected_body={"status": "ok"},
        timeout_seconds=timeout_seconds,
    )
    _wait_for_url_alive(
        url=f"{litellm_proxy_url}/health/liveness",
        expected_body=None,
        timeout_seconds=timeout_seconds,
    )


def _wait_for_url_alive(
    *,
    url: str,
    expected_body: dict[str, str] | None,
    timeout_seconds: float,
) -> None:
    """Poll ``url`` until it returns 200 (optionally matching ``expected_body``)."""
    deadline = datetime.now(timezone.utc).timestamp() + timeout_seconds
    last_status: int | None = None
    last_body_excerpt: str = ""
    with httpx.Client(timeout=10.0) as client:
        while datetime.now(timezone.utc).timestamp() < deadline:
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                last_body_excerpt = f"httpx error: {exc}"
                last_status = None
            else:
                last_status = response.status_code
                last_body_excerpt = response.text[:200]
                if response.status_code == 200 and (expected_body is None or response.json() == expected_body):
                    return
            time.sleep(_ENV_READY_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"URL {url!r} did not become ready within {timeout_seconds:.0f}s "
        f"(last_status={last_status!r}, last_body={last_body_excerpt!r})."
    )


def delete_user_via_admin_api(
    *,
    connection_uri: SecretStr,
    api_key: SecretStr,
    user_id: NonEmptyStr,
) -> None:
    """Delete a SuperTokens user via the core's ``/user/remove`` endpoint.

    SuperTokens returns 200 for both "deleted" and "user never existed",
    so we treat any 2xx as success. Non-2xx raises (let the caller
    decide whether to swallow it -- the verified_user fixture swallows
    it because the session-autouse sweep + the env's eventual SuperTokens
    app teardown are the safety nets).
    """
    base = str(connection_uri.get_secret_value()).rstrip("/")
    headers = {"api-key": api_key.get_secret_value()}
    with httpx.Client(timeout=_SUPERTOKENS_ADMIN_TIMEOUT_SECONDS) as client:
        resp = client.post(f"{base}/user/remove", headers=headers, json={"userId": str(user_id)})
        resp.raise_for_status()


def sweep_stale_users(
    *,
    connection_uri: SecretStr,
    api_key: SecretStr,
    email_pattern: re.Pattern[str],
    max_age_seconds: int,
) -> int:
    """List + delete users in the SuperTokens app matching ``email_pattern`` older than the cutoff.

    Paginates through ``GET /<tenant>/users`` until exhausted. Returns
    the count of deleted users. Any per-user delete failure is logged
    but does not abort the sweep -- the next session's sweep will pick
    it up.

    Used by the session-autouse ``_sweep_stale_test_users`` fixture as
    defensive cleanup against state leaked by a prior run that crashed
    before its ``verified_user`` teardown fired. The ``max_age_seconds``
    cutoff guards against deleting a concurrent run's freshly-created
    user.
    """
    base = str(connection_uri.get_secret_value()).rstrip("/")
    list_headers = {"api-key": api_key.get_secret_value()}
    cutoff_ms = (datetime.now(timezone.utc).timestamp() - max_age_seconds) * 1000
    deleted = 0
    # ``None`` on the first iteration (no pagination token) and on the
    # final iteration (server signalled no more pages). The loop runs
    # until the latter; both states share the same shape so we can use
    # a single condition variable.
    pagination_token: str | None = None
    has_unfetched_pages = True
    with httpx.Client(timeout=_SUPERTOKENS_ADMIN_TIMEOUT_SECONDS) as client:
        while has_unfetched_pages:
            params: dict[str, str] = {"limit": "200"}
            if pagination_token:
                params["paginationToken"] = pagination_token
            resp = client.get(f"{base}/{_SUPERTOKENS_TENANT_ID}/users", headers=list_headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            for raw_user in data.get("users", []):
                user_info = raw_user.get("user", raw_user)
                emails: list[str] = user_info.get("emails", [])
                user_id = user_info.get("id") or user_info.get("recipeUserId")
                time_joined = user_info.get("timeJoined", 0)
                if not user_id or not emails or time_joined > cutoff_ms:
                    continue
                if not any(email_pattern.match(email) for email in emails):
                    continue
                try:
                    delete_user_via_admin_api(
                        connection_uri=connection_uri, api_key=api_key, user_id=NonEmptyStr(user_id)
                    )
                    deleted += 1
                except (MindError, httpx.HTTPError) as exc:
                    logger.warning("Stale-user sweep: failed to delete {!r} ({})", user_id, exc)
            pagination_token = data.get("nextPaginationToken")
            has_unfetched_pages = bool(pagination_token)
    return deleted


def modal_env_exists(name: DevEnvName) -> bool:
    """Shell out to ``modal environment list --json`` and check whether ``name`` is in the result.

    Used by deployment_tests to verify Modal-side resource creation /
    teardown. Threads MODAL_PROFILE via the same path
    ``build_minds_env_subprocess_env`` uses so the listing targets the
    workspace this env lives in.
    """
    sub_env = dict(os.environ)
    modal_profile = modal_profile_for_tier_or_none(tier_for_env_name(str(name)))
    if modal_profile is not None:
        sub_env[MODAL_PROFILE_ENV_VAR] = modal_profile
    result = subprocess.run(
        ["uv", "run", "modal", "environment", "list", "--json"],
        env=sub_env,
        capture_output=True,
        text=True,
        timeout=_MODAL_ENV_LIST_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise MindError(
            f"`modal environment list --json` exited {result.returncode}: stderr={result.stderr.strip()!r}"
        )
    try:
        envs = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MindError(f"`modal environment list --json` returned non-JSON ({exc}): {result.stdout[:200]!r}") from exc
    # Modal's CLI emits a list of dicts; the env name lives under "Name" (capital N
    # in 1.4.x; older builds used "name"). Accept either.
    for entry in envs:
        if entry.get("Name") == str(name) or entry.get("name") == str(name):
            return True
    return False


def neon_project_exists(*, name: DevEnvName, org_id: str, api_token: SecretStr) -> bool:
    """Return True if a Neon project named ``minds-<name>`` exists under ``org_id``.

    Mirrors the lookup ``minds env destroy`` uses internally (Neon
    doesn't enforce unique names, so we look up by name + org). Returns
    True for 1+ matches, False for zero matches.
    """
    project_name = f"minds-{name}"
    url = f"https://console.neon.tech/api/v2/projects?org_id={org_id}&limit=400"
    headers = {"Authorization": f"Bearer {api_token.get_secret_value()}"}
    with httpx.Client(timeout=_NEON_PROBE_TIMEOUT_SECONDS) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
    payload = resp.json()
    raw_projects = payload.get("projects", [])
    matches = [p for p in raw_projects if p.get("name") == project_name]
    return len(matches) > 0


def supertokens_app_exists(*, name: DevEnvName, core_base_url: str, api_key: SecretStr) -> bool:
    """Return True if a SuperTokens app with id ``name`` exists in the core.

    SuperTokens has no native "GET app by id" endpoint; we probe via the
    multi-tenancy list-tenant call scoped to the app. If the app exists,
    we get 200. If not, we get 401 / 404 with an "app does not exist"
    style error.
    """
    url = f"{core_base_url.rstrip('/')}/appid-{name}/recipe/multitenancy/tenant/list"
    headers = {"api-key": api_key.get_secret_value(), "cdi-version": "5.1", "Accept": "application/json"}
    with httpx.Client(timeout=_SUPERTOKENS_PROBE_TIMEOUT_SECONDS) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code == 200:
        return True
    body = resp.text.lower()
    if resp.status_code in (401, 404) or "app does not exist" in body or "not found" in body:
        return False
    raise MindError(
        f"SuperTokens probe for app {str(name)!r} returned an unexpected response: "
        f"status={resp.status_code} body={resp.text[:300]!r}"
    )


def load_ci_credentials_from_vault() -> dict[str, str]:
    """Read the ci-tier vault entries the round-trip test needs.

    Returns a dict with NEON_ORG_ID, NEON_API_TOKEN, SUPERTOKENS_CONNECTION_URI,
    SUPERTOKENS_API_KEY. Reads from ``secrets/minds/ci/neon-admin`` +
    ``secrets/minds/ci/supertokens`` via the same ``read_vault_kv``
    helper the CLI uses, so the test honors the operator's existing
    Vault token / address. The ci tier's vault namespace mirrors the
    dev tier's today; the two are kept separate so we can diverge
    later without churning the test scaffolding.
    """
    neon_kv = read_vault_kv(VaultPath("secrets/minds/ci/neon-admin"))
    st_kv = read_vault_kv(VaultPath("secrets/minds/ci/supertokens"))
    missing: list[str] = []
    for key in ("NEON_ORG_ID", "NEON_API_TOKEN"):
        if not neon_kv.get(key):
            missing.append(f"secrets/minds/ci/neon-admin/{key}")
    for key in ("SUPERTOKENS_CONNECTION_URI", "SUPERTOKENS_API_KEY"):
        if not st_kv.get(key):
            missing.append(f"secrets/minds/ci/supertokens/{key}")
    if missing:
        raise MindError("Vault is missing required ci-tier credentials for the round-trip test: " + ", ".join(missing))
    return {
        "NEON_ORG_ID": neon_kv["NEON_ORG_ID"],
        "NEON_API_TOKEN": neon_kv["NEON_API_TOKEN"],
        "SUPERTOKENS_CONNECTION_URI": st_kv["SUPERTOKENS_CONNECTION_URI"],
        "SUPERTOKENS_API_KEY": st_kv["SUPERTOKENS_API_KEY"],
    }
