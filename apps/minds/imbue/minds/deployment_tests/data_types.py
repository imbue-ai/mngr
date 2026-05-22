"""Frozen data types passed between the orchestrator and the test fixtures.

The orchestrator writes a :class:`DeploymentEnvsConfig` to a JSON file
(path exported via ``MINDS_DEPLOYMENT_TEST_ENVS_JSON``); the conftest
fixtures load it on demand and hand pieces of it to tests. Secrets are
not in this file -- they live in env vars (see ``primitives.py``).
"""

from pathlib import Path

from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests.primitives import RunId
from imbue.minds.deployment_tests.primitives import SharedEnvRole
from imbue.minds.envs.primitives import DevEnvName


class SharedEnvUrls(FrozenModel):
    """Public URLs for one shared env, keyed by role in :class:`DeploymentEnvsConfig`.

    Secrets (Neon DSNs, SuperTokens admin key) for the same env live in
    env vars prefixed ``MINDS_DEPLOYMENT_TEST_SHARED_<ROLE_UPPER>_`` so
    they never land on disk in the test-results dir.
    """

    role: SharedEnvRole = Field(description="The role name this env serves (e.g. 'default').")
    env_name: DevEnvName = Field(description="The actual env name on disk (e.g. 'ci-20260518t140212z').")
    connector_url: AnyUrl = Field(description="Base URL of the deployed ``remote_service_connector`` for this env.")
    litellm_proxy_url: AnyUrl = Field(description="Base URL of the deployed ``litellm`` proxy for this env.")


class FctTemplateRef(FrozenModel):
    """How the test should reach the forever-claude-template content under test.

    Today (local pytest): ``worktree_path`` is set, ``test_branch`` /
    ``test_remote`` may or may not be populated (the orchestrator can do
    the push as a forward-compat rehearsal, but tests do not use it
    yet -- see :meth:`as_mngr_template_arg`).

    Future (offload): ``worktree_path`` will be ``None`` and the fixture
    will return the ``test_remote#test_branch`` form so sandboxes can
    clone the pushed branch directly. Same fixture, same field, same
    test code.
    """

    worktree_path: Path | None = Field(
        default=None,
        description=(
            "Absolute path to ``<monorepo>/.external_worktrees/forever-claude-template/``. "
            "Set when running locally; ``None`` when running in offload sandboxes."
        ),
    )
    test_branch: NonEmptyStr | None = Field(
        default=None,
        description="Name of the ``ci-<timestamp>`` branch the orchestrator pushed to the FCT remote.",
    )
    test_remote: NonEmptyStr | None = Field(
        default=None,
        description="URL of the FCT remote the test branch was pushed to.",
    )

    def as_mngr_template_arg(self) -> str:
        """Return the value to pass to ``mngr create --template <value>``.

        Prefers the local worktree path when available (faster, no
        clone). Falls back to the pushed-branch ref. Raises
        :class:`AssertionError` if neither is populated, since the
        orchestrator guarantees at least one.
        """
        if self.worktree_path is not None:
            return str(self.worktree_path)
        assert self.test_branch is not None and self.test_remote is not None, (
            "FctTemplateRef has neither a local worktree path nor a pushed branch ref; "
            "the orchestrator should always populate at least one."
        )
        return f"{self.test_remote}#{self.test_branch}"


class DeploymentEnvsConfig(FrozenModel):
    """The full JSON blob the orchestrator writes for each pytest invocation.

    Loaded on demand by the conftest fixtures (path comes from
    ``MINDS_DEPLOYMENT_TEST_ENVS_JSON``). Pydantic ``extra='forbid'`` so
    a stale field name surfaces as a parse error, not silent drop.
    """

    shared_envs: dict[SharedEnvRole, SharedEnvUrls] = Field(
        description="Role -> shared env URLs. Empty when the orchestrator was invoked in a mode that needs no shared env."
    )
    fct: FctTemplateRef = Field(
        description="How tests can reach the forever-claude-template content the orchestrator prepared."
    )
    run_id: RunId = Field(description="The run id stamped into every CI-created resource this run.")


class SharedEnvHandle(FrozenModel):
    """What the ``shared_env(role=...)`` fixture returns to a test.

    Combines the URLs from :class:`SharedEnvUrls` with the per-env secrets
    the orchestrator threaded in via env vars.
    """

    urls: SharedEnvUrls
    supertokens_connection_uri: SecretStr
    supertokens_api_key: SecretStr
    neon_host_pool_dsn: SecretStr
    neon_litellm_dsn: SecretStr


class VerifiedUserHandle(FrozenModel):
    """What the ``verified_user`` fixture returns to a test.

    A pre-verified user provisioned via the shared env's SuperTokens
    admin API. The fixture deletes the user in teardown.
    """

    email: NonEmptyStr
    password: SecretStr
    supertokens_user_id: NonEmptyStr
    session_token: SecretStr


class EphemeralEnvHandle(FrozenModel):
    """What the ``ephemeral_env`` fixture yields to a test.

    The fixture shells out to ``minds env deploy`` for a fresh
    ``ci-<timestamp>-<short-uuid>`` env, yields this handle, then
    unconditionally tears the env down (idempotent against an
    already-destroyed env, so a test that destroys the env itself does
    not double-destroy).
    """

    name: DevEnvName = Field(description="The env name the orchestrator-side deploy minted.")
    connector_url: AnyUrl
    litellm_proxy_url: AnyUrl
