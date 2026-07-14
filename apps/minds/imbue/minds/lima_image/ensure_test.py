import hashlib
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.minds.errors import LimaImageVerificationError
from imbue.minds.lima_image.cache_layout import LimaImageCacheLayout
from imbue.minds.lima_image.cache_layout import LimaImageCurrentPointer
from imbue.minds.lima_image.cache_layout import index_url
from imbue.minds.lima_image.cache_layout import manifest_signature_url
from imbue.minds.lima_image.cache_layout import manifest_url
from imbue.minds.lima_image.data_types import LimaImageEntry
from imbue.minds.lima_image.data_types import LimaImagePrefetchStatus
from imbue.minds.lima_image.data_types import LimaImageSource
from imbue.minds.lima_image.data_types import ROOT_MANIFEST_SCHEMA_VERSION
from imbue.minds.lima_image.data_types import RootManifest
from imbue.minds.lima_image.ensure import ensure_current_lima_image
from imbue.minds.lima_image.mock_lima_image_test import AcceptingSignatureVerifier
from imbue.minds.lima_image.mock_lima_image_test import FixedRawChunkStore
from imbue.minds.lima_image.mock_lima_image_test import InMemoryManifestFetcher
from imbue.minds.lima_image.mock_lima_image_test import RecordingProgressSink
from imbue.minds.lima_image.mock_lima_image_test import RejectingSignatureVerifier
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MindsImageVersion
from imbue.minds.lima_image.primitives import Sha256Hex

_BASE_URL = "https://fixture.invalid/lima"
_ARCH = ImageArch.X86_64


def _sha256(data: bytes) -> Sha256Hex:
    return Sha256Hex(hashlib.sha256(data).hexdigest())


def _publish(
    fetcher: InMemoryManifestFetcher,
    chunk_store: FixedRawChunkStore,
    *,
    version: MindsImageVersion,
    raw_bytes: bytes,
    include_arch: bool = True,
    signed: bool = True,
) -> None:
    """Register a published image (manifest + signature + index + assembled bytes) in the mocks."""
    index_object_key = f"indexes/{version}/{_ARCH.value}.caibx"
    entries = (
        (
            LimaImageEntry(
                arch=_ARCH,
                raw_index_object_key=index_object_key,
                raw_image_sha256=_sha256(raw_bytes),
                raw_image_size_bytes=NonNegativeInt(len(raw_bytes)),
            ),
        )
        if include_arch
        else ()
    )
    manifest = RootManifest(
        schema_version=ROOT_MANIFEST_SCHEMA_VERSION,
        minds_version=version,
        created_at=datetime.now(timezone.utc),
        entries=entries,
    )
    fetcher.objects_by_url[manifest_url(_BASE_URL, version)] = manifest.model_dump_json().encode()
    if signed:
        fetcher.objects_by_url[manifest_signature_url(_BASE_URL, version)] = b"signature"
    fetcher.objects_by_url[index_url(_BASE_URL, index_object_key)] = b"INDEX-BYTES"
    # ensure.py names the downloaded index <version>-<arch>.caibx.
    chunk_store.raw_bytes_by_index_name[f"{version}-{_ARCH.value}.caibx"] = raw_bytes


def _run(
    fetcher: InMemoryManifestFetcher,
    chunk_store: FixedRawChunkStore,
    verifier: AcceptingSignatureVerifier | RejectingSignatureVerifier,
    sink: RecordingProgressSink,
    *,
    version: MindsImageVersion,
    cache_dir: Path,
):
    return ensure_current_lima_image(
        source=LimaImageSource(base_url=_BASE_URL, public_key="RWtest"),
        minds_version=version,
        arch=_ARCH,
        cache_dir=cache_dir,
        fetcher=fetcher,
        verifier=verifier,
        chunk_store=chunk_store,
        progress_sink=sink,
    )


def test_base_download_assembles_verifies_and_installs(tmp_path: Path) -> None:
    version = MindsImageVersion("minds-v9.9.1")
    raw = b"raw-image-content-v1" * 100
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    sink = RecordingProgressSink()
    _publish(fetcher, chunk_store, version=version, raw_bytes=raw)

    result = _run(fetcher, chunk_store, AcceptingSignatureVerifier(), sink, version=version, cache_dir=tmp_path)

    assert result.status is LimaImagePrefetchStatus.READY
    assert result.raw_path is not None
    assert result.raw_path.read_bytes() == raw
    layout = LimaImageCacheLayout(cache_dir=tmp_path)
    assert layout.current_pointer_file.exists()
    # Phases are reported in order and end READY.
    statuses = [state.status for state in sink.states]
    assert statuses[0] is LimaImagePrefetchStatus.FETCHING_MANIFEST
    assert LimaImagePrefetchStatus.DOWNLOADING in statuses
    assert LimaImagePrefetchStatus.VERIFYING in statuses
    assert statuses[-1] is LimaImagePrefetchStatus.READY


