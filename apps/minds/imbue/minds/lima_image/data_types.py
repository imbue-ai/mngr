from datetime import datetime
from enum import auto
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MindsImageVersion
from imbue.minds.lima_image.primitives import Sha256Hex

# Bump when the on-CDN root-manifest shape changes incompatibly. The consumer
# refuses a manifest whose schema_version it does not understand rather than
# guessing at an unknown layout.
ROOT_MANIFEST_SCHEMA_VERSION: Final[int] = 1


class LimaImageEntry(FrozenModel):
    """One published per-architecture image within a release's root manifest.

    Raw is both what desync chunks and what Lima consumes, so the index + hash
    describe the image the consumer ends up running -- no conversion in between.
    """

    arch: ImageArch = Field(description="Architecture this entry targets")
    raw_index_object_key: str = Field(
        description="Object key (relative to the chunk-store base URL) of the desync .caibx index for the raw image"
    )
    raw_image_sha256: Sha256Hex = Field(
        description="SHA-256 of the fully-assembled raw image, checked after extraction"
    )
    raw_image_size_bytes: NonNegativeInt = Field(description="Size in bytes of the assembled raw image")


class RootManifest(FrozenModel):
    """The minisign-signed manifest describing every arch's image for one release."""

    schema_version: int = Field(description="On-CDN manifest schema version; must equal ROOT_MANIFEST_SCHEMA_VERSION")
    minds_version: MindsImageVersion = Field(description="The minds release tag this manifest describes")
    created_at: datetime = Field(description="When the manifest was produced (UTC)")
    entries: tuple[LimaImageEntry, ...] = Field(description="One entry per published architecture")

    def entry_for_arch(self, arch: ImageArch) -> LimaImageEntry | None:
        for entry in self.entries:
            if entry.arch == arch:
                return entry
        return None


class LimaImageSource(FrozenModel):
    """Per-env origin + trust anchor for the pre-baked image distribution.

    Sourced from the env's ``client.toml`` so staging / test / e2e can point at a
    fixture origin while production defaults to the CDN. ``base_url`` is the root
    under which ``manifests/``, ``indexes/``, and ``store/`` live; ``public_key``
    is the minisign public key (single-line ``RW...`` form) the root manifest's
    signature is verified against.
    """

    base_url: str = Field(description="Root URL of the chunk store / CDN (no trailing path beyond the base)")
    public_key: str = Field(
        description="Minisign public key (single line, e.g. 'RWxxxx...') the manifest is signed with"
    )


class LimaImagePrefetchStatus(UpperCaseStrEnum):
    """Lifecycle status of the per-env "ensure current image present" operation.

    Written to a state file by the prefetch worker and read by the Lima create
    gate. The non-terminal values are the ordered phases the ensure operation
    walks through; ``READY`` means the image is assembled, verified, and usable;
    ``VERSION_UNAVAILABLE`` means the CDN has nothing for this release+arch (the
    gate then falls back to build-in-VM); ``FAILED`` means a published image
    could not be fetched/verified (the gate surfaces a retryable error).
    """

    IDLE = auto()
    FETCHING_MANIFEST = auto()
    DOWNLOADING = auto()
    VERIFYING = auto()
    READY = auto()
    VERSION_UNAVAILABLE = auto()
    FAILED = auto()


class LimaImagePrefetchState(FrozenModel):
    """Snapshot of the ensure-image operation, persisted for the create gate to read."""

    status: LimaImagePrefetchStatus = Field(description="Current status of the ensure-image operation")
    minds_version: MindsImageVersion = Field(description="Release tag the operation targets")
    arch: ImageArch = Field(description="Architecture being ensured")
    updated_at: datetime = Field(description="When this state was last written (UTC)")
    raw_path: Path | None = Field(
        default=None,
        description="Absolute path to the verified, ready-to-use raw image; set only when status is READY",
    )
    detail: str | None = Field(
        default=None,
        description="Human-readable progress detail for the UI (e.g. desync's latest progress line)",
    )
    error: str | None = Field(default=None, description="Error message; set when status is FAILED")


class EnsureImageResult(FrozenModel):
    """Outcome of a single ``ensure_current_lima_image`` call."""

    status: LimaImagePrefetchStatus = Field(description="Terminal status (READY or VERSION_UNAVAILABLE)")
    raw_path: Path | None = Field(
        default=None, description="Absolute path to the verified raw image when status is READY"
    )
