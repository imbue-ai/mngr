"""Pytest fixtures for the ``minds_deployment`` and ``minds_services`` suites.

Five fixtures, mirroring the spec:

* ``shared_env(role)`` -- a pre-stood-up ci env reachable by URL.
* ``fct_template_ref`` -- worktree path + (future) pushed ``ci-...`` branch
  ref for the FCT content under test.
* ``verified_user`` -- function-scoped, pre-verified user created via the
  shared env's SuperTokens admin API and deleted in teardown.
* ``ephemeral_env`` -- function-scoped, mints a fresh ``ci-...`` env
  via ``minds env deploy`` and unconditionally tears it down in finally.
* ``signup_email`` -- function-scoped, fresh ``+<uuid>`` address against
  the per-run shared mail.tm account plus poll helpers.

Every fixture skips with a clear reason when the orchestrator-provided
config it needs is missing, so a stray ``pytest -k`` outside the
orchestrator does not crash hard. The expected invocation is always
``just minds-test-deployment`` (or one of its sibling recipes).
"""

import os
import pwd
import re
import shutil
import subprocess
from collections.abc import Callable
from collections.abc import Generator
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

import httpx
import pytest
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.config.loader import load_client_config
from imbue.minds.deployment_tests._mailtm import MailtmInbox
from imbue.minds.deployment_tests._mailtm import make_signup_address
from imbue.minds.deployment_tests.data_types import DeploymentEnvsConfig
from imbue.minds.deployment_tests.data_types import EphemeralEnvHandle
from imbue.minds.deployment_tests.data_types import FctTemplateRef
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import CI_TEST_USER_EMAIL_KEY
from imbue.minds.deployment_tests.helpers import CI_TEST_USER_PASSWORD_KEY
from imbue.minds.deployment_tests.helpers import SHARED_ENV_SECRET_KEYS
from imbue.minds.deployment_tests.helpers import build_minds_env_subprocess_env
from imbue.minds.deployment_tests.helpers import create_verified_user_via_admin_api
from imbue.minds.deployment_tests.helpers import delete_user_via_admin_api
from imbue.minds.deployment_tests.helpers import read_ci_test_user_credentials
from imbue.minds.deployment_tests.helpers import read_shared_env_secrets
from imbue.minds.deployment_tests.helpers import resolve_ci_run_key
from imbue.minds.deployment_tests.helpers import sweep_stale_users
from imbue.minds.deployment_tests.primitives import DEPLOYMENT_ENVS_JSON_ENV_VAR
from imbue.minds.deployment_tests.primitives import DeploymentTestConfigError
from imbue.minds.deployment_tests.primitives import MAILTM_ADDRESS_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_JWT_ENV_VAR
from imbue.minds.deployment_tests.primitives import MailtmAddress
from imbue.minds.deployment_tests.primitives import MailtmJwt
from imbue.minds.deployment_tests.primitives import SHARED_ENV_SECRET_ENV_VAR_PREFIX
from imbue.minds.deployment_tests.primitives import SharedEnvRole
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError
from imbue.mngr.utils.testing import get_short_random_string


@pytest.fixture(scope="session")
def deployment_envs_config() -> DeploymentEnvsConfig:
    """Load the orchestrator-provided per-run config from disk.

    Skips the test if ``MINDS_DEPLOYMENT_TEST_ENVS_JSON`` is unset, since
    that means the test was collected outside the orchestrator (e.g. via
    a stray ``uv run pytest``).
    """
    raw_path = os.environ.get(DEPLOYMENT_ENVS_JSON_ENV_VAR)
    if not raw_path:
        pytest.skip(
            f"{DEPLOYMENT_ENVS_JSON_ENV_VAR} is not set -- run this test via "
            "`just minds-test-deployment` (or one of its sibling recipes), not via plain `pytest`."
        )
    config_path = Path(raw_path)
    if not config_path.is_file():
        raise DeploymentTestConfigError(
            f"{DEPLOYMENT_ENVS_JSON_ENV_VAR}={raw_path!r} but no file exists at that path. "
            "Re-run via the orchestrator."
        )
    return DeploymentEnvsConfig.model_validate_json(config_path.read_text())


