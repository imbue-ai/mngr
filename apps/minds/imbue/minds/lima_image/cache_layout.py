from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.lima_image.data_types import RootManifest
from imbue.minds.lima_image.primitives import CHUNK_STORE_PREFIX
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MANIFEST_OBJECT_PREFIX
from imbue.minds.lima_image.primitives import MINISIGN_SIGNATURE_SUFFIX
from imbue.minds.lima_image.primitives import MindsImageVersion
from imbue.minds.lima_image.primitives import ROOT_MANIFEST_FILENAME
from imbue.minds.lima_image.primitives import Sha256Hex


class LimaImageCurrentPointer(FrozenModel):
    """On-disk record of which version+arch image is currently assembled and usable."""

    minds_version: MindsImageVersion = Field(description="The release tag of the current image")
    arch: ImageArch = Field(description="The architecture of the current image")
    raw_path: Path = Field(description="Absolute path to the current raw image")
    index_path: Path = Field(description="Absolute path to the current image's desync index, kept for seeding")
    raw_image_sha256: Sha256Hex | None = Field(
        default=None,
        description=(
            "SHA-256 the raw image was verified against when installed, so a later run can tell whether it still "
            "matches the published manifest without re-hashing multiple GB. None in pointers written before this "
            "field existed; those are re-hashed once and rewritten with it."
        ),
    )


class LimaImageCacheLayout(FrozenModel):
    """Pure filesystem layout of the per-env Lima image cache rooted at ``cache_dir``."""

    cache_dir: Path = Field(description="Root of the per-env image cache (e.g. ~/.minds/lima-images)")

    @property
    def state_file(self) -> Path:
        return self.cache_dir / "state.json"

    @property
    def current_pointer_file(self) -> Path:
        return self.cache_dir / "current.json"

    @property
    def desync_cache_dir(self) -> Path:
        """Local desync chunk cache, persisted across runs to avoid re-fetching."""
        return self.cache_dir / "chunk-cache"

    @property
    def tmp_dir(self) -> Path:
        """Scratch space for in-flight raw assembly."""
        return self.cache_dir / "tmp"

    @property
    def versions_dir(self) -> Path:
        return self.cache_dir / "versions"

    def version_dir(self, minds_version: MindsImageVersion, arch: ImageArch) -> Path:
        return self.versions_dir / str(minds_version) / arch.value

    def raw_path(self, minds_version: MindsImageVersion, arch: ImageArch) -> Path:
        return self.version_dir(minds_version, arch) / "image.raw"

    def index_path(self, minds_version: MindsImageVersion, arch: ImageArch) -> Path:
        return self.version_dir(minds_version, arch) / "image.caibx"


def manifest_url(base_url: str, minds_version: MindsImageVersion) -> str:
    """Return the URL of the signed root manifest for ``minds_version``."""
    return f"{_normalize_base(base_url)}/{MANIFEST_OBJECT_PREFIX}/{minds_version}/{ROOT_MANIFEST_FILENAME}"


def manifest_signature_url(base_url: str, minds_version: MindsImageVersion) -> str:
    """Return the URL of the root manifest's detached minisign signature."""
    return manifest_url(base_url, minds_version) + MINISIGN_SIGNATURE_SUFFIX


def index_url(base_url: str, entry_object_key: str) -> str:
    """Return the URL of a per-arch desync index given its manifest object key."""
    return f"{_normalize_base(base_url)}/{entry_object_key.lstrip('/')}"


def chunk_store_url(base_url: str) -> str:
    """Return the desync chunk-store URL (``-s`` target) under ``base_url``."""
    return f"{_normalize_base(base_url)}/{CHUNK_STORE_PREFIX}/"


def _normalize_base(base_url: str) -> str:
    return base_url.rstrip("/")


def root_manifest_describes(manifest: RootManifest, minds_version: MindsImageVersion) -> bool:
    """Return whether ``manifest`` actually describes ``minds_version`` (defends against a misfiled object)."""
    return manifest.minds_version == minds_version
