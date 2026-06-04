"""Primitives + errors for the dynamic env subsystem (dev + ci tiers)."""

import re
from enum import auto
from typing import Final
from typing import Self

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.errors import MindError


class DeployStrategy(UpperCaseStrEnum):
    """How ``modal deploy`` cycles existing containers when a new version goes live.

    Wraps Modal's ``--strategy`` flag (1.4.x+). ``ROLLOVER`` is Modal's
    default: containers from the prior version stay alive serving
    in-flight requests until they idle out, and new requests gradually
    move to the new version's containers as they cold-boot. No
    downtime, but a "stale-content" window where new code is deployed
    yet not actually serving traffic for several minutes.

    ``RECREATE`` terminates ALL running containers immediately; the
    next request cold-boots a fresh container at the new version. Brief
    downtime / latency window, but every subsequent request is
    guaranteed to hit the new code. Necessary when a migration has
    just shifted the DB schema (old code against new schema is an
    untested combination), and the typical right answer for dev / CI /
    test deploys where the operator runs ``deploy`` and immediately
    observes the result.

    Lives here (rather than in :mod:`provisioning`) so
    :mod:`per_env_deploy` can import the type without re-importing
    :mod:`provisioning` (which already imports the per-env helpers --
    the other direction would close the cycle).
    """

    ROLLOVER = auto()
    RECREATE = auto()


# Max total length of a DevEnvName. Bounded so the dev env name can
# safely participate in a Modal deployed-function hostname under DNS's
# 63-char limit, even with the longest planned workspace name. Budget:
# ``<workspace>-<env>--<app-name>-<function-name>.modal.run``, where
# the bit before ``.modal.run`` is the DNS label and capped at 63 chars.
# With workspace ``minds-dev`` (9) + ``-`` (1) + ``<env>`` (N) + ``--`` (2)
# + ``rsc-dev`` (7) + ``-`` (1) + ``api`` (3) = 23 + N, leaving N <= 40.
# Conservative -- planned workspaces are no longer than this; longer
# ones would force tightening the cap.
MAX_DEV_ENV_NAME_LENGTH: Final[int] = 40

# Prefixes that mark a dynamic env. ``dev-`` is the developer tier
# (``dev-<user>``); ``ci-`` is the CI tier (``ci-<timestamp>-<uuid>``)
# stood up by the deployment-tests orchestrator. Both share the same
# user-portion shape; both flow through the same per-env deploy path.
DYNAMIC_ENV_PREFIXES: Final[tuple[str, ...]] = ("dev", "ci")

# By convention every dynamic env name starts with the tier prefix so the
# derived ``MINDS_ROOT_NAME`` (``minds-<tier>-<rest>``) reads tier-first
# everywhere it surfaces (mngr prefix, env root dir, Cloudflare tunnel
# tag, Modal env name, etc). The pattern is enforced strictly so a typo
# can't accidentally land state in a place that won't be cleaned up by
# ``minds env destroy``. The user portion's max length (34) is chosen
# so the total ``<tier>-<user>`` name is at most :data:`MAX_DEV_ENV_NAME_LENGTH`.
_DEV_ENV_USER_PORTION_PATTERN: Final[str] = r"[a-z0-9][a-z0-9_-]{0,34}[a-z0-9]"
_DYNAMIC_TIER_PATTERN: Final[str] = "|".join(DYNAMIC_ENV_PREFIXES)
DEV_ENV_NAME_PATTERN: Final[str] = rf"(?:{_DYNAMIC_TIER_PATTERN})-{_DEV_ENV_USER_PORTION_PATTERN}"

# Reserved tier names that bypass the ``<tier>-`` prefix requirement.
# Mirrors the reserved set in :mod:`imbue.minds.cli.env`. Kept here so
# :class:`DevEnvName` (the canonical "name of an activated env" type
# threaded through ``deploy_env`` / ``destroy_env``) can also wrap a
# tier name without forcing every call site to special-case the dispatch.
_RESERVED_TIER_NAMES: Final[frozenset[str]] = frozenset({"staging", "production"})


class InvalidDevEnvNameError(MindError):
    """Raised when a dev-env name fails validation."""


class DevEnvName(NonEmptyStr):
    """Name of a dynamic env (``dev-`` or ``ci-``), or one of the reserved tier names.

    Dynamic envs must start with one of :data:`DYNAMIC_ENV_PREFIXES`
    followed by ``-`` and a 2-35 char suffix of lowercase alphanumerics
    / ``-`` / ``_`` (no leading or trailing punctuation). The name flows
    into Modal environment names, Neon DB names, SuperTokens app names,
    OVH IAM tags, and filesystem paths under ``~/.minds-<name>/``, so we
    keep it conservative.

    The reserved tier names ``staging`` and ``production`` are also
    accepted so the same type can carry the activated env name through
    ``deploy_env`` / ``destroy_env`` without forcing the caller to
    special-case the tier-vs-dynamic-env dispatch. The CLI maps the
    tier name back via :func:`_tier_for_env_name` and routes the right
    operations from there.
    """

    def __new__(cls, value: str) -> Self:
        stripped = value.strip()
        if stripped in _RESERVED_TIER_NAMES:
            return super().__new__(cls, stripped)
        if not re.fullmatch(DEV_ENV_NAME_PATTERN, stripped):
            raise InvalidDevEnvNameError(
                f"Invalid env name {value!r}: must match {DEV_ENV_NAME_PATTERN!r} "
                f"(prefix one of {list(DYNAMIC_ENV_PREFIXES)!r} followed by 2-36 lowercase "
                "alphanumerics/_/-, no leading/trailing punctuation). "
                "Example: ``dev-josh-1`` or ``ci-20260518t140212z``. "
                f"Reserved tier names {sorted(_RESERVED_TIER_NAMES)!r} are also accepted."
            )
        if len(stripped) > MAX_DEV_ENV_NAME_LENGTH:
            raise InvalidDevEnvNameError(
                f"Env name {value!r} is {len(stripped)} chars; must be at most "
                f"{MAX_DEV_ENV_NAME_LENGTH} so the resulting Modal deployed-function "
                "hostname stays under DNS's 63-char limit."
            )
        return super().__new__(cls, stripped)


class DevEnvNotFoundError(MindError):
    """Raised when the operator references a dev env that has no local file."""


class DevEnvAlreadyExistsError(MindError):
    """Raised when ``minds env create`` is invoked for an existing name."""


class DevEnvProvisioningError(MindError):
    """Raised when ``minds env create`` fails partway through, after rollback."""


class VaultReadError(MindError):
    """Raised when a Vault read fails (no auth, missing path, bad data)."""


class VaultSecretNotFoundError(VaultReadError):
    """Raised specifically when the Vault path has no secret (CLI exit code 2).

    A subclass so callers can distinguish "this secret genuinely doesn't exist"
    (safe to treat as absent) from a transient/auth ``VaultReadError`` (which
    must NOT be silently treated as an empty secret)."""