def test_an_unreachable_origin_still_serves_the_installed_image(tmp_path: Path) -> None:
    # The image is already assembled and verified, so an origin we cannot reach must not
    # take the app offline: it keeps working, it just cannot learn about a newer image.
    version = MindsImageVersion("minds-v9.9.2")
    raw = b"content" * 50
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    _publish(fetcher, chunk_store, version=version, raw_bytes=raw)
    _run(
        fetcher,
        chunk_store,
        AcceptingSignatureVerifier(),
        RecordingProgressSink(),
        version=version,
        cache_dir=tmp_path,
    )

    fetcher.objects_by_url.clear()
    sink2 = RecordingProgressSink()
    result = _run(fetcher, chunk_store, AcceptingSignatureVerifier(), sink2, version=version, cache_dir=tmp_path)

    assert result.status is LimaImagePrefetchStatus.READY
    assert result.raw_path is not None
    assert result.raw_path.read_bytes() == raw
    # It consults the manifest (that is what catches a replaced image), but having failed
    # to get one it neither re-downloads nor discards what it has.
    statuses = [state.status for state in sink2.states]
    assert statuses == [LimaImagePrefetchStatus.FETCHING_MANIFEST, LimaImagePrefetchStatus.READY]


def test_an_image_republished_under_the_same_version_is_re_fetched(tmp_path: Path) -> None:
    # The whole point of recording the verified hash: the cache keys on (version, arch),
    # so without comparing bytes against the signed manifest a version republished with a
    # corrected image would be ignored forever and every client would boot the old one.
    version = MindsImageVersion("minds-v9.9.6")
    stale_raw = b"the-broken-image" * 40
    fixed_raw = b"the-corrected-image" * 40
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()

    _publish(fetcher, chunk_store, version=version, raw_bytes=stale_raw)
    first = _run(
        fetcher,
        chunk_store,
        AcceptingSignatureVerifier(),
        RecordingProgressSink(),
        version=version,
        cache_dir=tmp_path,
    )
    assert first.raw_path is not None
    assert first.raw_path.read_bytes() == stale_raw

    # Same version, different bytes -- the signed manifest now names a different hash.
    _publish(fetcher, chunk_store, version=version, raw_bytes=fixed_raw)
    sink2 = RecordingProgressSink()
    result = _run(fetcher, chunk_store, AcceptingSignatureVerifier(), sink2, version=version, cache_dir=tmp_path)

    assert result.status is LimaImagePrefetchStatus.READY
    assert result.raw_path is not None
    assert result.raw_path.read_bytes() == fixed_raw, "the republished image must replace the stale one"
    assert LimaImagePrefetchStatus.DOWNLOADING in [state.status for state in sink2.states]


