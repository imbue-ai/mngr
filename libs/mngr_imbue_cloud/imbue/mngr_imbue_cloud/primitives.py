import re
from enum import auto
from typing import Final
from typing import Self

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr

IMBUE_CLOUD_BACKEND_NAME: Final[str] = "imbue_cloud"

_DEFAULT_CONNECTOR_URL: Final[str] = "https://joshalbrecht--remote-service-connector-production-fastapi-app.modal.run"


def get_default_connector_url() -> str:
    """The baked-in production connector URL."""
    return _DEFAULT_CONNECTOR_URL


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidImbueCloudAccount(ValueError):
    """Raised when an account email fails validation."""


class ImbueCloudAccount(NonEmptyStr):
    """Email address identifying an Imbue Cloud account."""

    def __new__(cls, value: str) -> Self:
        stripped = value.strip().lower()
        if not _EMAIL_RE.match(stripped):
            raise InvalidImbueCloudAccount(f"Not a valid email address: '{value}'")
        return super().__new__(cls, stripped)


class SuperTokensUserId(NonEmptyStr):
    """The SuperTokens user_id (UUID v4)."""


class LeaseDbId(NonEmptyStr):
    """Database id of a leased host (server-side UUID)."""


class ImbueCloudKeyType(UpperCaseStrEnum):
    """The class of secret being requested."""

    LITELLM = auto()


def slugify_account(account: str) -> str:
    """Produce a stable, filesystem-safe slug for use in provider instance names.

    Lowercases, replaces non-alphanumeric characters with hyphens, collapses
    runs of hyphens, and strips leading/trailing hyphens. Used by minds when
    writing dynamic provider instance entries.
    """
    lowered = account.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        raise InvalidImbueCloudAccount(f"Cannot slugify account: '{account}'")
    return slug
