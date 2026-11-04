"""Primitives, env-var contract, and Vault layout of the Imbue Studio smoke suite.

Smoke tests run after a deploy or a release against the published artifact and
the deployed environment (``apps/minds/docs/testing-overview.md`` section 1.10).
Everything a smoke run needs is passed in through the environment, so the same
suite runs from a CI job and from ``just minds-smoke`` with nothing placed by
hand:

* which artifact and which backend -- the tier, the release channel whose
  published build is under test, or an explicit binary / the dev checkout;
* which accounts to act as -- the tier's persistent account and the run's
  throwaway account;
* where the run records itself -- its run id and its resource manifest.

The two Vault directories are functions rather than literals so the
persistent-account path and the per-run handoff path exist in one place. The
roles and policies that grant them live in the imbue-ai/vault terraform repo.
"""

import re
from typing import Final
from typing import Self

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.minds.deployment_tests.primitives import MAILTM_ADDRESS_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_JWT_ENV_VAR
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.errors import MindError

# Which backend to test against: ``production``, ``staging``, or a dev env name.
SMOKE_TIER_ENV_VAR: Final[str] = "MINDS_SMOKE_TIER"
# Which release channel's published build to test (``alpha`` / ``beta`` / ``stable``).
SMOKE_CHANNEL_ENV_VAR: Final[str] = "MINDS_SMOKE_CHANNEL"
# An explicit Imbue Studio executable to test, which overrides the channel.
SMOKE_BINARY_PATH_ENV_VAR: Final[str] = "MINDS_SMOKE_BINARY_PATH"
# ``1`` runs the dev checkout's Electron instead of a published build.
SMOKE_DEV_CHECKOUT_ENV_VAR: Final[str] = "MINDS_SMOKE_DEV_CHECKOUT"
SMOKE_DEV_CHECKOUT_ENABLED_VALUE: Final[str] = "1"

# The tier's persistent account.
SMOKE_ACCOUNT_EMAIL_ENV_VAR: Final[str] = "MINDS_SMOKE_ACCOUNT_EMAIL"
SMOKE_ACCOUNT_PASSWORD_ENV_VAR: Final[str] = "MINDS_SMOKE_ACCOUNT_PASSWORD"

# The run's throwaway account, minted by ``setup`` and deleted by ``teardown``.
SMOKE_THROWAWAY_EMAIL_ENV_VAR: Final[str] = "MINDS_SMOKE_THROWAWAY_EMAIL"
SMOKE_THROWAWAY_PASSWORD_ENV_VAR: Final[str] = "MINDS_SMOKE_THROWAWAY_PASSWORD"
SMOKE_THROWAWAY_USER_ID_ENV_VAR: Final[str] = "MINDS_SMOKE_THROWAWAY_USER_ID"

# The id tying one run's resources, its Vault handoff, and its manifest together.
SMOKE_RUN_ID_ENV_VAR: Final[str] = "MINDS_SMOKE_RUN_ID"
# The handoff state file ``smoke_tests.py setup --out`` wrote, for a local run of the suite.
SMOKE_HANDOFF_FILE_ENV_VAR: Final[str] = "MINDS_SMOKE_HANDOFF_FILE"
# Where the test job appends the resources it creates, for teardown to read.
SMOKE_MANIFEST_PATH_ENV_VAR: Final[str] = "MINDS_SMOKE_MANIFEST_PATH"

# The mail.tm mailbox the throwaway account verifies its email through. The
# address and JWT keep the names the deployment tests already thread through
# pytest; the account id (which only teardown needs, to delete the mailbox)
# follows the same spelling.
MAILTM_ACCOUNT_ID_ENV_VAR: Final[str] = "MAILTM_ACCOUNT_ID"

# Where ``minds-admin env activate`` says the activated env's client.toml lives.
# Read for envs whose config is not committed to the repo (dev and ci envs).
ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR: Final[str] = "MINDS_CLIENT_CONFIG_PATH"

# Every account the suite touches carries this prefix, so a leftover is
# unmistakable to an operator and the stale sweep can find it by address alone.
SMOKE_EMAIL_PREFIX: Final[str] = "studio-smoke-"
# The tier's persistent account. Its address is fixed (one per tier, created
# once by the operator), and the stale sweep never deletes it.
PERSISTENT_ACCOUNT_EMAIL: Final[str] = "studio-smoke-persistent@example.test"
# The ally plan is the only one with an LLM budget, which the journey needs.
PERSISTENT_ACCOUNT_PLAN: Final[str] = "ally"
# The throwaway account only completes a signup, so it needs no budget.
THROWAWAY_ACCOUNT_PLAN: Final[str] = "free"