@pytest.fixture(scope="session")
def fct_template_ref(deployment_envs_config: DeploymentEnvsConfig) -> FctTemplateRef:
    """Return the FCT template ref the orchestrator prepared for this run.

    Today the orchestrator populates ``worktree_path`` so tests pass a
    local-disk path to ``mngr create --template <path>``. When this moves
    to offload the same fixture will return the pushed-branch form
    instead -- the fixture is the abstraction boundary, test code does
    not change.
    """
    return deployment_envs_config.fct


@pytest.fixture
def shared_env(
    deployment_envs_config: DeploymentEnvsConfig,
) -> Callable[[str], SharedEnvHandle]:
    """Factory: ``shared_env('default')`` returns a :class:`SharedEnvHandle` for that role.

    Skips with a clear reason if the role is not configured by the
    orchestrator. Reads per-env secrets from env vars named
    ``MINDS_DEPLOYMENT_TEST_SHARED_<ROLE_UPPER>_<KEY>``.
    """

    def _get_shared_env(role: str) -> SharedEnvHandle:
        role_key = SharedEnvRole(role)
        if role_key not in deployment_envs_config.shared_envs:
            pytest.skip(
                f"Shared env role {role!r} is not configured for this run. "
                f"Configured roles: {sorted(deployment_envs_config.shared_envs.keys())!r}."
            )
        urls = deployment_envs_config.shared_envs[role_key]
        secrets = _resolve_shared_env_secrets(role=role_key, run_key=resolve_ci_run_key(deployment_envs_config.run_id))
        return SharedEnvHandle(
            urls=urls,
            supertokens_connection_uri=SecretStr(secrets["SUPERTOKENS_CONNECTION_URI"]),
            supertokens_api_key=SecretStr(secrets["SUPERTOKENS_API_KEY"]),
            neon_host_pool_dsn=SecretStr(secrets["NEON_HOST_POOL_DSN"]),
            neon_litellm_dsn=SecretStr(secrets["NEON_LITELLM_DSN"]),
        )

    return _get_shared_env


@pytest.fixture
def ci_test_user() -> tuple[NonEmptyStr, SecretStr]:
    """Fixed ``(email, password)`` for the CI test user the env-build step created.

    Reads the credentials from env vars (set by the orchestrator / CI job) when
    present -- the only path available inside an offload sandbox -- and otherwise
    falls back to reading them from Vault (local runs, where the operator has a
    Vault token).
    """
    email = os.environ.get(CI_TEST_USER_EMAIL_KEY)
    password = os.environ.get(CI_TEST_USER_PASSWORD_KEY)
    if email and password:
        return NonEmptyStr(email), SecretStr(password)
    try:
        return read_ci_test_user_credentials()
    except MindError as exc:
        pytest.skip(f"CI test-user credentials not in env vars and Vault read failed: {exc}")


@pytest.fixture
def verified_user(
    shared_env: Callable[[str], SharedEnvHandle],
) -> Generator[VerifiedUserHandle, None, None]:
    """Function-scoped pre-verified user against the ``default`` shared env's SuperTokens.

    Implementation calls the env's SuperTokens admin API to create the
    user + mark the email verified + mint a session token; teardown
    deletes the user. Tests that need a user against a different shared
    env should call ``shared_env('<other-role>')`` themselves and invoke
    the same provisioning code directly (or we add a second fixture
    parametrized on role once that need actually materializes).
    """
    handle = shared_env("default")
    email = NonEmptyStr(f"test-{get_short_random_string()}@example.test")
    password = SecretStr(f"pw-{uuid4().hex}")
    user_id, session_token = create_verified_user_via_admin_api(
        connection_uri=handle.supertokens_connection_uri,
        api_key=handle.supertokens_api_key,
        connector_url=handle.urls.connector_url,
        email=email,
        password=password,
    )
    try:
        yield VerifiedUserHandle(
            email=email,
            password=password,
            supertokens_user_id=user_id,
            session_token=session_token,
        )
    finally:
        try:
            delete_user_via_admin_api(
                connection_uri=handle.supertokens_connection_uri,
                api_key=handle.supertokens_api_key,
                user_id=user_id,
            )
        except (MindError, httpx.HTTPError) as exc:
            logger.warning(
                "Failed to delete verified-user fixture user {!r} ({}); the session-scoped "
                "sweep + the shared env's SuperTokens app teardown at run-end are the safety nets.",
                email,
                exc,
            )


