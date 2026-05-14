"""Primitives + errors for the dynamic dev env subsystem."""

import re
from typing import Final
from typing import Self

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.errors import MindError

DEV_ENV_NAME_PATTERN: Final[str] = r"[a-z0-9][a-z0-9_-]{0,38}[a-z0-9]"


class InvalidDevEnvNameError(MindError):
    """Raised when a dev-env name fails validation."""


class DevEnvName(NonEmptyStr):
    """Name of a dynamic dev environment (validated to a tight charset).

    Constrained to lowercase alphanumerics, hyphen, and underscore. The
    name flows into Modal environment names, Neon DB names, SuperTokens
    app names, Vultr tags, and filesystem paths under ``~/.minds/envs/``,
    so we keep it conservative.
    """

    def __new__(cls, value: str) -> Self:
        stripped = value.strip()
        if not re.fullmatch(DEV_ENV_NAME_PATTERN, stripped):
            raise InvalidDevEnvNameError(
                f"Invalid dev env name {value!r}: must match {DEV_ENV_NAME_PATTERN!r} "
                f"(2-40 lowercase alphanumerics/_/-, no leading/trailing punctuation)."
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
