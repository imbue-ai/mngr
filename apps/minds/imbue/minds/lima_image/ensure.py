import hashlib
import shutil
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import LimaImageVerificationError
from imbue.minds.lima_image.cache_layout import LimaImageCacheLayout
from imbue.minds.lima_image.cache_layout import LimaImageCurrentPointer
from imbue.minds.lima_image.cache_layout import chunk_store_url
from imbue.minds.lima_image.cache_layout import index_url
from imbue.minds.lima_image.cache_layout import manifest_signature_url
from imbue.minds.lima_image.cache_layout import manifest_url
from imbue.minds.lima_image.cache_layout import root_manifest_describes
from imbue.minds.lima_image.data_types import EnsureImageResult
from imbue.minds.lima_image.data_types import LimaImageEntry
from imbue.minds.lima_image.data_types import LimaImagePrefetchState
from imbue.minds.lima_image.data_types import LimaImagePrefetchStatus
from imbue.minds.lima_image.data_types import LimaImageSource
from imbue.minds.lima_image.data_types import ROOT_MANIFEST_SCHEMA_VERSION
from imbue.minds.lima_image.data_types import RootManifest
from imbue.minds.lima_image.interfaces import ImageChunkStoreInterface
from imbue.minds.lima_image.interfaces import LimaImageProgressSinkInterface
from imbue.minds.lima_image.interfaces import ManifestFetcherInterface
from imbue.minds.lima_image.interfaces import SignatureVerifierInterface
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MindsImageVersion
from imbue.minds.lima_image.primitives import Sha256Hex

_SHA256_READ_CHUNK_BYTES: Final[int] = 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_of_file(path: Path) -> Sha256Hex:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_SHA256_READ_CHUNK_BYTES), b""):
            digest.update(block)
    return Sha256Hex(digest.hexdigest())


def _read_current_pointer(layout: LimaImageCacheLayout) -> LimaImageCurrentPointer | None:
    pointer_file = layout.current_pointer_file
    if not pointer_file.exists():
        return None
    try:
        return LimaImageCurrentPointer.model_validate_json(pointer_file.read_text())
    except (OSError, ValidationError) as exc:
        logger.warning("Ignoring unreadable lima image pointer {}: {}", pointer_file, exc)
        return None


def _write_current_pointer(layout: LimaImageCacheLayout, pointer: LimaImageCurrentPointer) -> None:
    layout.cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = layout.current_pointer_file.with_suffix(".tmp")
    tmp_path.write_text(pointer.model_dump_json())
    tmp_path.rename(layout.current_pointer_file)


def _prune_other_versions(layout: LimaImageCacheLayout, keep_version: MindsImageVersion, keep_arch: ImageArch) -> None:
    """Delete every assembled image except the one for ``keep_version`` + ``keep_arch`` (retention = current only)."""
    if not layout.versions_dir.exists():
        return
    keep_version_dir = layout.versions_dir / str(keep_version)
    keep_arch_dir = layout.version_dir(keep_version, keep_arch)
    for version_path in layout.versions_dir.iterdir():
        if version_path != keep_version_dir:
            shutil.rmtree(version_path, ignore_errors=True)
            continue
        for arch_path in version_path.iterdir():
            if arch_path != keep_arch_dir:
                shutil.rmtree(arch_path, ignore_errors=True)


class _ProgressReporter(MutableModel):
    """Persists ensure-image progress for one (version, arch) run via the injected sink."""

    progress_sink: LimaImageProgressSinkInterface = Field(frozen=True, description="Where progress is written")
    minds_version: MindsImageVersion = Field(frozen=True, description="Release tag being ensured")
    arch: ImageArch = Field(frozen=True, description="Architecture being ensured")

    def emit(self, status: LimaImagePrefetchStatus, detail: str | None, raw_path: Path | None) -> None:
        self.progress_sink.write_state(
            LimaImagePrefetchState(
                status=status,
                minds_version=self.minds_version,
                arch=self.arch,
                updated_at=_now(),
                raw_path=raw_path,
                detail=detail,
                error=None,
            )
        )


