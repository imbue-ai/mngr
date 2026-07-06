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
from imbue.minds.lima_image.interfaces import ImageFormatConverterInterface
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

    def emit(self, status: LimaImagePrefetchStatus, detail: str | None, qcow2_path: Path | None) -> None:
        self.progress_sink.write_state(
            LimaImagePrefetchState(
                status=status,
                minds_version=self.minds_version,
                arch=self.arch,
                updated_at=_now(),
                qcow2_path=qcow2_path,
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
    layout: LimaImageCacheLayout,
    current: LimaImageCurrentPointer | None,
    target_version: MindsImageVersion,
    converter: ImageFormatConverterInterface,
) -> tuple[Path, Path] | None:
    """Convert the current qcow2 back to raw so it can seed the upgrade; None when there's no usable prior image."""
    if current is None or current.minds_version == target_version:
        return None
    if not current.qcow2_path.exists() or not current.index_path.exists():
        return None
    layout.tmp_dir.mkdir(parents=True, exist_ok=True)
    seed_raw = layout.tmp_dir / "seed.raw"
    with log_span("Converting current qcow2 to raw to seed the upgrade"):
        converter.convert_qcow2_to_raw(qcow2_file=current.qcow2_path, raw_file=seed_raw)
    return current.index_path, seed_raw


def ensure_current_lima_image(
    *,
    source: LimaImageSource,
    minds_version: MindsImageVersion,
    arch: ImageArch,
    cache_dir: Path,
    fetcher: ManifestFetcherInterface,
    verifier: SignatureVerifierInterface,
    chunk_store: ImageChunkStoreInterface,
    converter: ImageFormatConverterInterface,
    progress_sink: LimaImageProgressSinkInterface,
) -> EnsureImageResult:
    """Idempotently ensure the pre-baked qcow2 for ``minds_version``+``arch`` is present, verified, and current.

    Safe to re-run and resumable: an already-current image short-circuits with no
    network; an interrupted download resumes via desync's in-place extract; the
    previous version is deleted only after the new one is fully assembled and
    verified. Returns READY (with the qcow2 path) or VERSION_UNAVAILABLE (no
    published image for this release+arch). Raises ``LimaImageError`` subclasses
    for a published-but-unfetchable/unverifiable image -- never a silent rebuild.
    """
    layout = LimaImageCacheLayout(cache_dir=cache_dir)
    reporter = _ProgressReporter(progress_sink=progress_sink, minds_version=minds_version, arch=arch)

    # Fast path: the image for this exact version is already assembled.
    current = _read_current_pointer(layout)
    if current is not None and current.minds_version == minds_version and current.arch == arch:
        if current.qcow2_path.exists():
            reporter.emit(LimaImagePrefetchStatus.READY, None, current.qcow2_path)
            return EnsureImageResult(status=LimaImagePrefetchStatus.READY, qcow2_path=current.qcow2_path)

    reporter.emit(LimaImagePrefetchStatus.FETCHING_MANIFEST, None, None)
    manifest = _fetch_and_verify_manifest(
        source=source, minds_version=minds_version, layout=layout, fetcher=fetcher, verifier=verifier
    )
    if manifest is None:
        reporter.emit(LimaImagePrefetchStatus.VERSION_UNAVAILABLE, None, None)
        return EnsureImageResult(status=LimaImagePrefetchStatus.VERSION_UNAVAILABLE, qcow2_path=None)
    entry = manifest.entry_for_arch(arch)
    if entry is None:
        logger.info("Manifest for {} has no image for arch {}", minds_version, arch.value)
        reporter.emit(LimaImagePrefetchStatus.VERSION_UNAVAILABLE, None, None)
        return EnsureImageResult(status=LimaImagePrefetchStatus.VERSION_UNAVAILABLE, qcow2_path=None)

    qcow2_path = _assemble_and_install_image(
        source=source,
        minds_version=minds_version,
        arch=arch,
        entry=entry,
        layout=layout,
        current=current,
        fetcher=fetcher,
        chunk_store=chunk_store,
        converter=converter,
        reporter=reporter,
    )
    reporter.emit(LimaImagePrefetchStatus.READY, None, qcow2_path)
    return EnsureImageResult(status=LimaImagePrefetchStatus.READY, qcow2_path=qcow2_path)


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
    converter: ImageFormatConverterInterface,
    reporter: _ProgressReporter,
) -> Path:
    # Download the per-arch index next to the chunk store.
    layout.tmp_dir.mkdir(parents=True, exist_ok=True)
    downloaded_index = layout.tmp_dir / f"{minds_version}-{arch.value}.caibx"
    fetcher.download_to_file(index_url(source.base_url, entry.raw_index_object_key), downloaded_index)

    # Seed from the prior image when possible (incremental download).
    seed = _seed_paths_from_current(layout=layout, current=current, target_version=minds_version, converter=converter)
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

    # Convert raw -> qcow2 (the format Lima consumes), staged then atomically swapped.
    reporter.emit(LimaImagePrefetchStatus.CONVERTING, None, None)
    version_dir = layout.version_dir(minds_version, arch)
    version_dir.mkdir(parents=True, exist_ok=True)
    staged_qcow2 = layout.tmp_dir / f"{minds_version}-{arch.value}.qcow2"
    with log_span("Converting raw lima image to qcow2"):
        converter.convert_raw_to_qcow2(raw_file=assembled_raw, qcow2_file=staged_qcow2)

    final_qcow2 = layout.qcow2_path(minds_version, arch)
    final_index = layout.index_path(minds_version, arch)
    staged_qcow2.replace(final_qcow2)
    downloaded_index.replace(final_index)

    # Commit the new pointer, then prune the prior version + scratch files.
    _write_current_pointer(
        layout,
        LimaImageCurrentPointer(
            minds_version=minds_version, arch=arch, qcow2_path=final_qcow2, index_path=final_index
        ),
    )
    _prune_other_versions(layout, keep_version=minds_version, keep_arch=arch)
    assembled_raw.unlink(missing_ok=True)
    if seed_blob_file is not None:
        seed_blob_file.unlink(missing_ok=True)
    return final_qcow2


def _report_download_line(reporter: _ProgressReporter, line: str) -> None:
    stripped = line.strip()
    if stripped:
        reporter.emit(LimaImagePrefetchStatus.DOWNLOADING, stripped, None)
