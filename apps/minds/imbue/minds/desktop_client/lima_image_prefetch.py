"""Desktop-client glue for the pre-baked Lima image cache (issue #2306).

Resolves the per-env image source from config, decides when a create should use
the pre-baked image (the gate), and runs the background prefetch worker that
keeps the current release's image present + verified. The heavy lifting lives in
``imbue.minds.lima_image``; this module is the minds-app-level wiring.
"""

import time
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.errors import LimaImageDownloadError
from imbue.minds.errors import LimaImageError
from imbue.minds.lima_image.cache_layout import LimaImageCacheLayout
from imbue.minds.lima_image.data_types import LimaImagePrefetchState
from imbue.minds.lima_image.data_types import LimaImagePrefetchStatus
from imbue.minds.lima_image.data_types import LimaImageSource
from imbue.minds.lima_image.desync import DesyncImageChunkStore
from imbue.minds.lima_image.ensure import ensure_current_lima_image
from imbue.minds.lima_image.interfaces import ImageChunkStoreInterface
from imbue.minds.lima_image.interfaces import ImageFormatConverterInterface
from imbue.minds.lima_image.interfaces import LimaImageProgressSinkInterface
from imbue.minds.lima_image.interfaces import ManifestFetcherInterface
from imbue.minds.lima_image.interfaces import SignatureVerifierInterface
from imbue.minds.lima_image.manifest_fetcher import HttpxManifestFetcher
from imbue.minds.lima_image.minisign import MinisignSignatureVerifier
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MindsImageVersion
from imbue.minds.lima_image.primitives import get_current_image_arch
from imbue.minds.lima_image.primitives import lima_provider_image_url_setting_key
from imbue.minds.lima_image.progress import FileLimaImageProgressSink
from imbue.minds.lima_image.qemu_converter import QemuImageFormatConverter

# Set to a truthy value to disable downloading/using the pre-baked image entirely
# (forces build-in-VM). Used by tests / dev iteration.
KILL_SWITCH_ENV_VAR: Final[str] = "MINDS_DISABLE_LIMA_IMAGE_CACHE"

# Subdirectory under the env data root holding the per-env image cache.
LIMA_IMAGE_CACHE_DIRNAME: Final[str] = "lima-images"

# Background auto-retry backoff bounds for a published-but-failing download.
_RETRY_INITIAL_BACKOFF_SECONDS: Final[float] = 5.0
_RETRY_MAX_BACKOFF_SECONDS: Final[float] = 120.0


