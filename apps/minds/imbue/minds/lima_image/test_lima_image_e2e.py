"""End-to-end integration test of the lima image cache against the real desync / minisign binaries.

Builds a fixture chunk store + signed manifest, serves it over a local HTTP
server, and drives ``ensure_current_lima_image`` with the real implementations.
Skipped automatically when any required binary is absent (e.g. the offload CI
sandboxes), mirroring how the docker/tmux-gated tests behave.
"""

import hashlib
import http.server
import shutil
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.minds.errors import LimaImageVerificationError
from imbue.minds.lima_image.cache_layout import LimaImageCacheLayout
from imbue.minds.lima_image.data_types import LimaImageEntry
from imbue.minds.lima_image.data_types import LimaImagePrefetchStatus
from imbue.minds.lima_image.data_types import LimaImageSource
from imbue.minds.lima_image.data_types import ROOT_MANIFEST_SCHEMA_VERSION
from imbue.minds.lima_image.data_types import RootManifest
from imbue.minds.lima_image.desync import DesyncImageChunkStore
from imbue.minds.lima_image.desync import _get_desync_binary
from imbue.minds.lima_image.ensure import ensure_current_lima_image
from imbue.minds.lima_image.manifest_fetcher import HttpxManifestFetcher
from imbue.minds.lima_image.minisign_verify import PythonMinisignSignatureVerifier
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MindsImageVersion
from imbue.minds.lima_image.primitives import Sha256Hex
from imbue.minds.lima_image.progress import FileLimaImageProgressSink

# Resolved exactly as the runtime resolves it, so a dev tree whose ``resources/`` is
# staged exercises the binary that actually ships (the session conftest points
# MINDS_DESYNC_BINARY at it) and needs it on neither PATH nor Homebrew.
# minisign has no bundled counterpart: it signs the fixture manifest here, which is
# the publisher's job, not the app's -- the app verifies with PythonMinisignSignatureVerifier.
_DESYNC = _get_desync_binary()
_REQUIRED_BINARIES = (_DESYNC, "minisign")
_ARCH = ImageArch.X86_64

pytestmark = pytest.mark.skipif(
    any(shutil.which(name) is None for name in _REQUIRED_BINARIES),
    reason=f"requires {', '.join(_REQUIRED_BINARIES)}",
)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    # Set per-fixture before the server starts; serves files from this root.
    directory_root: str = "."

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=type(self).directory_root, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        return None


def _publish_image(origin_dir: Path, version: MindsImageVersion, raw_image: Path) -> None:
    """Chunk ``raw_image`` into the origin store + write a signed manifest, as the publish script will."""
    store_dir = origin_dir / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    index_path = origin_dir / "indexes" / str(version) / f"{_ARCH.value}.caibx"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_DESYNC, "make", "-s", str(store_dir), str(index_path), str(raw_image)],
        check=True,
        capture_output=True,
    )
    raw_sha = Sha256Hex(hashlib.sha256(raw_image.read_bytes()).hexdigest())
    manifest = RootManifest(
        schema_version=ROOT_MANIFEST_SCHEMA_VERSION,
        minds_version=version,
        created_at=datetime.now(timezone.utc),
        entries=(
            LimaImageEntry(
                arch=_ARCH,
                raw_index_object_key=f"indexes/{version}/{_ARCH.value}.caibx",
                raw_image_sha256=raw_sha,
                raw_image_size_bytes=NonNegativeInt(raw_image.stat().st_size),
            ),
        ),
    )
    manifest_path = origin_dir / "manifests" / str(version) / "root.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest.model_dump_json())
    subprocess.run(
        ["minisign", "-S", "-s", str(origin_dir / "minisign.key"), "-m", str(manifest_path)],
        check=True,
        capture_output=True,
        input=b"",
    )


