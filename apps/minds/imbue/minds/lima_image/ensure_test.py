import hashlib
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.minds.errors import LimaImageVerificationError
from imbue.minds.lima_image.cache_layout import LimaImageCacheLayout
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


def test_second_call_is_a_no_network_fast_path(tmp_path: Path) -> None:
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

    # Drop every published object: a second call must still succeed from local state.
    fetcher.objects_by_url.clear()
    sink2 = RecordingProgressSink()
    result = _run(fetcher, chunk_store, AcceptingSignatureVerifier(), sink2, version=version, cache_dir=tmp_path)

    assert result.status is LimaImagePrefetchStatus.READY
    assert [state.status for state in sink2.states] == [LimaImagePrefetchStatus.READY]


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
    fetcher = InMemoryManifestFetcher()
    chunk_store = FixedRawChunkStore()
    _publish(fetcher, chunk_store, version=v1, raw_bytes=b"image-one" * 100)
    _publish(fetcher, chunk_store, version=v2, raw_bytes=b"image-two" * 100)

    _run(fetcher, chunk_store, AcceptingSignatureVerifier(), RecordingProgressSink(), version=v1, cache_dir=tmp_path)
    result = _run(
        fetcher, chunk_store, AcceptingSignatureVerifier(), RecordingProgressSink(), version=v2, cache_dir=tmp_path
    )

    assert result.status is LimaImagePrefetchStatus.READY
    # Seeding fired for the v2 assembly (the prior raw image was offered as a seed).
    assert f"{v2}-{_ARCH.value}.caibx" in chunk_store.seed_index_names_seen
    # Retention: the old version directory is gone, only v2 remains.
    layout = LimaImageCacheLayout(cache_dir=tmp_path)
    assert not layout.version_dir(v1, _ARCH).exists()
    assert layout.version_dir(v2, _ARCH).exists()
