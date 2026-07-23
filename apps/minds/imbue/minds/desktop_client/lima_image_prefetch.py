"""Desktop-client glue for the pre-baked Lima image cache.

Resolves the per-env image source from config, decides when a create should use
the pre-baked image (the gate), and runs the background prefetch worker that
keeps the current release's image present + verified. The heavy lifting lives in
``imbue.minds.lima_image``; this module is the minds-app-level wiring.
"""

import threading
import time
from collections.abc import Callable
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
from imbue.minds.errors import LimaImageVerificationError
from imbue.minds.lima_image.cache_layout import LimaImageCacheLayout
from imbue.minds.lima_image.data_types import LimaImagePrefetchState
from imbue.minds.lima_image.data_types import LimaImagePrefetchStatus
from imbue.minds.lima_image.data_types import LimaImageSource
from imbue.minds.lima_image.desync import DesyncImageChunkStore
from imbue.minds.lima_image.ensure import ensure_current_lima_image
from imbue.minds.lima_image.interfaces import ImageChunkStoreInterface
from imbue.minds.lima_image.interfaces import LimaImageProgressSinkInterface
from imbue.minds.lima_image.interfaces import ManifestFetcherInterface
from imbue.minds.lima_image.interfaces import SignatureVerifierInterface
from imbue.minds.lima_image.manifest_fetcher import HttpxManifestFetcher
from imbue.minds.lima_image.minisign_verify import PythonMinisignSignatureVerifier
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MindsImageVersion
from imbue.minds.lima_image.primitives import get_current_image_arch
from imbue.minds.lima_image.primitives import lima_provider_image_url_setting_key
from imbue.minds.lima_image.progress import FileLimaImageProgressSink

# Set to a truthy value to disable downloading/using the pre-baked image entirely
# (forces build-in-VM). Used by tests / dev iteration.
KILL_SWITCH_ENV_VAR: Final[str] = "MINDS_DISABLE_LIMA_IMAGE_CACHE"

# Subdirectory under the env data root holding the per-env image cache.
LIMA_IMAGE_CACHE_DIRNAME: Final[str] = "lima-images"

# The gate resolves the release tag against the remote; a hung remote must not stall a create.
_LS_REMOTE_TIMEOUT_SECONDS: Final[float] = 30.0

# Background auto-retry backoff bounds for a published-but-failing download.
_RETRY_INITIAL_BACKOFF_SECONDS: Final[float] = 5.0
_RETRY_MAX_BACKOFF_SECONDS: Final[float] = 120.0

# st_blocks is defined in 512-byte units regardless of the filesystem's block size.
_STAT_BLOCK_BYTES: Final[int] = 512

# A create stops waiting on a download that gains less than this in this long: the image is
# not coming, so build in-VM now instead of sitting out the rest of the wait. The floor is
# far below any working download (a measured cold pull moves ~5.5GB in ~6 minutes, i.e. this
# much every few seconds), so it fires on a dead or hopelessly throttled transfer, not a slow
# one -- a merely slow link keeps its full wait and is bounded by the overall timeout instead.
DOWNLOAD_STALL_WINDOW_SECONDS: Final[float] = 120.0
DOWNLOAD_STALL_MIN_PROGRESS_BYTES: Final[int] = 32 * 1024 * 1024


