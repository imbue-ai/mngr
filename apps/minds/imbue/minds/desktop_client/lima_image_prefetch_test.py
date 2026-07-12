from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.desktop_client.lima_image_prefetch import LimaImagePrefetcher
from imbue.minds.desktop_client.lima_image_prefetch import is_lima_image_cache_disabled
from imbue.minds.desktop_client.lima_image_prefetch import make_lima_image_source
from imbue.minds.desktop_client.lima_image_prefetch import prebaked_image_mngr_setting_args
from imbue.minds.desktop_client.lima_image_prefetch import resolve_ready_prebaked_lima_image
from imbue.minds.desktop_client.lima_image_prefetch import should_use_prebaked_lima_image
from imbue.minds.errors import LimaImageDownloadError
from imbue.minds.lima_image.cache_layout import manifest_signature_url
from imbue.minds.lima_image.cache_layout import manifest_url
from imbue.minds.lima_image.data_types import LimaImageEntry
from imbue.minds.lima_image.data_types import LimaImagePrefetchState
from imbue.minds.lima_image.data_types import LimaImagePrefetchStatus
from imbue.minds.lima_image.data_types import LimaImageSource
from imbue.minds.lima_image.data_types import ROOT_MANIFEST_SCHEMA_VERSION
from imbue.minds.lima_image.data_types import RootManifest
from imbue.minds.lima_image.mock_lima_image_test import AcceptingSignatureVerifier
from imbue.minds.lima_image.mock_lima_image_test import FixedRawChunkStore
from imbue.minds.lima_image.mock_lima_image_test import InMemoryManifestFetcher
from imbue.minds.lima_image.mock_lima_image_test import RecordingProgressSink
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MindsImageVersion
from imbue.minds.lima_image.primitives import Sha256Hex

_DEFAULT_REPO = "https://github.com/imbue-ai/default-workspace-template.git"
_TAG = "minds-v0.3.4"
_COMMIT = "a" * 40
_SOURCE = LimaImageSource(base_url="https://cdn.example/lima", public_key="RWkey")


def _gate(
    *,
    is_lima_launch_mode: bool = True,
    repo_url: str = _DEFAULT_REPO,
    branch_or_tag: str | None = _TAG,
    source: LimaImageSource | None = _SOURCE,
    is_dev_loop: bool = False,
    environ: Mapping[str, str] | None = None,
    current_release_commit: str | None = None,
) -> bool:
    return should_use_prebaked_lima_image(
        is_lima_launch_mode=is_lima_launch_mode,
        repo_url=repo_url,
        branch_or_tag=branch_or_tag,
        current_release_tag=_TAG,
        current_release_commit=current_release_commit,
        default_repo_url=_DEFAULT_REPO,
        source=source,
        is_dev_loop=is_dev_loop,
        environ=environ if environ is not None else {},
    )


def test_gate_true_for_default_workspace() -> None:
    assert _gate() is True


def test_gate_false_when_not_lima() -> None:
    assert _gate(is_lima_launch_mode=False) is False


def test_gate_false_when_no_source() -> None:
    assert _gate(source=None) is False


def test_gate_false_in_dev_loop() -> None:
    assert _gate(is_dev_loop=True) is False


def test_gate_false_for_non_default_repo() -> None:
    assert _gate(repo_url="https://github.com/someone/fork.git") is False


def test_gate_false_for_non_release_branch() -> None:
    assert _gate(branch_or_tag="main") is False
    assert _gate(branch_or_tag=None) is False


def test_gate_true_for_the_release_tags_own_commit() -> None:
    # CI pins the workspace to a SHA for reproducibility. The image is baked from a
    # commit, so the tag's commit is the same content as the tag: requiring the tag's
    # *name* would send every SHA-pinned create down the slow path, leaving the fast
    # path untested by the very run that is supposed to exercise it.
    assert _gate(branch_or_tag=_COMMIT, current_release_commit=_COMMIT) is True


def test_gate_false_for_a_commit_that_is_not_the_release_tags() -> None:
    assert _gate(branch_or_tag="b" * 40, current_release_commit=_COMMIT) is False