def test_a_truncated_installed_image_is_re_fetched(tmp_path: Path) -> None:
    # The installed image is trusted via the hash recorded when it was verified, not
    # re-hashed every launch, so the size is what catches a file that lost bytes after
    # we wrote it (an interrupted write, a full disk) rather than being replaced.
    version = MindsImageVersion("minds-v9.9.7")
    raw = b"good-image" * 60
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    _publish(fetcher, chunk_store, version=version, raw_bytes=raw)
    first = _run(
        fetcher,
        chunk_store,
        AcceptingSignatureVerifier(),
        RecordingProgressSink(),
        version=version,
        cache_dir=tmp_path,
    )
    assert first.raw_path is not None

    first.raw_path.write_bytes(raw[: len(raw) // 2])
    result = _run(
        fetcher,
        chunk_store,
        AcceptingSignatureVerifier(),
        RecordingProgressSink(),
        version=version,
        cache_dir=tmp_path,
    )

    assert result.status is LimaImagePrefetchStatus.READY
    assert result.raw_path is not None
    assert result.raw_path.read_bytes() == raw, "a truncated image must be re-fetched, not booted"


def test_missing_manifest_reports_version_unavailable(tmp_path: Path) -> None:
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    sink = RecordingProgressSink()
    result = _run(
        fetcher,
        chunk_store,
        AcceptingSignatureVerifier(),
        sink,
        version=MindsImageVersion("minds-v0.0.0"),
        cache_dir=tmp_path,
    )
    assert result.status is LimaImagePrefetchStatus.VERSION_UNAVAILABLE
    assert sink.states[-1].status is LimaImagePrefetchStatus.VERSION_UNAVAILABLE


def test_manifest_without_this_arch_reports_version_unavailable(tmp_path: Path) -> None:
    version = MindsImageVersion("minds-v9.9.3")
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    _publish(fetcher, chunk_store, version=version, raw_bytes=b"x", include_arch=False)
    result = _run(
        fetcher,
        chunk_store,
        AcceptingSignatureVerifier(),
        RecordingProgressSink(),
        version=version,
        cache_dir=tmp_path,
    )
    assert result.status is LimaImagePrefetchStatus.VERSION_UNAVAILABLE


def test_rejected_signature_raises_verification_error(tmp_path: Path) -> None:
    version = MindsImageVersion("minds-v9.9.4")
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    _publish(fetcher, chunk_store, version=version, raw_bytes=b"y" * 64)
    with pytest.raises(LimaImageVerificationError):
        _run(
            fetcher,
            chunk_store,
            RejectingSignatureVerifier(),
            RecordingProgressSink(),
            version=version,
            cache_dir=tmp_path,
        )


def test_unsigned_manifest_raises_verification_error(tmp_path: Path) -> None:
    version = MindsImageVersion("minds-v9.9.5")
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    _publish(fetcher, chunk_store, version=version, raw_bytes=b"z" * 64, signed=False)
    with pytest.raises(LimaImageVerificationError):
        _run(
            fetcher,
            chunk_store,
            AcceptingSignatureVerifier(),
            RecordingProgressSink(),
            version=version,
            cache_dir=tmp_path,
        )


def test_assembled_hash_mismatch_raises_verification_error(tmp_path: Path) -> None:
    version = MindsImageVersion("minds-v9.9.6")
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    _publish(fetcher, chunk_store, version=version, raw_bytes=b"correct" * 10)
    # Corrupt the assembled bytes so they no longer match the manifest hash.
    chunk_store.raw_bytes_by_index_name[f"{version}-{_ARCH.value}.caibx"] = b"tampered"
    with pytest.raises(LimaImageVerificationError):
        _run(
            fetcher,
            chunk_store,
            AcceptingSignatureVerifier(),
            RecordingProgressSink(),
            version=version,
            cache_dir=tmp_path,
        )


def test_upgrade_seeds_from_prior_and_prunes_old_version(tmp_path: Path) -> None:
    v1 = MindsImageVersion("minds-v9.9.7")
    v2 = MindsImageVersion("minds-v9.9.8")
    raw_v1 = b"image-one" * 100
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    _publish(fetcher, chunk_store, version=v1, raw_bytes=raw_v1)
    _publish(fetcher, chunk_store, version=v2, raw_bytes=b"image-two" * 100)

    _run(fetcher, chunk_store, AcceptingSignatureVerifier(), RecordingProgressSink(), version=v1, cache_dir=tmp_path)
    result = _run(
        fetcher, chunk_store, AcceptingSignatureVerifier(), RecordingProgressSink(), version=v2, cache_dir=tmp_path
    )

    assert result.status is LimaImagePrefetchStatus.READY
    # The v1 image is the seed *in place*: it must still be readable during the v2
    # assembly, so it can only be pruned afterwards.
    assert chunk_store.seed_blob_bytes_by_index_name[f"{v2}-{_ARCH.value}.caibx"] == raw_v1
    # Retention: the old version directory is gone, only v2 remains.
    layout = LimaImageCacheLayout(cache_dir=tmp_path)
    assert not layout.version_dir(v1, _ARCH).exists()
    assert layout.version_dir(v2, _ARCH).exists()


def test_failed_upgrade_leaves_the_current_image_intact(tmp_path: Path) -> None:
    v1 = MindsImageVersion("minds-v9.9.9")
    v2 = MindsImageVersion("minds-v9.10.0")
    raw_v1 = b"image-one" * 100
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    _publish(fetcher, chunk_store, version=v1, raw_bytes=raw_v1)
    _publish(fetcher, chunk_store, version=v2, raw_bytes=b"image-two" * 100)
    _run(fetcher, chunk_store, AcceptingSignatureVerifier(), RecordingProgressSink(), version=v1, cache_dir=tmp_path)

    # Corrupt v2's assembled bytes so the upgrade fails the post-extract hash check --
    # the failure path that runs after v1 has been handed to the extractor as the seed.
    chunk_store.raw_bytes_by_index_name[f"{v2}-{_ARCH.value}.caibx"] = b"corrupt"
    with pytest.raises(LimaImageVerificationError):
        _run(
            fetcher, chunk_store, AcceptingSignatureVerifier(), RecordingProgressSink(), version=v2, cache_dir=tmp_path
        )

    # The seed was the live v1 image, so a failed upgrade must not have consumed it: v1 is
    # still on disk, still current, and still the image a create would be pointed at.
    layout = LimaImageCacheLayout(cache_dir=tmp_path)
    assert layout.raw_path(v1, _ARCH).read_bytes() == raw_v1
    assert layout.index_path(v1, _ARCH).exists()
    pointer = LimaImageCurrentPointer.model_validate_json(layout.current_pointer_file.read_text())
    assert pointer.minds_version == v1
    assert pointer.raw_path == layout.raw_path(v1, _ARCH)