def is_lima_image_cache_disabled(environ: Mapping[str, str]) -> bool:
    """Return whether the kill-switch env var disables the pre-baked image path."""
    return environ.get(KILL_SWITCH_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def baked_refs(current_release_tag: str, current_release_commit: str | None) -> frozenset[str]:
    """The refs that name the content the image was baked from: the release tag, and its commit."""
    if current_release_commit is None:
        return frozenset({current_release_tag})
    return frozenset({current_release_tag, current_release_commit})


def resolve_release_tag_commit(*, repo_url: str, release_tag: str, concurrency_group: ConcurrencyGroup) -> str | None:
    """Resolve ``release_tag`` to the commit it names, or None if it cannot be resolved.

    An annotated tag's ``ls-remote`` output carries both the tag object and, on a
    ``^{}`` line, the commit it peels to; the peeled commit is the one a create pins.
    None is not an error: the gate simply keeps matching on the tag name alone.
    """
    peeled_ref = f"refs/tags/{release_tag}^{{}}"
    cg = concurrency_group.make_concurrency_group(name="lima-image-resolve-release-tag")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "ls-remote", repo_url, f"refs/tags/{release_tag}", peeled_ref],
            is_checked_after=False,
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
        )
    if result.returncode != 0:
        logger.warning("Could not resolve {} in {}; SHA-pinned creates will build in-VM", release_tag, repo_url)
        return None

    commit_by_ref: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            commit_by_ref[parts[1].strip()] = parts[0].strip()
    return commit_by_ref.get(peeled_ref) or commit_by_ref.get(f"refs/tags/{release_tag}")