@pytest.fixture
def ephemeral_env(deployment_envs_config: DeploymentEnvsConfig) -> Generator[EphemeralEnvHandle, None, None]:
    """Function-scoped fresh ``ci-<timestamp>-<uuid>`` env for ``minds_deployment`` tests.

    Shells out to ``minds env deploy`` (matching how an operator would
    invoke it) and unconditionally tears down via ``minds env destroy``
    in finally. The orchestrator-side name+age sweep is the leak safety
    net if both this teardown AND the orchestrator's per-run cleanup
    fail.
    """
    name = _mint_ephemeral_env_name()
    handle = _deploy_ephemeral_env(name=name, run_id=str(deployment_envs_config.run_id))
    try:
        yield handle
    finally:
        try:
            _destroy_ephemeral_env(name=name)
        except MindError as exc:
            logger.warning(
                "Failed to destroy ephemeral env {!r} in teardown ({}); the orchestrator's "
                "per-run ledger cleanup + name+age sweep are the safety nets.",
                name,
                exc,
            )


@pytest.fixture
def signup_email() -> Generator[MailtmInbox, None, None]:
    """Fresh ``+<uuid>`` mail.tm address scoped to this test.

    Returns a :class:`MailtmInbox` rooted at
    ``<runner-account-local>+<test-uuid>@<runner-account-domain>``;
    use it to fetch the verification token and one-time login code.
    The mail.tm account itself is shared across the whole run (created
    by the orchestrator, torn down at the end).
    """
    try:
        account_address = MailtmAddress(_require_env_var(MAILTM_ADDRESS_ENV_VAR))
        # Validate the JWT as a non-empty primitive before wrapping it in SecretStr
        # so a stray empty env var still surfaces as a clear DeploymentTestConfigError
        # rather than a SecretStr-around-empty-string that would only fail later.
        jwt = SecretStr(MailtmJwt(_require_env_var(MAILTM_JWT_ENV_VAR)))
    except DeploymentTestConfigError as exc:
        pytest.skip(str(exc))
    address = make_signup_address(account_address, suffix=get_short_random_string())
    yield MailtmInbox(address=address, account_address=account_address, jwt=jwt)


# Email-pattern + age threshold the session-scoped sweep uses to delete
# leftover test users from a prior crashed run. Bounded conservatively:
# 30 minutes is well beyond any single test run, so the sweep cannot
# delete a user a concurrent run is actively using.
_STALE_TEST_USER_EMAIL_PATTERN = re.compile(r"^test-[0-9a-f]+@example\.test$")
_STALE_TEST_USER_MAX_AGE_SECONDS = 30 * 60


# Dotfiles the deployment_tests subprocesses need from the operator's
# real HOME. The project-wide ``setup_test_mngr_env`` autouse re-points
# HOME at a per-test tmpdir; each entry here is copied from the real
# HOME into the tmpdir HOME before the test body runs so the subprocess
# CLIs (``vault``, ``modal``) find their auth files at the expected
# paths under the redirected HOME.
#
# - ``.vault-token``: HashiCorp Vault CLI auth token. ``minds env
#   deploy`` calls ``_load_dev_credentials_from_vault`` which shells out
#   to ``vault`` and expects this file.
# - ``.modal.toml``: Modal CLI auth tokens per workspace.
#   ``modal deploy`` / ``modal app history`` / ``modal app rollback``
#   read this file to pick the workspace's API tokens.
_OPERATOR_DOTFILES_TO_COPY: Final[tuple[str, ...]] = (".vault-token", ".modal.toml")

# Captured at module-import time (before any pytest fixture runs) via
# pwd.getpwuid, which reads /etc/passwd directly -- so it stays correct
# even after the autouse fixture re-points ``$HOME`` at a tmpdir.
_OPERATOR_REAL_HOME: Final[Path] = Path(pwd.getpwuid(os.getuid()).pw_dir)