def test_gate_false_for_a_sha_when_the_tag_could_not_be_resolved() -> None:
    # An unresolvable tag must not open the gate to arbitrary SHAs; it just means
    # SHA-pinned creates build in-VM, which is the safe direction.
    assert _gate(branch_or_tag=_COMMIT, current_release_commit=None) is False


def test_gate_false_when_kill_switch_set() -> None:
    assert _gate(environ={"MINDS_DISABLE_LIMA_IMAGE_CACHE": "1"}) is False


def test_kill_switch_truthy_values() -> None:
    assert is_lima_image_cache_disabled({"MINDS_DISABLE_LIMA_IMAGE_CACHE": "1"})
    assert is_lima_image_cache_disabled({"MINDS_DISABLE_LIMA_IMAGE_CACHE": "TRUE"})
    assert not is_lima_image_cache_disabled({"MINDS_DISABLE_LIMA_IMAGE_CACHE": "0"})
    assert not is_lima_image_cache_disabled({})


def test_make_source_requires_both_fields() -> None:
    assert make_lima_image_source(None) is None
    base_only = ClientEnvConfig(
        connector_url=AnyUrl("https://c.example"),
        litellm_proxy_url=AnyUrl("https://l.example"),
        lima_image_base_url=AnyUrl("https://cdn.example/lima"),
    )
    assert make_lima_image_source(base_only) is None
    both = ClientEnvConfig(
        connector_url=AnyUrl("https://c.example"),
        litellm_proxy_url=AnyUrl("https://l.example"),
        lima_image_base_url=AnyUrl("https://cdn.example/lima"),
        lima_image_minisign_public_key="RWkey",
    )
    source = make_lima_image_source(both)
    assert source is not None and source.base_url == "https://cdn.example/lima"


def test_setting_args_point_lima_at_local_raw_image() -> None:
    args = prebaked_image_mngr_setting_args(ImageArch.X86_64, Path("/data/lima-images/img.raw"))
    assert args == ["-S", "providers.lima.default_image_url_x86_64=/data/lima-images/img.raw"]


def _prefetcher(
    fetcher: InMemoryManifestFetcher,
    chunk_store: FixedRawChunkStore,
    sink: RecordingProgressSink,
    cache_dir: Path,
) -> LimaImagePrefetcher:
    return LimaImagePrefetcher(
        source=_SOURCE,
        minds_version=MindsImageVersion(_TAG),
        arch=ImageArch.X86_64,
        cache_dir=cache_dir,
        fetcher=fetcher,
        verifier=AcceptingSignatureVerifier(),
        chunk_store=chunk_store,
        progress_sink=sink,
    )


def test_ensure_once_records_failed_on_missing_published_objects(tmp_path: Path) -> None:
    # Manifest exists but the index object is missing -> download failure -> FAILED (retryable).
    fetcher = InMemoryManifestFetcher()
    manifest = RootManifest(
        schema_version=ROOT_MANIFEST_SCHEMA_VERSION,
        minds_version=MindsImageVersion(_TAG),
        created_at=datetime.now(timezone.utc),
        entries=(
            LimaImageEntry(
                arch=ImageArch.X86_64,
                raw_index_object_key=f"indexes/{_TAG}/x86_64.caibx",
                raw_image_sha256=Sha256Hex("a" * 64),
                raw_image_size_bytes=NonNegativeInt(10),
            ),
        ),
    )
    fetcher.objects_by_url[manifest_url(_SOURCE.base_url, MindsImageVersion(_TAG))] = (
        manifest.model_dump_json().encode()
    )
    fetcher.objects_by_url[manifest_signature_url(_SOURCE.base_url, MindsImageVersion(_TAG))] = b"sig"
    # index object intentionally absent -> download_to_file raises -> FAILED

    sink = RecordingProgressSink()
    state = _prefetcher(fetcher, FixedRawChunkStore(), sink, tmp_path).ensure_once()
    assert state.status is LimaImagePrefetchStatus.FAILED
    assert state.error is not None