def _fetch_and_verify_manifest(
    *,
    source: LimaImageSource,
    minds_version: MindsImageVersion,
    layout: LimaImageCacheLayout,
    fetcher: ManifestFetcherInterface,
    verifier: SignatureVerifierInterface,
) -> RootManifest | None:
    """Fetch + signature-verify the root manifest. Returns None when no manifest is published (404)."""
    manifest_bytes = fetcher.fetch_optional_bytes(manifest_url(source.base_url, minds_version))
    if manifest_bytes is None:
        return None
    signature_bytes = fetcher.fetch_optional_bytes(manifest_signature_url(source.base_url, minds_version))
    if signature_bytes is None:
        # A manifest with no signature is untrusted, not "absent".
        raise LimaImageVerificationError(f"Root manifest for {minds_version} has no signature")

    layout.tmp_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = layout.tmp_dir / "root.json"
    signature_file = layout.tmp_dir / "root.json.minisig"
    manifest_file.write_bytes(manifest_bytes)
    signature_file.write_bytes(signature_bytes)
    verifier.verify_detached(signed_file=manifest_file, signature_file=signature_file, public_key=source.public_key)

    try:
        manifest = RootManifest.model_validate_json(manifest_bytes)
    except ValidationError as exc:
        raise LimaImageVerificationError(f"Root manifest for {minds_version} is malformed: {exc}") from exc
    if manifest.schema_version != ROOT_MANIFEST_SCHEMA_VERSION:
        raise LimaImageVerificationError(
            f"Root manifest schema_version {manifest.schema_version} != supported {ROOT_MANIFEST_SCHEMA_VERSION}"
        )
    if not root_manifest_describes(manifest, minds_version):
        raise LimaImageVerificationError(
            f"Root manifest at {minds_version} actually describes {manifest.minds_version}"
        )
    return manifest


def _seed_paths_from_current(
    *,
    current: LimaImageCurrentPointer | None,
    target_version: MindsImageVersion,
) -> tuple[Path, Path] | None:
    """Return the prior image's (index, blob) to seed the upgrade; None when there's no usable prior image.

    desync reads a seed without modifying it, so the current image serves as the
    seed in place. It is removed by the post-install prune, not here.
    """
    if current is None or current.minds_version == target_version:
        return None
    if not current.raw_path.exists() or not current.index_path.exists():
        return None
    return current.index_path, current.raw_path


def ensure_current_lima_image(
    *,
    source: LimaImageSource,
    minds_version: MindsImageVersion,
    arch: ImageArch,
    cache_dir: Path,
    fetcher: ManifestFetcherInterface,
    verifier: SignatureVerifierInterface,
    chunk_store: ImageChunkStoreInterface,
    progress_sink: LimaImageProgressSinkInterface,
) -> EnsureImageResult:
    """Idempotently ensure the pre-baked image for ``minds_version``+``arch`` is present, verified, and current.

    Safe to re-run and resumable: an already-current image short-circuits with no
    network; an interrupted download resumes via desync's in-place extract; the
    previous version is deleted only after the new one is fully assembled and
    verified. Returns READY (with the raw image path) or VERSION_UNAVAILABLE (no
    published image for this release+arch). Raises ``LimaImageError`` subclasses
    for a published-but-unfetchable/unverifiable image -- never a silent rebuild.
    """
    layout = LimaImageCacheLayout(cache_dir=cache_dir)
    reporter = _ProgressReporter(progress_sink=progress_sink, minds_version=minds_version, arch=arch)

    # Fast path: the image for this exact version is already assembled.
    current = _read_current_pointer(layout)
    if current is not None and current.minds_version == minds_version and current.arch == arch:
        if current.raw_path.exists():
            reporter.emit(LimaImagePrefetchStatus.READY, None, current.raw_path)
            return EnsureImageResult(status=LimaImagePrefetchStatus.READY, raw_path=current.raw_path)

    reporter.emit(LimaImagePrefetchStatus.FETCHING_MANIFEST, None, None)
    manifest = _fetch_and_verify_manifest(
        source=source, minds_version=minds_version, layout=layout, fetcher=fetcher, verifier=verifier
    )
    if manifest is None:
        reporter.emit(LimaImagePrefetchStatus.VERSION_UNAVAILABLE, None, None)
        return EnsureImageResult(status=LimaImagePrefetchStatus.VERSION_UNAVAILABLE, raw_path=None)
    entry = manifest.entry_for_arch(arch)
    if entry is None:
        logger.info("Manifest for {} has no image for arch {}", minds_version, arch.value)
        reporter.emit(LimaImagePrefetchStatus.VERSION_UNAVAILABLE, None, None)
        return EnsureImageResult(status=LimaImagePrefetchStatus.VERSION_UNAVAILABLE, raw_path=None)

    raw_path = _assemble_and_install_image(
        source=source,
        minds_version=minds_version,
        arch=arch,
        entry=entry,
        layout=layout,
        current=current,
        fetcher=fetcher,
        chunk_store=chunk_store,
        reporter=reporter,
    )
    reporter.emit(LimaImagePrefetchStatus.READY, None, raw_path)
    return EnsureImageResult(status=LimaImagePrefetchStatus.READY, raw_path=raw_path)