@pytest.fixture(scope="module")
def origin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str, Path]]:
    """Build a signed fixture origin served over HTTP. Yields (base_url, public_key, origin_dir)."""
    origin_dir = tmp_path_factory.mktemp("lima-origin")
    # Generate an unencrypted minisign keypair for signing the manifests.
    subprocess.run(
        ["minisign", "-G", "-W", "-p", str(origin_dir / "minisign.pub"), "-s", str(origin_dir / "minisign.key")],
        check=True,
        capture_output=True,
    )
    public_key = (origin_dir / "minisign.pub").read_text().splitlines()[1]

    _QuietHandler.directory_root = str(origin_dir)
    server = socketserver.TCPServer(("127.0.0.1", 0), _QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, public_key, origin_dir
    finally:
        server.shutdown()
        server.server_close()


def _make_raw_image(path: Path, *, marker: bytes) -> None:
    """Write a multi-MB pseudo image: a shared body plus a per-version tail (for delta seeding)."""
    # ~3.3 MB body shared across versions, plus a per-version tail.
    body = (b"COMMON-BLOCK-" * 64) * 4096
    path.write_bytes(body + marker * 8192)


@pytest.fixture
def concurrency_group() -> Iterator[ConcurrencyGroup]:
    # A long-lived group (as the backend keeps) so ensure() exceptions propagate
    # plainly to the caller rather than being wrapped by a per-call CG __exit__.
    with ConcurrencyGroup(name="lima-image-e2e") as group:
        yield group


def _ensure(cg: ConcurrencyGroup, base_url: str, public_key: str, version: MindsImageVersion, cache_dir: Path):
    return ensure_current_lima_image(
        source=LimaImageSource(base_url=base_url, public_key=public_key),
        minds_version=version,
        arch=_ARCH,
        cache_dir=cache_dir,
        fetcher=HttpxManifestFetcher(),
        verifier=PythonMinisignSignatureVerifier(),
        chunk_store=DesyncImageChunkStore(concurrency_group=cg),
        progress_sink=FileLimaImageProgressSink(state_file=LimaImageCacheLayout(cache_dir=cache_dir).state_file),
    )


@pytest.mark.timeout(180)
def test_base_download_and_upgrade_end_to_end(
    origin: tuple[str, str, Path], concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    base_url, public_key, origin_dir = origin
    cache_dir = tmp_path / "cache"
    v1 = MindsImageVersion("minds-v1.0.0")
    v2 = MindsImageVersion("minds-v1.0.1")

    raw_v1 = origin_dir / "raw-v1.img"
    raw_v2 = origin_dir / "raw-v2.img"
    _make_raw_image(raw_v1, marker=b"AAAA")
    _make_raw_image(raw_v2, marker=b"BBBB")
    _publish_image(origin_dir, v1, raw_v1)
    _publish_image(origin_dir, v2, raw_v2)

    # Base install: assemble v1 from the real chunk store. What lands is the raw image
    # Lima consumes, byte-identical to what was published -- there is no conversion step.
    result_v1 = _ensure(concurrency_group, base_url, public_key, v1, cache_dir)
    assert result_v1.status is LimaImagePrefetchStatus.READY
    assert result_v1.raw_path is not None and result_v1.raw_path.exists()
    assert result_v1.raw_path.read_bytes() == raw_v1.read_bytes()

    # Idempotent re-run is a no-op fast path.
    assert _ensure(concurrency_group, base_url, public_key, v1, cache_dir).status is LimaImagePrefetchStatus.READY

    # Upgrade to v2 (seeded by v1), and confirm retention pruned v1.
    result_v2 = _ensure(concurrency_group, base_url, public_key, v2, cache_dir)
    assert result_v2.status is LimaImagePrefetchStatus.READY
    layout = LimaImageCacheLayout(cache_dir=cache_dir)
    assert layout.raw_path(v2, _ARCH).read_bytes() == raw_v2.read_bytes()
    assert not layout.version_dir(v1, _ARCH).exists()


@pytest.mark.timeout(60)
def test_missing_version_is_unavailable(
    origin: tuple[str, str, Path], concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    base_url, public_key, _ = origin
    result = _ensure(
        concurrency_group, base_url, public_key, MindsImageVersion("minds-v0.0.0-absent"), tmp_path / "cache"
    )
    assert result.status is LimaImagePrefetchStatus.VERSION_UNAVAILABLE


@pytest.mark.timeout(120)
def test_tampered_manifest_is_rejected(
    origin: tuple[str, str, Path], concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    base_url, public_key, origin_dir = origin
    version = MindsImageVersion("minds-v2.0.0")
    raw = origin_dir / "raw-tamper.img"
    _make_raw_image(raw, marker=b"CCCC")
    _publish_image(origin_dir, version, raw)
    # Tamper with the manifest *after* signing so the signature no longer matches.
    manifest_path = origin_dir / "manifests" / str(version) / "root.json"
    manifest_path.write_text(manifest_path.read_text().replace("v2.0.0", "v2.0.0-evil"))
    with pytest.raises(LimaImageVerificationError):
        _ensure(concurrency_group, base_url, public_key, version, tmp_path / "cache")