def test_wait_until_terminal_returns_ready(tmp_path: Path) -> None:
    sink = RecordingProgressSink()
    prefetcher = _prefetcher(InMemoryManifestFetcher(), FixedRawChunkStore(), sink, tmp_path)
    sink.write_state(
        LimaImagePrefetchState(
            status=LimaImagePrefetchStatus.READY,
            minds_version=MindsImageVersion(_TAG),
            arch=ImageArch.X86_64,
            updated_at=datetime.now(timezone.utc),
            raw_path=tmp_path / "image.raw",
        )
    )
    state = prefetcher.wait_until_terminal(timeout_seconds=1.0, poll_interval_seconds=0.01)
    assert state is not None and state.status is LimaImagePrefetchStatus.READY


def _resolve(
    prefetcher: LimaImagePrefetcher | None,
    *,
    branch_or_tag: str | None = _TAG,
    wait_timeout_seconds: float = 1.0,
) -> Path | None:
    return resolve_ready_prebaked_lima_image(
        prefetcher=prefetcher,
        is_lima_launch_mode=True,
        repo_url=_DEFAULT_REPO,
        branch_or_tag=branch_or_tag,
        current_release_tag=_TAG,
        default_repo_url=_DEFAULT_REPO,
        is_dev_loop=False,
        environ={},
        wait_timeout_seconds=wait_timeout_seconds,
        poll_interval_seconds=0.01,
    )


def _seed(
    sink: RecordingProgressSink, status: LimaImagePrefetchStatus, *, raw_path: Path | None, error: str | None
) -> None:
    sink.write_state(
        LimaImagePrefetchState(
            status=status,
            minds_version=MindsImageVersion(_TAG),
            arch=ImageArch.X86_64,
            updated_at=datetime.now(timezone.utc),
            raw_path=raw_path,
            error=error,
        )
    )


def test_resolve_returns_none_without_prefetcher() -> None:
    assert _resolve(None) is None


def test_resolve_returns_none_when_gate_false(tmp_path: Path) -> None:
    sink = RecordingProgressSink()
    prefetcher = _prefetcher(InMemoryManifestFetcher(), FixedRawChunkStore(), sink, tmp_path)
    _seed(sink, LimaImagePrefetchStatus.READY, raw_path=tmp_path / "i.raw", error=None)
    assert _resolve(prefetcher, branch_or_tag="main") is None


def test_resolve_returns_path_when_ready(tmp_path: Path) -> None:
    sink = RecordingProgressSink()
    prefetcher = _prefetcher(InMemoryManifestFetcher(), FixedRawChunkStore(), sink, tmp_path)
    raw = tmp_path / "image.raw"
    _seed(sink, LimaImagePrefetchStatus.READY, raw_path=raw, error=None)
    assert _resolve(prefetcher) == raw


def test_resolve_returns_none_when_version_unavailable(tmp_path: Path) -> None:
    sink = RecordingProgressSink()
    prefetcher = _prefetcher(InMemoryManifestFetcher(), FixedRawChunkStore(), sink, tmp_path)
    _seed(sink, LimaImagePrefetchStatus.VERSION_UNAVAILABLE, raw_path=None, error=None)
    assert _resolve(prefetcher) is None


def test_resolve_raises_when_failed(tmp_path: Path) -> None:
    sink = RecordingProgressSink()
    prefetcher = _prefetcher(InMemoryManifestFetcher(), FixedRawChunkStore(), sink, tmp_path)
    _seed(sink, LimaImagePrefetchStatus.FAILED, raw_path=None, error="network down")
    with pytest.raises(LimaImageDownloadError):
        _resolve(prefetcher)


def test_resolve_raises_on_timeout_without_terminal_state(tmp_path: Path) -> None:
    sink = RecordingProgressSink()
    prefetcher = _prefetcher(InMemoryManifestFetcher(), FixedRawChunkStore(), sink, tmp_path)
    # No terminal state ever written -> wait times out -> retryable raise.
    with pytest.raises(LimaImageDownloadError):
        _resolve(prefetcher, wait_timeout_seconds=0.05)