@pytest.fixture(autouse=True)
def _copy_operator_credentials_into_test_home(
    setup_test_mngr_env: None,
) -> Generator[None, None, None]:
    """Copy ``~/.vault-token`` + ``~/.modal.toml`` from the operator's real HOME into the test HOME.

    The project-wide ``setup_test_mngr_env`` autouse fixture (from
    ``imbue.mngr.utils.plugin_testing``) re-points ``$HOME`` at a
    per-test tmpdir for filesystem isolation, which we generally want
    -- it keeps any test-driven file writes from landing in the
    operator's real home. The downside is that the in-test subprocess
    ``minds env deploy`` shells out to ``vault`` (reads
    ``$HOME/.vault-token``) and ``modal`` (reads ``$HOME/.modal.toml``)
    which then find empty / missing auth files and fail with 403s.

    This fixture depends on ``setup_test_mngr_env`` as a parameter so
    it runs AFTER the HOME re-point; it then copies the named dotfiles
    from the operator's pre-captured real HOME into the new HOME so the
    subprocesses succeed. The per-test tmpdir is deleted at teardown
    by pytest's ``tmp_path`` machinery so no operator-credential copies
    leak past the test.
    """
    _ = setup_test_mngr_env
    new_home = Path(os.environ["HOME"])
    # Defensive: if the upstream override didn't actually fire, skip
    # the copies -- the operator's real dotfiles are already in place.
    if new_home.resolve() != _OPERATOR_REAL_HOME.resolve():
        for relpath in _OPERATOR_DOTFILES_TO_COPY:
            src = _OPERATOR_REAL_HOME / relpath
            if src.is_file():
                shutil.copy2(src, new_home / relpath)
    yield


@pytest.fixture(scope="session", autouse=True)
def _sweep_stale_test_users(deployment_envs_config: DeploymentEnvsConfig) -> None:
    """Once per pytest session, delete any leftover ``test-*@example.test`` users.

    Defensive cleanup against state leaked by a prior run that crashed
    before its ``verified_user`` teardown fired. Only deletes users
    older than ``_STALE_TEST_USER_MAX_AGE_SECONDS`` so a concurrent run
    creating its own users right now is safe.

    Skipped silently for runs that don't have a ``default`` shared env
    configured (e.g. a ``minds_deployment``-only run, once those tests
    exist) -- those tests don't use ``verified_user`` so there's
    nothing to sweep against.
    """
    default_role = SharedEnvRole("default")
    if default_role not in deployment_envs_config.shared_envs:
        return
    try:
        secrets = _resolve_shared_env_secrets(
            role=default_role, run_key=resolve_ci_run_key(deployment_envs_config.run_id)
        )
    except (MindError, pytest.skip.Exception) as exc:
        logger.warning("Skipping stale-test-user sweep: {}", exc)
        return
    connection_uri = SecretStr(secrets["SUPERTOKENS_CONNECTION_URI"])
    api_key = SecretStr(secrets["SUPERTOKENS_API_KEY"])
    deleted = sweep_stale_users(
        connection_uri=connection_uri,
        api_key=api_key,
        email_pattern=_STALE_TEST_USER_EMAIL_PATTERN,
        max_age_seconds=_STALE_TEST_USER_MAX_AGE_SECONDS,
    )
    if deleted:
        logger.info("Stale-test-user sweep deleted {} user(s)", deleted)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DeploymentTestConfigError(
            f"Required env var {name!r} is unset. The orchestrator should populate it before "
            "invoking pytest; if you are running this test outside the orchestrator, that is the issue."
        )
    return value


def _resolve_shared_env_secrets(*, role: SharedEnvRole, run_key: str) -> dict[str, str]:
    """Return the per-env secrets, preferring injected env vars over a Vault read.

    Env vars (``MINDS_DEPLOYMENT_TEST_SHARED_<ROLE>_<KEY>``) are the only path
    available inside an offload sandbox, so they win when fully present. Local
    runs that did not inject them fall back to reading the per-run Vault path
    the env-build step wrote (the operator's Vault token is available there).
    """
    env_prefix = f"{SHARED_ENV_SECRET_ENV_VAR_PREFIX}{str(role).upper()}_"
    from_env = [os.environ.get(f"{env_prefix}{key}") for key in SHARED_ENV_SECRET_KEYS]
    if all(from_env):
        return {key: value for key, value in zip(SHARED_ENV_SECRET_KEYS, from_env, strict=False) if value is not None}
    try:
        return read_shared_env_secrets(run_key=run_key, role=role)
    except MindError as exc:
        pytest.skip(f"Shared env secrets for role {role!r} not in env vars and Vault read failed: {exc}")


