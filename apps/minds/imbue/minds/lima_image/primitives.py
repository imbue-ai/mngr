import platform
import re
from enum import auto
from typing import Any
from typing import Final
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr

# The minisign-signed root manifest and per-arch indexes live under these
# fixed prefixes within a release's chunk-store base URL. Kept here (the
# lowest layer) so both the consumer and the publish-side helpers agree on
# the on-CDN layout.
MANIFEST_OBJECT_PREFIX: Final[str] = "manifests"
INDEX_OBJECT_PREFIX: Final[str] = "indexes"
CHUNK_STORE_PREFIX: Final[str] = "store"
ROOT_MANIFEST_FILENAME: Final[str] = "root.json"
MINISIGN_SIGNATURE_SUFFIX: Final[str] = ".minisig"

_SHA256_HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class MindsImageVersion(NonEmptyStr):
    """A minds release tag that names a pre-baked image set, e.g. ``minds-v0.3.4``."""

    ...


class Sha256Hex(str):
    """A lowercase hex-encoded SHA-256 digest (exactly 64 hex chars)."""

    def __new__(cls, value: str) -> Self:
        normalized = value.strip().lower()
        if not _SHA256_HEX_PATTERN.match(normalized):
            raise ValueError(f"Not a valid lowercase hex SHA-256 digest: {value!r}")
        return super().__new__(cls, normalized)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=64, max_length=64),
        )


class ImageArch(UpperCaseStrEnum):
    """A CPU architecture a pre-baked Lima image targets.

    The string values (``AARCH64`` / ``X86_64``) are the canonical keys used in
    the on-CDN root manifest. Helpers below map them to the Lima arch string and
    to the ``mngr`` per-arch image-url config field.
    """

    AARCH64 = auto()
    X86_64 = auto()


def get_current_image_arch() -> ImageArch:
    """Return the :class:`ImageArch` matching the machine this process runs on."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return ImageArch.AARCH64
    return ImageArch.X86_64


def lima_provider_image_url_setting_key(arch: ImageArch) -> str:
    """Return the ``mngr`` settings key that overrides the Lima default image URL for ``arch``.

    These are the existing ``providers.lima.default_image_url_*`` overrides the
    Lima provider already consumes, so pointing Lima at a locally-assembled image
    needs no provider code change -- only a ``-S <key>=<path>`` on ``mngr create``.
    """
    match arch:
        case ImageArch.AARCH64:
            return "providers.lima.default_image_url_aarch64"
        case ImageArch.X86_64:
            return "providers.lima.default_image_url_x86_64"