def make_lima_image_source(client_env_config: ClientEnvConfig | None) -> LimaImageSource | None:
    """Build the per-env image source, or None when the env doesn't configure one."""
    if client_env_config is None:
        return None
    base_url = client_env_config.lima_image_base_url
    public_key = client_env_config.lima_image_minisign_public_key
    if base_url is None or public_key is None:
        return None
    # config validated it as a URL; the cache layer works in plain strings.
    return LimaImageSource(base_url=str(base_url), public_key=public_key)


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
    current_release_commit: str | None = None,
) -> bool:
    """Decide whether this create should use the pre-baked image.

    True only for the *default* workspace: a Lima create of the default workspace template repo
    at the content the image was baked from, with a configured source, not in the dev loop,
    and not disabled by the kill switch. Anything else falls back to build-in-VM.

    The image is baked from a *commit*, so a create pinned to the release tag's commit SHA is
    just as safe as one naming the tag, and ``current_release_commit`` (when resolved) admits it.
    Requiring the tag's name would take the slow path for every SHA-pinned create -- which is
    what CI does for reproducibility, so the fast path would go untested.
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
    if branch_or_tag not in baked_refs(current_release_tag, current_release_commit):
        return False
    return True


def prebaked_image_mngr_setting_args(arch: ImageArch, raw_path: Path) -> list[str]:
    """Return the ``-S providers.lima.default_image_url_<arch>=<path>`` args pointing Lima at the baked image."""
    return ["-S", f"{lima_provider_image_url_setting_key(arch)}={raw_path}"]


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
    progress_sink: LimaImageProgressSinkInterface = Field(frozen=True, description="Progress state sink")
    stall_window_seconds: float = Field(
        default=DOWNLOAD_STALL_WINDOW_SECONDS,
        frozen=True,
        description="A waiting create gives up on a download that does not advance within this window",
    )
    stall_min_progress_bytes: int = Field(
        default=DOWNLOAD_STALL_MIN_PROGRESS_BYTES,
        frozen=True,
        description="Bytes a download must gain per window to count as still advancing",
    )

    def ensure_once(self) -> LimaImagePrefetchState:
        """Run a single ensure attempt, recording how it failed if it did.

        An image that cannot be *fetched* is FAILED and worth retrying; one that does not
        *verify* is UNTRUSTED, which retrying cannot fix -- the published bytes are the
        problem, and re-pulling multiple GB of them is pure waste.
        """
        try:
            ensure_current_lima_image(
                source=self.source,
                minds_version=self.minds_version,
                arch=self.arch,
                cache_dir=self.cache_dir,
                fetcher=self.fetcher,
                verifier=self.verifier,
                chunk_store=self.chunk_store,
                progress_sink=self.progress_sink,
            )
        except LimaImageVerificationError as exc:
            logger.error("Pre-baked Lima image did not verify against its signed manifest: {}", exc)
            self.progress_sink.write_state(self._failure_state(str(exc), LimaImagePrefetchStatus.UNTRUSTED))
        except LimaImageError as exc:
            logger.warning("Lima image prefetch attempt failed: {}", exc)
            self.progress_sink.write_state(self._failure_state(str(exc), LimaImagePrefetchStatus.FAILED))
        state = self.progress_sink.read_state()
        # read_state cannot be None right after a write, but stay total for the type checker.
        return state if state is not None else self._failure_state("no state recorded", LimaImagePrefetchStatus.FAILED)

    def run_background_loop(self, concurrency_group: ConcurrencyGroup) -> None:
        """Ensure-with-backoff until the image is READY or no retry could help (or shutdown).

        A FAILED fetch is retried with capped exponential backoff -- the inner-level auto-retry
        behind the user's manual retry. UNTRUSTED is not retried: the bytes that are published
        do not verify, so the next attempt would download the same multiple GB to reject them
        again.
        """
        backoff = _RETRY_INITIAL_BACKOFF_SECONDS
        terminal_statuses = (
            LimaImagePrefetchStatus.READY,
            LimaImagePrefetchStatus.VERSION_UNAVAILABLE,
            LimaImagePrefetchStatus.UNTRUSTED,
        )
        while not concurrency_group.is_shutting_down():
            state = self.ensure_once()
            if state.status in terminal_statuses:
                return
            # Interruptible backoff: wait() returns True if shutdown was requested.
            if concurrency_group.shutdown_event.wait(backoff):
                return
            backoff = min(backoff * 2, _RETRY_MAX_BACKOFF_SECONDS)

    def downloaded_bytes(self) -> int | None:
        """Bytes of the in-flight image that have actually landed, or None if it is not being assembled.

        The image is sparse, so the apparent size is the full 20GiB from the moment desync
        creates the file; only the allocated blocks say how much has really been fetched.
        """
        assembling = LimaImageCacheLayout(cache_dir=self.cache_dir).assembling_raw_path(self.minds_version, self.arch)
        try:
            return assembling.stat().st_blocks * _STAT_BLOCK_BYTES
        except OSError:
            return None

    def wait_until_terminal(
        self,
        timeout_seconds: float,
        poll_interval_seconds: float,
        on_download_progress: Callable[[int], None] | None = None,
    ) -> LimaImagePrefetchState | None:
        """Poll the persisted state until a terminal status, a stalled download, or the timeout.

        Returns the last state seen (None if none was ever written). A non-terminal state means
        the image is not usable *yet*: the caller builds in-VM rather than failing.

        Returns early when the download stops advancing. Waiting the full timeout out only pays
        off if the bytes are still coming; once they are not, every further second is one the
        user could have spent building the workspace in-VM instead.

        ``on_download_progress`` is called with the bytes fetched so far, so a caller blocked
        here can show that something is happening -- desync itself prints nothing off a tty.
        """
        deadline = time.monotonic() + timeout_seconds
        terminal_statuses = (
            LimaImagePrefetchStatus.READY,
            LimaImagePrefetchStatus.VERSION_UNAVAILABLE,
            LimaImagePrefetchStatus.FAILED,
            LimaImagePrefetchStatus.UNTRUSTED,
        )
        # A throwaway Event gives an interruptible sleep without time.sleep (the
        # codebase's standard poll idiom); it is never set, so wait() just delays.
        waiter = threading.Event()
        latest_state = self.progress_sink.read_state()
        window_started_at = time.monotonic()
        bytes_at_window_start = self.downloaded_bytes() or 0
        while (latest_state is None or latest_state.status not in terminal_statuses) and time.monotonic() < deadline:
            if latest_state is not None and latest_state.status is LimaImagePrefetchStatus.DOWNLOADING:
                fetched_bytes = self.downloaded_bytes() or 0
                if on_download_progress is not None:
                    on_download_progress(fetched_bytes)
                elapsed = time.monotonic() - window_started_at
                if elapsed >= self.stall_window_seconds:
                    if fetched_bytes - bytes_at_window_start < self.stall_min_progress_bytes:
                        logger.warning(
                            "Pre-baked image download gained only {} bytes in {:.0f}s; treating it as stalled",
                            fetched_bytes - bytes_at_window_start,
                            elapsed,
                        )
                        return latest_state
                    window_started_at = time.monotonic()
                    bytes_at_window_start = fetched_bytes
            else:
                # Not downloading (fetching the manifest, verifying): nothing to stall on, and
                # the byte count is meaningless, so keep the window anchored to now.
                window_started_at = time.monotonic()
                bytes_at_window_start = self.downloaded_bytes() or 0
            waiter.wait(poll_interval_seconds)
            latest_state = self.progress_sink.read_state()
        return latest_state

    def _failure_state(self, message: str, status: LimaImagePrefetchStatus) -> LimaImagePrefetchState:
        return LimaImagePrefetchState(
            status=status,
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
    current_release_commit: str | None = None,
    on_download_progress: Callable[[int], None] | None = None,
    on_fallback_to_in_vm: Callable[[str], None] | None = None,
) -> Path | None:
    """Resolve the baked raw image path to use for a create, or None to build in-VM.

    Returns None (build in-VM) whenever the image is merely *absent*: the gate does not apply
    (non-default workspace, no prefetcher, kill switch, dev loop), nothing is published for this
    release+arch, the download could not be made (network, disk, a missing tool), or it stalled
    or ran out the wait. None of those mean anything is wrong with the image, and the slow path
    still works -- so the user gets a workspace instead of an error, and the prefetch keeps
    running for the next create.

    Raises ``LimaImageVerificationError`` for UNTRUSTED, the one case where the image itself is
    the problem: bytes that do not match the signed manifest are never booted, and quietly
    building in-VM would hide the fact that someone is serving an image we cannot vouch for.
    """
    if prefetcher is None:
        return None
    if not should_use_prebaked_lima_image(
        is_lima_launch_mode=is_lima_launch_mode,
        repo_url=repo_url,
        branch_or_tag=branch_or_tag,
        current_release_tag=current_release_tag,
        current_release_commit=current_release_commit,
        default_repo_url=default_repo_url,
        source=prefetcher.source,
        is_dev_loop=is_dev_loop,
        environ=environ,
    ):
        return None
    state = prefetcher.wait_until_terminal(
        wait_timeout_seconds, poll_interval_seconds, on_download_progress=on_download_progress
    )
    match state:
        case None:
            return _fall_back_to_in_vm("the pre-baked image download never started", on_fallback_to_in_vm)
        case LimaImagePrefetchState(status=LimaImagePrefetchStatus.READY, raw_path=None):
            raise LimaImageDownloadError("Pre-baked Lima image reported ready without a path; please retry.")
        case LimaImagePrefetchState(status=LimaImagePrefetchStatus.READY, raw_path=Path() as raw_path):
            if not raw_path.exists():
                # The state says READY but the image is gone (a cleaned cache, a pruned disk).
                # Handing Lima a path to nothing would fail the create with an obscure error;
                # the prefetch re-downloads it on the next run.
                return _fall_back_to_in_vm(f"the pre-baked image is no longer at {raw_path}", on_fallback_to_in_vm)
            return raw_path
        case LimaImagePrefetchState(status=LimaImagePrefetchStatus.VERSION_UNAVAILABLE):
            return _fall_back_to_in_vm(
                f"no pre-baked image is published for {current_release_tag}", on_fallback_to_in_vm
            )
        case LimaImagePrefetchState(status=LimaImagePrefetchStatus.UNTRUSTED):
            # The published bytes do not match the signed manifest. Never boot them, and do not
            # paper over it by quietly building in-VM: someone is serving an image we cannot
            # vouch for, and that is worth stopping for.
            raise LimaImageVerificationError(
                state.error or f"Pre-baked Lima image for {current_release_tag} did not verify"
            )
        case LimaImagePrefetchState(status=LimaImagePrefetchStatus.FAILED):
            # The image could not be fetched (network, disk, a missing tool). Nothing is wrong
            # with the image itself and the slow path still works, so the user gets a workspace
            # rather than an error; the prefetch keeps retrying behind them.
            return _fall_back_to_in_vm(
                state.error or f"the pre-baked image for {current_release_tag} could not be downloaded",
                on_fallback_to_in_vm,
            )
        case _:
            # Still working when we stopped waiting -- either the download stalled or it ran out
            # the clock. The image is not broken, so do not fail the create over it: build the
            # workspace in-VM and leave the download running for the next one.
            return _fall_back_to_in_vm(
                f"the pre-baked image is still {state.status.value.lower()}", on_fallback_to_in_vm
            )


def _fall_back_to_in_vm(reason: str, on_fallback_to_in_vm: Callable[[str], None] | None) -> None:
    """Report why the create is building in-VM rather than using the pre-baked image, and return None."""
    logger.info("Building the workspace in-VM: {}", reason)
    if on_fallback_to_in_vm is not None:
        on_fallback_to_in_vm(reason)
    return None


class LimaImageCreateGate(FrozenModel):
    """Bundles everything the Lima create path needs to consult the pre-baked image.

    Built at startup (where the release tag + default repo URL + dev-loop signal
    are known) and handed to the ``AgentCreator`` so the create worker can resolve
    a ready image without importing the templates module (which would form a
    cycle, since templates already pulls ``AgentCreationInfo`` from agent_creator).
    """

    prefetcher: LimaImagePrefetcher = Field(description="The background image prefetcher")
    current_release_tag: str = Field(description="Release tag the baked image is keyed to (FALLBACK_BRANCH)")
    default_repo_url: str = Field(description="Default workspace template repo URL")
    is_dev_loop: bool = Field(description="Whether the operator opted into local-worktree dev defaults")
    current_release_commit: str | None = Field(
        default=None, description="Commit current_release_tag names, so a SHA-pinned create still matches"
    )

    def resolve_image_for_create(
        self,
        *,
        is_lima_launch_mode: bool,
        repo_url: str,
        branch_or_tag: str | None,
        environ: Mapping[str, str],
        wait_timeout_seconds: float,
        poll_interval_seconds: float,
        on_download_progress: Callable[[int], None] | None = None,
        on_fallback_to_in_vm: Callable[[str], None] | None = None,
    ) -> Path | None:
        """Resolve the baked image path for a create (or None to build in-VM); raises on a published-but-unready image."""
        return resolve_ready_prebaked_lima_image(
            prefetcher=self.prefetcher,
            is_lima_launch_mode=is_lima_launch_mode,
            repo_url=repo_url,
            branch_or_tag=branch_or_tag,
            current_release_tag=self.current_release_tag,
            current_release_commit=self.current_release_commit,
            default_repo_url=self.default_repo_url,
            is_dev_loop=self.is_dev_loop,
            environ=environ,
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            on_download_progress=on_download_progress,
            on_fallback_to_in_vm=on_fallback_to_in_vm,
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
    """Build a prefetcher wired to the real desync/minisign/httpx implementations."""
    cache_dir = lima_image_cache_dir(data_dir)
    return LimaImagePrefetcher(
        source=source,
        minds_version=MindsImageVersion(current_release_tag),
        arch=get_current_image_arch(),
        cache_dir=cache_dir,
        fetcher=HttpxManifestFetcher(),
        verifier=PythonMinisignSignatureVerifier(),
        chunk_store=DesyncImageChunkStore(concurrency_group=concurrency_group),
        progress_sink=FileLimaImageProgressSink(state_file=LimaImageCacheLayout(cache_dir=cache_dir).state_file),
    )