def _mint_ephemeral_env_name() -> DevEnvName:
    """Build a ``ci-<lowercased-timestamp>-<short-uuid>`` env name.

    Lowercased ``t`` / ``z`` because :class:`DevEnvName`'s validator
    enforces ``[a-z0-9][a-z0-9_-]{0,N}[a-z0-9]`` (the existing
    ``DeployId`` shape with uppercase ``T``/``Z`` is fine in Modal
    Secret names but not in env names). The ``ci-`` prefix routes the
    name to the CI tier (see :func:`tier_for_env_name`) and lets the
    name+age sweep target CI envs without colliding with developer-
    owned ``dev-josh`` / ``dev-alice`` envs.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    short = get_short_random_string()
    return DevEnvName(f"ci-{stamp}-{short}")


_MINDS_DEPLOY_TIMEOUT_SECONDS = 15 * 60
_MINDS_DESTROY_TIMEOUT_SECONDS = 10 * 60
# ``minds env deploy/destroy`` validate that they're being run from
# inside the monorepo (they write a ``.minds-deploy-recover-target-<env>.json``
# at the repo root). Pytest changes cwd to a tmpdir for each test, so
# the subprocess inherits that tmpdir and would fail the check. Pin
# cwd to the repo root explicitly.
_REPO_ROOT_FOR_SUBPROCESS = Path(__file__).resolve().parents[3]


def _deploy_ephemeral_env(*, name: DevEnvName, run_id: str) -> EphemeralEnvHandle:
    """``mkdir -p <env-root>`` + ``uv run minds env deploy``; parse client.toml; return handle.

    Shells out to the same ``minds env deploy`` CLI an operator would
    run, with the activation env vars set (so the subprocess targets
    ``<name>`` without needing a prior ``eval activate``). Captures
    output to the test's stdout via ``check_output``. On failure,
    surfaces stdout/stderr in the raised exception so the test author
    can see what went wrong without scraping pytest logs.

    The ``run_id`` is unused at the function-arg level today (the env
    name already encodes it) but kept in the signature so future code
    that ledgers the ephemeral env can stamp the run id without a
    plumbing change.
    """
    _ = run_id
    target = env_root_dir(name)
    target.mkdir(parents=True, exist_ok=True)
    sub_env = build_minds_env_subprocess_env(name)
    logger.info("ephemeral_env: deploying {!r}", name)
    completed = subprocess.run(
        ["uv", "run", "minds", "env", "deploy"],
        env=sub_env,
        cwd=str(_REPO_ROOT_FOR_SUBPROCESS),
        capture_output=True,
        text=True,
        timeout=_MINDS_DEPLOY_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise MindError(
            f"`minds env deploy` for {name!r} exited {completed.returncode}.\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    client_toml = client_config_file(name)
    if not client_toml.is_file():
        raise MindError(
            f"`minds env deploy` for {name!r} completed but did not write {client_toml}. "
            "This usually means the deploy succeeded the modal side but failed the local-state write step."
        )
    client_config = load_client_config(client_toml)
    return EphemeralEnvHandle(
        name=name,
        connector_url=client_config.connector_url,
        litellm_proxy_url=client_config.litellm_proxy_url,
    )


def _destroy_ephemeral_env(*, name: DevEnvName) -> None:
    """``uv run minds env destroy`` for ``name``. Idempotent against missing env root.

    Returns silently if the env root doesn't exist (already destroyed
    or never created). Otherwise shells out to ``minds env destroy``
    which is itself idempotent per-resource. Any non-zero exit raises
    so the caller can log + log a leak warning.
    """
    if not env_root_dir(name).is_dir():
        logger.info("ephemeral_env: {!r} has no env root on disk -- destroy is a no-op", name)
        return
    sub_env = build_minds_env_subprocess_env(name)
    logger.info("ephemeral_env: destroying {!r}", name)
    completed = subprocess.run(
        ["uv", "run", "minds", "env", "destroy"],
        env=sub_env,
        cwd=str(_REPO_ROOT_FOR_SUBPROCESS),
        capture_output=True,
        text=True,
        timeout=_MINDS_DESTROY_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise MindError(
            f"`minds env destroy` for {name!r} exited {completed.returncode}.\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