# The persistent account's leaves under ``persistent_account_vault_dir``.
PERSISTENT_ACCOUNT_EMAIL_VAULT_KEY: Final[str] = "SMOKE_ACCOUNT_EMAIL"
PERSISTENT_ACCOUNT_PASSWORD_VAULT_KEY: Final[str] = "SMOKE_ACCOUNT_PASSWORD"

# The throwaway account's leaves under ``run_handoff_vault_dir``. The mailbox's
# leaves are named after the env vars above, so the test job can export every
# leaf verbatim.
THROWAWAY_ACCOUNT_EMAIL_VAULT_KEY: Final[str] = "THROWAWAY_ACCOUNT_EMAIL"
THROWAWAY_ACCOUNT_PASSWORD_VAULT_KEY: Final[str] = "THROWAWAY_ACCOUNT_PASSWORD"
THROWAWAY_ACCOUNT_USER_ID_VAULT_KEY: Final[str] = "THROWAWAY_ACCOUNT_USER_ID"
MAILTM_ACCOUNT_ADDRESS_VAULT_KEY: Final[str] = MAILTM_ADDRESS_ENV_VAR
MAILTM_ACCOUNT_JWT_VAULT_KEY: Final[str] = MAILTM_JWT_ENV_VAR
MAILTM_ACCOUNT_ID_VAULT_KEY: Final[str] = MAILTM_ACCOUNT_ID_ENV_VAR

_VAULT_MINDS_ROOT: Final[str] = "secrets/minds"
_SMOKE_RUN_ID_PATTERN: Final[str] = r"[a-z0-9-]+"


class SmokeConfigError(MindError):
    """Raised when a smoke run's env-var contract, run id, or target config is unusable."""

    ...


class SmokeAccountError(MindError):
    """Raised when a smoke account cannot be minted, resolved, signed in, or deleted."""

    ...


class SmokeAccountExistsError(SmokeAccountError):
    """Raised when an account already exists at the address a smoke command tried to mint.

    Its own type because the persistent-account command treats it as "already
    created, prove the stored credentials" rather than as a failure.
    """

    ...


class SmokeSignInRefusedError(SmokeAccountError):
    """Raised when the connector refuses a smoke account's email and password.

    Its own type because a teardown reads it as "there is no account to act as
    any more" -- the throwaway account of a run that was already torn down --
    which is not a cleanup failure.
    """

    ...


class SmokeTeardownError(MindError):
    """Raised when a teardown step cannot complete (the step is still recorded in the report)."""

    ...


class SmokeManifestError(MindError):
    """Raised when a release-feed manifest the suite fetched does not have the expected shape."""

    ...


class SmokeAppError(MindError):
    """Raised when the launched Imbue Studio app does not reach the state a journey step waits for."""

    ...


class ReleaseChannelName(NonEmptyStr):
    """A release channel the update feed publishes (``stable``, ``beta``, ``alpha``)."""

    ...


class ReleasePlatformName(NonEmptyStr):
    """A desktop platform the update feed publishes a manifest for (``mac``, ``linux``)."""

    ...


class SmokeRunId(NonEmptyStr):
    """The id tying one smoke run's resources, Vault handoff, and manifest together.

    Lowercase letters, digits and ``-`` only: the id lands in a Vault path, in
    a mail.tm local part, and in the smoke accounts' addresses, none of which
    tolerate arbitrary text. A GitHub run id and a compact UTC timestamp both
    fit as-is.
    """

    def __new__(cls, value: str) -> Self:
        stripped = value.strip()
        if not re.fullmatch(_SMOKE_RUN_ID_PATTERN, stripped):
            raise SmokeConfigError(
                f"Invalid smoke run id {value!r}: must match {_SMOKE_RUN_ID_PATTERN!r} "
                "(lowercase letters, digits and '-'). Example: '20261014t101500z' or a GitHub run id."
            )
        return super().__new__(cls, stripped)


@pure
def persistent_account_vault_dir(tier: str) -> VaultPath:
    """``secrets/minds/<tier>/smoke``: the tier's persistent smoke account credentials."""
    return VaultPath(f"{_VAULT_MINDS_ROOT}/{tier}/smoke")


@pure
def run_handoff_vault_dir(run_id: SmokeRunId) -> VaultPath:
    """``secrets/minds/ci/smoke-runs/<run_id>``: one run's setup-to-test-to-teardown handoff.

    Under the ci tier rather than the tested tier because the handoff belongs
    to the CI run, not to the environment under test: the setup job writes it,
    the test job reads it, and the teardown job deletes it.
    """
    return VaultPath(f"{_VAULT_MINDS_ROOT}/ci/smoke-runs/{run_id}")
