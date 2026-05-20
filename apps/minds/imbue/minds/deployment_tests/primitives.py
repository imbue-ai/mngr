"""Primitives + errors for the minds deployment / services test suite.

Kept small and focused: a strongly-typed handle on the run id the
orchestrator stamps into every CI-created resource, the role name for a
shared dev env, and the email-shaped types the signup test threads
through mail.tm.
"""

from typing import Final

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.errors import MindError

# ``MINDS_DEPLOYMENT_TEST_ENVS_JSON`` -- env var the orchestrator sets to the
# absolute path of the per-run ``deployment_envs.json`` file. The fixtures
# read this on demand; tests never touch it directly.
DEPLOYMENT_ENVS_JSON_ENV_VAR: Final[str] = "MINDS_DEPLOYMENT_TEST_ENVS_JSON"

# Per-run mail.tm test account credentials. The orchestrator creates one
# disposable mail.tm account per run and exports these so signup-flow tests
# can poll the same shared mailbox using ``+<uuid>`` local-part suffixes.
MAILTM_ADDRESS_ENV_VAR: Final[str] = "MAILTM_ACCOUNT_ADDRESS"
MAILTM_JWT_ENV_VAR: Final[str] = "MAILTM_ACCOUNT_JWT"

# Per-shared-env secret env var prefix. The full name is
# ``MINDS_DEPLOYMENT_TEST_SHARED_<ROLE_UPPER>_<KEY>``. Keeps secrets out of
# the on-disk deployment_envs.json file (which carries only URLs).
SHARED_ENV_SECRET_ENV_VAR_PREFIX: Final[str] = "MINDS_DEPLOYMENT_TEST_SHARED_"


class RunId(NonEmptyStr):
    """Identifier the orchestrator stamps into every CI-created resource for a run.

    Compact ISO 8601 UTC, ``YYYYMMDDtHHMMSSz`` (lowercase t/z so the value
    can be embedded in a ``DevEnvName`` whose regex is lowercase-only).
    """

    ...


class SharedEnvRole(NonEmptyStr):
    """Name of a shared dev env role configured by the orchestrator.

    Initial roster is a single ``default`` role; the structure supports any
    number even though only one is shipped now.
    """

    ...


class MailtmAddress(NonEmptyStr):
    """Full mail.tm address of the per-run disposable account."""

    ...


class MailtmJwt(NonEmptyStr):
    """JWT for the per-run mail.tm account (used as a bearer for the mail.tm HTTP API)."""

    ...


class SignupEmailAddress(NonEmptyStr):
    """A ``<account-local>+<suffix>@<mailtm-domain>`` address freshly handed out per signup test.

    The local part comes from the per-run mail.tm account (minted by the
    orchestrator); the ``+<suffix>`` is a per-test value so concurrent
    tests do not collide on the same shared inbox.
    """

    ...


class VerificationToken(NonEmptyStr):
    """A token extracted from a mail.tm verification email."""

    ...


class OneTimeLoginCode(NonEmptyStr):
    """A one-time login code extracted from a mail.tm sign-in email."""

    ...


class DeploymentTestConfigError(MindError):
    """Raised when the orchestrator-provided test config can't be located or parsed."""


class MailtmFetchError(MindError):
    """Raised when polling mail.tm fails (no email arrived in time, or the API errored)."""


class InvalidMailtmAddressError(MindError):
    """Raised when a mail.tm address fails structural validation (e.g. missing ``@``)."""
