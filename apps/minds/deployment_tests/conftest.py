"""Pytest fixtures for the ``minds_deployment`` and ``minds_services`` suites.

Five fixtures, mirroring the spec:

* ``shared_env(role)`` -- a pre-stood-up dev env reachable by URL.
* ``fct_template_ref`` -- worktree path + (future) pushed ``ci-...`` branch
  ref for the FCT content under test.
* ``verified_user`` -- function-scoped, pre-verified user created via the
  shared env's SuperTokens admin API and deleted in teardown.
* ``ephemeral_env`` -- function-scoped, mints a fresh ``dev-ci-...`` env
  via ``minds env deploy`` and unconditionally tears it down in finally.
* ``signup_email`` -- function-scoped, fresh ``+<uuid>`` address against
  the per-run shared mail.tm account plus poll helpers.

Every fixture skips with a clear reason when the orchestrator-provided
config it needs is missing, so a stray ``pytest -k`` outside the
orchestrator does not crash hard. The expected invocation is always
``just minds-test-deployment`` (or one of its sibling recipes).
"""

import os
from collections.abc import Generator
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

import pytest
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests._mailtm import MailtmInbox
from imbue.minds.deployment_tests._mailtm import make_signup_address
from imbue.minds.deployment_tests.data_types import DeploymentEnvsConfig
from imbue.minds.deployment_tests.data_types import EphemeralEnvHandle
from imbue.minds.deployment_tests.data_types import FctTemplateRef
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.primitives import DEPLOYMENT_ENVS_JSON_ENV_VAR
from imbue.minds.deployment_tests.primitives import DeploymentTestConfigError
from imbue.minds.deployment_tests.primitives import MAILTM_ADDRESS_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_JWT_ENV_VAR
from imbue.minds.deployment_tests.primitives import MailtmAddress
from imbue.minds.deployment_tests.primitives import MailtmJwt
from imbue.minds.deployment_tests.primitives import SHARED_ENV_SECRET_ENV_VAR_PREFIX
from imbue.minds.deployment_tests.primitives import SharedEnvRole
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
        env_prefix = f"{SHARED_ENV_SECRET_ENV_VAR_PREFIX}{role.upper()}_"
        try:
            return SharedEnvHandle(
                urls=urls,
                supertokens_connection_uri=SecretStr(_require_env_var(f"{env_prefix}SUPERTOKENS_CONNECTION_URI")),
                supertokens_api_key=SecretStr(_require_env_var(f"{env_prefix}SUPERTOKENS_API_KEY")),
                neon_host_pool_dsn=SecretStr(_require_env_var(f"{env_prefix}NEON_HOST_POOL_DSN")),
                neon_litellm_dsn=SecretStr(_require_env_var(f"{env_prefix}NEON_LITELLM_DSN")),
            )
        except DeploymentTestConfigError as exc:
            pytest.skip(str(exc))

    return _get_shared_env


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
    user_id, session_token = _create_verified_user_via_admin_api(
        connection_uri=handle.supertokens_connection_uri,
        api_key=handle.supertokens_api_key,
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
            _delete_user_via_admin_api(
                connection_uri=handle.supertokens_connection_uri,
                api_key=handle.supertokens_api_key,
                user_id=user_id,
            )
        except MindError as exc:
            logger.warning(
                "Failed to delete verified-user fixture user {!r} ({}); the shared env's "
                "SuperTokens app teardown at run-end is the safety net.",
                email,
                exc,
            )


@pytest.fixture
def ephemeral_env(deployment_envs_config: DeploymentEnvsConfig) -> Generator[EphemeralEnvHandle, None, None]:
    """Function-scoped fresh ``dev-ci-<timestamp>-<uuid>`` env for ``minds_deployment`` tests.

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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DeploymentTestConfigError(
            f"Required env var {name!r} is unset. The orchestrator should populate it before "
            "invoking pytest; if you are running this test outside the orchestrator, that is the issue."
        )
    return value


def _mint_ephemeral_env_name() -> DevEnvName:
    """Build a ``dev-ci-<lowercased-timestamp>-<short-uuid>`` env name.

    Lowercased ``t`` / ``z`` because :class:`DevEnvName`'s validator
    enforces ``[a-z0-9][a-z0-9_-]{0,N}[a-z0-9]`` (the existing
    ``DeployId`` shape with uppercase ``T``/``Z`` is fine in Modal
    Secret names but not in dev env names). The ``dev-ci-`` prefix lets
    the name+age sweep target CI envs without colliding with
    developer-owned ``dev-josh`` / ``dev-alice`` envs.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    short = get_short_random_string()
    return DevEnvName(f"dev-ci-{stamp}-{short}")


def _deploy_ephemeral_env(*, name: DevEnvName, run_id: str) -> EphemeralEnvHandle:
    """Run ``uv run minds env deploy`` for a fresh dev env and return its URLs.

    Stub: real implementation needs to drive ``minds env activate``
    + ``minds env deploy``, parse the resulting client.toml, and append
    the env to the orchestrator ledger. All currently-shipped
    ``minds_deployment`` tests are skipped, so this stub is not yet
    exercised; iterating on it is the next implementation step.
    """
    pytest.skip(
        f"ephemeral_env provisioning for {name!r} (run {run_id!r}) is not implemented yet -- "
        "see specs/minds-deployment-tests.md."
    )


def _destroy_ephemeral_env(*, name: DevEnvName) -> None:
    """Run ``uv run minds env destroy`` for the named env. Idempotent against missing state.

    Stub today; the orchestrator-side name+age sweep is the safety net
    until this is wired up.
    """
    logger.warning(
        "ephemeral_env destroy for {!r} is not implemented yet; relying on the orchestrator's "
        "name+age sweep to reclaim the env later.",
        name,
    )


def _create_verified_user_via_admin_api(
    *,
    connection_uri: SecretStr,
    api_key: SecretStr,
    email: NonEmptyStr,
    password: SecretStr,
) -> tuple[NonEmptyStr, SecretStr]:
    """Call SuperTokens admin API: create user, mark email verified, mint session.

    Stub: real implementation needs the SuperTokens admin endpoints.
    Returns ``(user_id, session_token)``. Tests that use this fixture
    are skipped today, so this stub is unreached.
    """
    pytest.skip(
        f"verified_user provisioning for {email!r} is not implemented yet -- see specs/minds-deployment-tests.md."
    )


def _delete_user_via_admin_api(*, connection_uri: SecretStr, api_key: SecretStr, user_id: NonEmptyStr) -> None:
    """Call SuperTokens admin API: delete a user by id (stub today)."""
    logger.warning(
        "verified_user teardown for user_id={!r} is not implemented yet; relying on shared env's "
        "SuperTokens app teardown at run end to reclaim the user.",
        user_id,
    )