def _assemble_and_install_image(
    *,
    source: LimaImageSource,
    minds_version: MindsImageVersion,
    arch: ImageArch,
    entry: LimaImageEntry,
    layout: LimaImageCacheLayout,
    current: LimaImageCurrentPointer | None,
    fetcher: ManifestFetcherInterface,
    chunk_store: ImageChunkStoreInterface,
    reporter: _ProgressReporter,
) -> Path:
    # Download the per-arch index next to the chunk store.
    layout.tmp_dir.mkdir(parents=True, exist_ok=True)
    downloaded_index = layout.tmp_dir / f"{minds_version}-{arch.value}.caibx"
    fetcher.download_to_file(index_url(source.base_url, entry.raw_index_object_key), downloaded_index)

    # Seed from the prior image when possible (incremental download).
    seed = _seed_paths_from_current(current=current, target_version=minds_version)
    seed_index_file = seed[0] if seed is not None else None
    seed_blob_file = seed[1] if seed is not None else None

    # Assemble the raw image (resumable, seeded).
    assembled_raw = layout.tmp_dir / f"{minds_version}-{arch.value}.raw"
    reporter.emit(LimaImagePrefetchStatus.DOWNLOADING, None, None)
    with log_span("Assembling raw lima image {} ({})", minds_version, arch.value):
        chunk_store.extract_image(
            index_file=downloaded_index,
            chunk_store_url=chunk_store_url(source.base_url),
            output_file=assembled_raw,
            local_cache_dir=layout.desync_cache_dir,
            seed_index_file=seed_index_file,
            seed_blob_file=seed_blob_file,
            on_output=lambda line, is_stdout: _report_download_line(reporter, line),
        )

    # Verify the assembled raw against the signed manifest hash before using it.
    reporter.emit(LimaImagePrefetchStatus.VERIFYING, None, None)
    actual_sha = _sha256_of_file(assembled_raw)
    if actual_sha != entry.raw_image_sha256:
        assembled_raw.unlink(missing_ok=True)
        raise LimaImageVerificationError(
            f"Assembled image hash {actual_sha} != manifest {entry.raw_image_sha256} for {minds_version}/{arch.value}"
        )

    # Install the raw image Lima consumes directly: rename within the cache dir, so
    # the swap is atomic and the file's sparseness survives.
    version_dir = layout.version_dir(minds_version, arch)
    version_dir.mkdir(parents=True, exist_ok=True)
    final_raw = layout.raw_path(minds_version, arch)
    final_index = layout.index_path(minds_version, arch)
    assembled_raw.replace(final_raw)
    downloaded_index.replace(final_index)

    # Commit the new pointer, then prune the prior version (which is the seed, so it
    # must outlive the assembly above).
    _write_current_pointer(
        layout,
        LimaImageCurrentPointer(minds_version=minds_version, arch=arch, raw_path=final_raw, index_path=final_index),
    )
    _prune_other_versions(layout, keep_version=minds_version, keep_arch=arch)
    return final_raw


def _report_download_line(reporter: _ProgressReporter, line: str) -> None:
    stripped = line.strip()
    if stripped:
        reporter.emit(LimaImagePrefetchStatus.DOWNLOADING, stripped, None)
