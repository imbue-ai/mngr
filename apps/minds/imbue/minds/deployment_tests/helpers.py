"""Helpers test bodies + fixtures share across the deployment_tests suite.

Lives next to ``data_types.py`` rather than inside ``conftest.py`` because
pytest treats ``conftest.py`` as an implementation detail (loaded by
auto-discovery, not normally imported from). Anything a test body
``imports`` should be in a regular module.
"""

import os
import re
import time
from datetime import datetime
from datetime import timezone
from typing import Final

import httpx
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.cli._activated_env import modal_profile_for_tier_or_none
from imbue.minds.cli._activated_env import tier_for_env_name
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError

_SUPERTOKENS_TENANT_ID: Final[str] = "public"
_SUPERTOKENS_ADMIN_TIMEOUT_SECONDS: Final[float] = 30.0
_ENV_READY_TIMEOUT_SECONDS: Final[float] = 60.0
_ENV_READY_POLL_INTERVAL_SECONDS: Final[float] = 1.0


def build_minds_env_subprocess_env(name: DevEnvName) -> dict[str, str]:
    """Build the env dict for a ``minds env deploy/destroy`` subprocess targeting ``name``.

    Mirrors what ``minds env activate <name>`` exports (without going
    through the print-shell-vars indirection): MINDS_ROOT_NAME, MNGR_HOST_DIR,
    MNGR_PREFIX, MINDS_CLIENT_CONFIG_PATH, and (for tiers with a
    committed ``modal_workspace``) MODAL_PROFILE. The MODAL_PROFILE
    lookup goes through the same ``modal_profile_for_tier_or_none``
    helper ``minds env activate`` itself uses, so a separated CI Modal
    workspace (planned) automatically lands here without having to
    update a test-only hardcoded constant.

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
        env["MODAL_PROFILE"] = modal_profile
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