def is_lima_image_cache_disabled(environ: Mapping[str, str]) -> bool:
    """Return whether the kill-switch env var disables the pre-baked image path."""
    return environ.get(KILL_SWITCH_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def make_lima_image_source(client_env_config: ClientEnvConfig | None) -> LimaImageSource | None:
    """Build the per-env image source, or None when the env doesn't configure one."""
    if client_env_config is None:
        return None
    base_url = client_env_config.lima_image_base_url
    public_key = client_env_config.lima_image_minisign_public_key
    if base_url is None or public_key is None:
        return None
    return LimaImageSource(base_url=base_url, public_key=public_key)


def should_use_prebaked_lima_image(
    *,
    is_lima_launch_mode: bool,
    repo_url: str,
    branch_or_tag: str | None,
    current_release_tag: str,
    default_repo_url: str,
    source: LimaImageSource | None,
    is_dev_loop: bool,
    environ: Mapping[str, str],
) -> bool:
    """Decide whether this create should use the pre-baked image.

    True only for the *default* workspace: a Lima create of the default FCT repo
    at the current release tag, with a configured source, not in the dev loop, and
    not disabled by the kill switch. Anything else falls back to build-in-VM.
    """
    if not is_lima_launch_mode:
        return False
    if source is None:
        return False
    if is_dev_loop:
        return False
    if is_lima_image_cache_disabled(environ):
        return False
    if repo_url != default_repo_url:
        return False
    if branch_or_tag != current_release_tag:
        return False
    return True


def prebaked_image_mngr_setting_args(arch: ImageArch, qcow2_path: Path) -> list[str]:
    """Return the ``-S providers.lima.default_image_url_<arch>=<path>`` args pointing Lima at the baked image."""
    return ["-S", f"{lima_provider_image_url_setting_key(arch)}={qcow2_path}"]


class LimaImagePrefetcher(MutableModel):
    """Background worker that keeps the current release's pre-baked image present + verified.

    Dependency-injected impls keep the orchestration testable; use
    :func:`make_lima_image_prefetcher` for the production wiring.
    """

    source: LimaImageSource = Field(frozen=True, description="Per-env origin + trust anchor")
    minds_version: MindsImageVersion = Field(frozen=True, description="Current release tag to ensure")
    arch: ImageArch = Field(frozen=True, description="Architecture to ensure")
    cache_dir: Path = Field(frozen=True, description="Per-env image cache directory")
    fetcher: ManifestFetcherInterface = Field(frozen=True, description="Manifest/index fetcher")
    verifier: SignatureVerifierInterface = Field(frozen=True, description="Manifest signature verifier")
    chunk_store: ImageChunkStoreInterface = Field(frozen=True, description="Chunk-store extractor")
    converter: ImageFormatConverterInterface = Field(frozen=True, description="raw<->qcow2 converter")
    progress_sink: LimaImageProgressSinkInterface = Field(frozen=True, description="Progress state sink")

    def ensure_once(self) -> LimaImagePrefetchState:
        """Run a single ensure attempt; on a published-image failure record FAILED and return it."""
        try:
            ensure_current_lima_image(
                source=self.source,
                minds_version=self.minds_version,
                arch=self.arch,
                cache_dir=self.cache_dir,
                fetcher=self.fetcher,
                verifier=self.verifier,
                chunk_store=self.chunk_store,
                converter=self.converter,
                progress_sink=self.progress_sink,
            )
        except LimaImageError as exc:
            logger.warning("Lima image prefetch attempt failed: {}", exc)
            self._record_failure(str(exc))
        state = self.progress_sink.read_state()
        # read_state cannot be None right after a write, but stay total for the type checker.
        return state if state is not None else self._failure_state("no state recorded")

    def run_background_loop(self, concurrency_group: ConcurrencyGroup) -> None:
        """Ensure-with-backoff until READY or VERSION_UNAVAILABLE (or shutdown).

        A FAILED published-image download is retried with capped exponential
        backoff -- the inner-level auto-retry behind the user's manual retry.
        """
        backoff = _RETRY_INITIAL_BACKOFF_SECONDS
        while not concurrency_group.is_shutting_down():
            state = self.ensure_once()
            if state.status in (LimaImagePrefetchStatus.READY, LimaImagePrefetchStatus.VERSION_UNAVAILABLE):
                return
            # Interruptible backoff: wait() returns True if shutdown was requested.
            if concurrency_group.shutdown_event.wait(backoff):
                return
            backoff = min(backoff * 2, _RETRY_MAX_BACKOFF_SECONDS)

    def wait_until_terminal(
        self, timeout_seconds: float, poll_interval_seconds: float
    ) -> LimaImagePrefetchState | None:
        """Poll the persisted state until a terminal status or timeout; None if no state yet at timeout."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            state = self.progress_sink.read_state()
            if state is not None and state.status in (
                LimaImagePrefetchStatus.READY,
                LimaImagePrefetchStatus.VERSION_UNAVAILABLE,
                LimaImagePrefetchStatus.FAILED,
            ):
                return state
            if time.monotonic() >= deadline:
                return state
            time.sleep(poll_interval_seconds)

    def _record_failure(self, message: str) -> None:
        self.progress_sink.write_state(self._failure_state(message))

    def _failure_state(self, message: str) -> LimaImagePrefetchState:
        return LimaImagePrefetchState(
            status=LimaImagePrefetchStatus.FAILED,
            minds_version=self.minds_version,
            arch=self.arch,
            updated_at=datetime.now(timezone.utc),
            error=message,
        )


def resolve_ready_prebaked_lima_image(
    *,
    prefetcher: "LimaImagePrefetcher | None",
    is_lima_launch_mode: bool,
    repo_url: str,
    branch_or_tag: str | None,
    current_release_tag: str,
    default_repo_url: str,
    is_dev_loop: bool,
    environ: Mapping[str, str],
    wait_timeout_seconds: float,
    poll_interval_seconds: float,
) -> Path | None:
    """Resolve the baked qcow2 path to use for a create, or None to build in-VM.

    Returns None when the gate does not apply (non-default workspace, no prefetcher,
    kill switch, dev loop) or when no image is published for this release+arch
    (VERSION_UNAVAILABLE -> build in-VM). Raises ``LimaImageDownloadError`` when a
    *published* image cannot be made ready in time (FAILED or timeout): a retryable
    hard failure the create surfaces rather than silently rebuilding the slow way.
    """
    if prefetcher is None:
        return None
    if not should_use_prebaked_lima_image(
        is_lima_launch_mode=is_lima_launch_mode,
        repo_url=repo_url,
        branch_or_tag=branch_or_tag,
        current_release_tag=current_release_tag,
        default_repo_url=default_repo_url,
        source=prefetcher.source,
        is_dev_loop=is_dev_loop,
        environ=environ,
    ):
        return None
    state = prefetcher.wait_until_terminal(wait_timeout_seconds, poll_interval_seconds)
    if state is None:
        raise LimaImageDownloadError("Pre-baked Lima image is not ready yet; please retry.")
    match state.status:
        case LimaImagePrefetchStatus.READY:
            if state.qcow2_path is None:
                raise LimaImageDownloadError("Pre-baked Lima image reported ready without a path; please retry.")
            return state.qcow2_path
        case LimaImagePrefetchStatus.VERSION_UNAVAILABLE:
            logger.info("No pre-baked Lima image published for {}; building in-VM", current_release_tag)
            return None
        case _:
            raise LimaImageDownloadError(
                state.error or f"Pre-baked Lima image not ready (status {state.status.value}); please retry."
            )


class LimaImageCreateGate(FrozenModel):
    """Bundles everything the Lima create path needs to consult the pre-baked image.

    Built at startup (where the release tag + default repo URL + dev-loop signal
    are known) and handed to the ``AgentCreator`` so the create worker can resolve
    a ready image without importing the templates module (which would form an
    import cycle: templates already imports ``AgentCreationInfo`` from agent_creator).
    """

    prefetcher: LimaImagePrefetcher = Field(description="The background image prefetcher")
    current_release_tag: str = Field(description="Release tag the baked image is keyed to (FALLBACK_BRANCH)")
    default_repo_url: str = Field(description="Default forever-claude-template repo URL")
    is_dev_loop: bool = Field(description="Whether the operator opted into local-worktree dev defaults")

    def resolve_qcow2_for_create(
        self,
        *,
        is_lima_launch_mode: bool,
        repo_url: str,
        branch_or_tag: str | None,
        environ: Mapping[str, str],
        wait_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> Path | None:
        """Resolve the baked qcow2 path for a create (or None to build in-VM); raises on a published-but-unready image."""
        return resolve_ready_prebaked_lima_image(
            prefetcher=self.prefetcher,
            is_lima_launch_mode=is_lima_launch_mode,
            repo_url=repo_url,
            branch_or_tag=branch_or_tag,
            current_release_tag=self.current_release_tag,
            default_repo_url=self.default_repo_url,
            is_dev_loop=self.is_dev_loop,
            environ=environ,
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


def lima_image_cache_dir(data_dir: Path) -> Path:
    """Return the per-env image cache directory under the env data root."""
    return data_dir / LIMA_IMAGE_CACHE_DIRNAME


def make_lima_image_prefetcher(
    *,
    source: LimaImageSource,
    current_release_tag: str,
    data_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> LimaImagePrefetcher:
    """Build a prefetcher wired to the real desync/minisign/qemu-img/httpx implementations."""
    cache_dir = lima_image_cache_dir(data_dir)
    return LimaImagePrefetcher(
        source=source,
        minds_version=MindsImageVersion(current_release_tag),
        arch=get_current_image_arch(),
        cache_dir=cache_dir,
        fetcher=HttpxManifestFetcher(),
        verifier=MinisignSignatureVerifier(concurrency_group=concurrency_group),
        chunk_store=DesyncImageChunkStore(concurrency_group=concurrency_group),
        converter=QemuImageFormatConverter(concurrency_group=concurrency_group),
        progress_sink=FileLimaImageProgressSink(state_file=LimaImageCacheLayout(cache_dir=cache_dir).state_file),
    )
