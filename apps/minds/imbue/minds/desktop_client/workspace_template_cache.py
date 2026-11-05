import hashlib
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_creator import TemplateCloneCacheInterface
from imbue.minds.desktop_client.agent_creator import clone_git_repo
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import InvalidTemplateReleaseTagError
from imbue.minds.errors import WorkspaceTemplateCacheError
from imbue.minds.primitives import GitCommitHash
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import TEMPLATE_RELEASE_TAG_PATTERN
from imbue.minds.primitives import TemplateReleaseTag
from imbue.mngr.utils.thread_cleanup import start_mngr_thread

# The cache's directory under the tier's cache root, which the OS may purge: everything in it is regenerable.
TEMPLATE_CACHE_DIRNAME: Final[str] = "template-cache"

# A published clone is named for its release and a digest of its repo URL; its siblings
# are clones in progress and clones being deleted. Nothing else in the directory is the
# cache's to delete: older app versions and the data-dir migration left files there.
_URL_DIGEST_LENGTH: Final[int] = 12
_ENTRY_NAME_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{TEMPLATE_RELEASE_TAG_PATTERN}-[0-9a-f]{{{_URL_DIGEST_LENGTH}}}$"
)
_INCOMING_DIR_PREFIX: Final[str] = ".incoming-"
_DISCARDED_DIR_PREFIX: Final[str] = ".discarded-"

_COPYING_DIR_PREFIX: Final[str] = ".copying-"

# An incoming or discarded directory this old was left by a crash; a younger one may be
# another app instance's clone or delete still in progress.
_ORPHANED_DIR_AGE_SECONDS: Final[float] = 24 * 60 * 60

_LS_REMOTE_TIMEOUT_SECONDS: Final[float] = 30.0
_LOCAL_GIT_TIMEOUT_SECONDS: Final[float] = 60.0

# The prefetch's download gives up once git receives under this many bytes a second for
# the stall timeout (git's http.lowSpeedLimit and http.lowSpeedTime), so a slow link can
# take as long as it needs while a dead one is let go.
_CLONE_LOW_SPEED_LIMIT_BYTES_PER_SECOND: Final[int] = 1000
_CLONE_STALL_TIMEOUT_SECONDS: Final[int] = 60
# Bounds each git step of the prefetch, for a hang git's stall check cannot see (a remote
# that is not HTTP(S), a stuck local step).
_CLONE_TIMEOUT_SECONDS: Final[float] = 3600.0

# Turned off for the cache's git commands: a filesystem monitor git started in the cache
# would watch it for as long as the daemon lives, and its socket in .git cannot be copied.
_FSMONITOR_CONFIG_KEY: Final[str] = "core.fsmonitor"

# How often a create waiting on the prefetch checks that the download is still growing.
_PROGRESS_POLL_SECONDS: Final[float] = 1.0

# A copy is well under a second of local disk work; this long means the disk is in trouble.
_SLOW_COPY_WARNING_SECONDS: Final[float] = 5.0

# Waits before retrying a failed prefetch (offline at launch, a GitHub outage). After the
# last one, creates clone from the network until the next launch.
_PREFETCH_RETRY_WAITS_SECONDS: Final[tuple[float, ...]] = (15.0, 60.0, 300.0)


def resolve_release_tag_commit(*, repo_url: str, release_tag: str, concurrency_group: ConcurrencyGroup) -> str | None:
    """Resolve ``release_tag`` to the commit it names on the remote, or None if the remote cannot be asked or lacks the tag.

    An annotated tag's ``ls-remote`` output carries both the tag object and, on a
    ``^{}`` line, the commit it peels to; the peeled commit is the one a create pins.
    """
    peeled_ref = f"refs/tags/{release_tag}^{{}}"
    cg = concurrency_group.make_concurrency_group(name="resolve-release-tag-commit")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "ls-remote", repo_url, f"refs/tags/{release_tag}", peeled_ref],
            is_checked_after=False,
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
        )
    if result.returncode != 0:
        logger.debug("git ls-remote could not resolve {} in {}: {}", release_tag, repo_url, result.stderr.strip())
        return None

    commit_by_ref: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            commit_by_ref[parts[1].strip()] = parts[0].strip()
    return commit_by_ref.get(peeled_ref) or commit_by_ref.get(f"refs/tags/{release_tag}")


@pure
def template_cache_entry_name(repo_url: str, release_tag: TemplateReleaseTag) -> str:
    """The directory name of one repo's clone at one release: the tag, then a digest of the URL."""
    return "{}-{}".format(release_tag, hashlib.sha256(repo_url.encode()).hexdigest()[:_URL_DIGEST_LENGTH])


class WorkspaceTemplateCacheStatus(UpperCaseStrEnum):
    """Whether a create of the cached release can copy it right now."""

    IDLE = auto()
    FETCHING = auto()
    READY = auto()
    UNAVAILABLE = auto()


class CachedTemplateClone(FrozenModel):
    """A published clone in the template cache, checked to be at its release's commit."""

    path: Path = Field(description="The clone's directory")
    commit: GitCommitHash = Field(description="The commit the clone has checked out, which its FETCH_HEAD names too")


class _GitDirCopyFilter(FrozenModel):
    """A ``copytree`` ignore function for a cached clone's ``.git``: leaves out its index, and any socket or FIFO."""

    git_dir: Path = Field(description="The cached clone's .git, whose index the copy rebuilds rather than copies")

    def __call__(self, directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if _is_socket_or_fifo(Path(directory) / name)}
        if Path(directory) == self.git_dir:
            ignored.add("index")
        return ignored


def _is_socket_or_fifo(path: Path) -> bool:
    mode = path.lstat().st_mode
    return stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode)


class _ObjectHardlinkingCopier(FrozenModel):
    """A ``copytree`` copy function that hardlinks git's object files and copies every other file."""

    objects_dir: Path = Field(description="The source repo's .git/objects; git never rewrites a file in it")

    def __call__(self, source: str, destination: str) -> str:
        if Path(source).is_relative_to(self.objects_dir):
            try:
                os.link(source, destination)
            except OSError as e:
                logger.debug("Copying {} instead of hardlinking it: {}", source, e)
            else:
                return destination
        return shutil.copy2(source, destination)


def _run_git(args: Sequence[str], repo_dir: Path, concurrency_group: ConcurrencyGroup) -> str:
    """Run a local git command in ``repo_dir`` and return its stdout; raises WorkspaceTemplateCacheError if it fails.

    ``-C`` rather than a working directory, so a ``repo_dir`` deleted from under the
    cache is a git failure rather than a process that cannot start.
    """
    cg = concurrency_group.make_concurrency_group(name="workspace-template-cache-git")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "-C", str(repo_dir), "-c", f"{_FSMONITOR_CONFIG_KEY}=false", *args],
            timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if result.returncode != 0:
        output = result.stderr.strip() if result.stderr.strip() else result.stdout.strip()
        raise WorkspaceTemplateCacheError(f"git {args[0]} failed in {repo_dir}: {output}")
    return result.stdout


def _read_fetched_commit(repo_dir: Path, concurrency_group: ConcurrencyGroup) -> GitCommitHash:
    """The commit a fresh ``clone_git_repo`` clone has checked out, which ``checkout_branch`` finds through FETCH_HEAD.

    Raises WorkspaceTemplateCacheError when either is missing or they disagree.
    """
    commits = _run_git(["rev-parse", "HEAD^{commit}", "FETCH_HEAD^{commit}"], repo_dir, concurrency_group).split()
    if len(commits) != 2 or commits[0] != commits[1]:
        raise WorkspaceTemplateCacheError(f"HEAD and FETCH_HEAD in {repo_dir} do not name one commit: {commits}")
    return GitCommitHash(commits[0])


def _copy_cached_clone(
    cached_clone: CachedTemplateClone, copying_dir: Path, clone_dir: Path, concurrency_group: ConcurrencyGroup
) -> None:
    """Make ``clone_dir`` the same repository as the cached clone, without the network.

    The git objects are hardlinked and the rest of ``.git`` (config, HEAD, FETCH_HEAD)
    is copied as is. The index and working tree are rebuilt from HEAD rather than
    copied, which is faster than copying the files and means a missing or stale index
    in the cache cannot leave the copy with the wrong files. The copy is built in
    ``copying_dir`` and renamed to ``clone_dir`` only once it checks out, so a failed
    copy never leaves ``clone_dir`` for the network clone to trip over. Raises
    WorkspaceTemplateCacheError when git cannot check the copy out or it is at another
    commit, and OSError when the files cannot be copied.
    """
    started_at = time.monotonic()
    copying_dir.mkdir(parents=True, exist_ok=False)
    source_git_dir = cached_clone.path / ".git"
    shutil.copytree(
        source_git_dir,
        copying_dir / ".git",
        symlinks=True,
        ignore=_GitDirCopyFilter(git_dir=source_git_dir),
        copy_function=_ObjectHardlinkingCopier(objects_dir=source_git_dir / "objects"),
    )
    _run_git(["read-tree", "--reset", "-u", "HEAD"], copying_dir, concurrency_group)
    copied_commit = _read_fetched_commit(copying_dir, concurrency_group)
    if copied_commit != cached_clone.commit:
        raise WorkspaceTemplateCacheError(
            f"The copied clone is at {copied_commit}, not the cached {cached_clone.commit}"
        )
    copying_dir.rename(clone_dir)
    elapsed_seconds = time.monotonic() - started_at
    if elapsed_seconds > _SLOW_COPY_WARNING_SECONDS:
        logger.warning("Copying the cached workspace template into {} took {:.1f}s", clone_dir, elapsed_seconds)


def _delete_path(path: Path) -> None:
    """Delete a file or directory tree; whatever cannot be deleted is left for a later launch's cleanup."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            logger.debug("Could not delete all of {}", path)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("Could not delete {}: {}", path, e)


def _is_modified_before(path: Path, cutoff: float) -> bool:
    try:
        modified_at = path.lstat().st_mtime
    except OSError as e:
        logger.debug("Could not read when {} was modified: {}", path, e)
        return False
    return modified_at < cutoff


def _tree_size(path: Path) -> int:
    """The bytes in the files under ``path``, which grow while git downloads into it and checks it out."""
    total = 0
    for directory, _, file_names in os.walk(path):
        for file_name in file_names:
            try:
                total += os.lstat(os.path.join(directory, file_name)).st_size
            except OSError as e:
                # Git renames its temporary files as it goes.
                logger.trace("Not counting {} toward the download's progress: {}", file_name, e)
                continue
    return total


def _discard_cache_path(path: Path) -> None:
    """Delete ``path``, first moving it off its name so a create copying from it fails rather than copying half a tree."""
    discarded = path.with_name(_DISCARDED_DIR_PREFIX + uuid4().hex)
    try:
        path.rename(discarded)
    except OSError as e:
        logger.debug("Could not move {} aside to delete it: {}", path, e)
        return
    _delete_path(discarded)


def _remove_stale_cache_paths(cache_dir: Path, kept_entry_name: str) -> None:
    """Delete the cache's clones of other releases, and its clones or deletes that a crash cut short."""
    cutoff = time.time() - _ORPHANED_DIR_AGE_SECONDS
    try:
        paths = tuple(cache_dir.iterdir())
    except OSError as e:
        logger.debug("Could not list the template cache at {}: {}", cache_dir, e)
        return
    for path in paths:
        if path.name == kept_entry_name:
            continue
        if _ENTRY_NAME_RE.match(path.name) is not None:
            _discard_cache_path(path)
        elif path.name.startswith((_INCOMING_DIR_PREFIX, _DISCARDED_DIR_PREFIX)) and _is_modified_before(path, cutoff):
            _delete_path(path)
        else:
            logger.trace("Leaving {} in the template cache", path)


class WorkspaceTemplateCache(TemplateCloneCacheInterface):
    """Keeps a checked clone of one pinned template release on disk, so a create of that release copies it instead of cloning it."""

    repo_url: GitUrl = Field(frozen=True, description="The template repository, exactly as a create names it")
    release_tag: TemplateReleaseTag = Field(frozen=True, description="The release the cache holds a clone of")
    cache_dir: Path = Field(frozen=True, description="Directory the cache owns: the clone and its temporary siblings")
    concurrency_group: ConcurrencyGroup = Field(
        frozen=True, description="Group the prefetch thread and every git command run under"
    )
    clone_stall_timeout_seconds: int = Field(
        default=_CLONE_STALL_TIMEOUT_SECONDS,
        frozen=True,
        description="The prefetch's download from an HTTP(S) remote gives up after this long receiving almost nothing",
    )
    clone_timeout_seconds: float = Field(
        default=_CLONE_TIMEOUT_SECONDS, frozen=True, description="Bound on each git step of the prefetch's clone"
    )
    prefetch_retry_waits_seconds: tuple[float, ...] = Field(
        default=_PREFETCH_RETRY_WAITS_SECONDS,
        frozen=True,
        description="Waits before each retry of a failed prefetch; none are left after the last",
    )

    _status: WorkspaceTemplateCacheStatus = PrivateAttr(default=WorkspaceTemplateCacheStatus.IDLE)
    _cached_clone: CachedTemplateClone | None = PrivateAttr(default=None)
    _incoming_dir: Path | None = PrivateAttr(default=None)
    _condition: threading.Condition = PrivateAttr(default_factory=threading.Condition)

    @property
    def status(self) -> WorkspaceTemplateCacheStatus:
        with self._condition:
            return self._status

    @property
    def entry_dir(self) -> Path:
        return self.cache_dir / template_cache_entry_name(self.repo_url, self.release_tag)

    def start_prefetch(self) -> None:
        """Make the release's clone ready on a background thread, retrying a failure a few times."""
        # FETCHING before the thread starts, so a create that arrives first waits for it.
        self._publish(WorkspaceTemplateCacheStatus.FETCHING, None)
        start_mngr_thread(
            self.concurrency_group,
            target=self._prefetch_with_retries,
            name="workspace-template-prefetch",
            # Best-effort: a failed prefetch only means creates clone from the network.
            is_checked=False,
        )

    def prefetch_once(self) -> WorkspaceTemplateCacheStatus:
        """Reuse the release's clone if it is sound and current, else clone it again; returns the resulting status."""
        self._publish(WorkspaceTemplateCacheStatus.FETCHING, None)
        try:
            cached_clone = self._find_or_clone_release()
            self._publish(
                WorkspaceTemplateCacheStatus.READY
                if cached_clone is not None
                else WorkspaceTemplateCacheStatus.UNAVAILABLE,
                cached_clone,
            )
        finally:
            # An unexpected error must not leave creates waiting on a prefetch that has ended.
            self._publish_unavailable_if_fetching()
        return self.status

    def materialize_clone(
        self,
        repo_url: str,
        ref: str,
        clone_dir: Path,
        stall_timeout_seconds: float,
        on_waiting: Callable[[], None],
    ) -> bool:
        if repo_url != self.repo_url or ref != self.release_tag:
            return False
        cached_clone = self._wait_for_cached_clone(stall_timeout_seconds, on_waiting)
        if cached_clone is None:
            return False
        copying_dir = clone_dir.with_name(_COPYING_DIR_PREFIX + uuid4().hex)
        try:
            with log_span("Copying the cached workspace template {} into {}", self.release_tag, clone_dir):
                _copy_cached_clone(cached_clone, copying_dir, clone_dir, self.concurrency_group)
        except (OSError, WorkspaceTemplateCacheError) as e:
            logger.warning("Could not copy the cached workspace template {}, so cloning it: {}", self.release_tag, e)
            _delete_path(copying_dir)
            if not self._is_still_at_commit(cached_clone):
                self._stop_serving(cached_clone)
            return False
        return True

    def _prefetch_with_retries(self) -> None:
        for retry_wait_seconds in (*self.prefetch_retry_waits_seconds, None):
            if self.prefetch_once() is WorkspaceTemplateCacheStatus.READY or retry_wait_seconds is None:
                return
            # Interruptible backoff: wait() returns True once the app is shutting down.
            if self.concurrency_group.shutdown_event.wait(retry_wait_seconds):
                return

    def _find_or_clone_release(self) -> CachedTemplateClone | None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Could not create the workspace template cache at {}: {}", self.cache_dir, e)
            return None
        _remove_stale_cache_paths(self.cache_dir, kept_entry_name=self.entry_dir.name)
        existing_clone = self._check_existing_clone()
        if existing_clone is not None:
            return existing_clone
        return self._clone_release()

    def _check_existing_clone(self) -> CachedTemplateClone | None:
        """The published clone, unless it is missing, damaged, or no longer at the commit the tag names upstream."""
        if not self.entry_dir.is_dir():
            return None
        try:
            commit = _read_fetched_commit(self.entry_dir, self.concurrency_group)
        except WorkspaceTemplateCacheError as e:
            logger.warning("Discarding the damaged workspace template clone at {}: {}", self.entry_dir, e)
            _discard_cache_path(self.entry_dir)
            return None
        # Offline, the remote cannot be asked; a release tag is never moved, so keep the clone.
        remote_commit = resolve_release_tag_commit(
            repo_url=self.repo_url, release_tag=self.release_tag, concurrency_group=self.concurrency_group
        )
        if remote_commit is not None and remote_commit != commit:
            logger.warning(
                "{} now names {} upstream, not the cached {}; cloning it again",
                self.release_tag,
                remote_commit,
                commit,
            )
            _discard_cache_path(self.entry_dir)
            return None
        return CachedTemplateClone(path=self.entry_dir, commit=commit)

    def _clone_release(self) -> CachedTemplateClone | None:
        """Clone the release beside the cache's entry and publish it there with one rename, so no create sees half a clone."""
        incoming_dir = self.cache_dir / (_INCOMING_DIR_PREFIX + uuid4().hex)
        with self._condition:
            self._incoming_dir = incoming_dir
        try:
            with log_span("Cloning {} at {} into the workspace template cache", self.repo_url, self.release_tag):
                clone_git_repo(
                    self.repo_url,
                    incoming_dir,
                    branch=self.release_tag,
                    parent_cg=self.concurrency_group,
                    timeout_seconds=self.clone_timeout_seconds,
                    git_config={
                        "http.lowSpeedLimit": str(_CLONE_LOW_SPEED_LIMIT_BYTES_PER_SECOND),
                        "http.lowSpeedTime": str(self.clone_stall_timeout_seconds),
                        _FSMONITOR_CONFIG_KEY: "false",
                    },
                )
        except (GitCloneError, OSError) as e:
            logger.warning("Could not prefetch the workspace template {}: {}", self.release_tag, e)
            _delete_path(incoming_dir)
            return None
        try:
            commit = _read_fetched_commit(incoming_dir, self.concurrency_group)
        except WorkspaceTemplateCacheError as e:
            logger.warning("Discarding a prefetched workspace template clone that did not check out: {}", e)
            _delete_path(incoming_dir)
            return None
        try:
            incoming_dir.rename(self.entry_dir)
        except OSError as e:
            logger.debug("Another process published {} first ({}); checking its clone", self.release_tag, e)
            _delete_path(incoming_dir)
            return self._check_existing_clone()
        return CachedTemplateClone(path=self.entry_dir, commit=commit)

    def _wait_for_cached_clone(
        self, stall_timeout_seconds: float, on_waiting: Callable[[], None]
    ) -> CachedTemplateClone | None:
        """The clone to copy once no prefetch is running, or None if there is none or the prefetch stops receiving data."""
        with self._condition:
            is_settled = self._is_settled()
            status = self._status
            cached_clone = self._cached_clone
        if not is_settled:
            on_waiting()
        poll_seconds = min(_PROGRESS_POLL_SECONDS, stall_timeout_seconds / 4)
        progress: tuple[Path | None, int] | None = None
        progressed_at = time.monotonic()
        while not is_settled:
            with self._condition:
                is_settled = self._condition.wait_for(self._is_settled, timeout=poll_seconds)
                status = self._status
                cached_clone = self._cached_clone
                incoming_dir = self._incoming_dir
            if is_settled:
                break
            latest_progress = (incoming_dir, _tree_size(incoming_dir) if incoming_dir is not None else 0)
            now = time.monotonic()
            if latest_progress != progress:
                progress = latest_progress
                progressed_at = now
            if now - progressed_at >= stall_timeout_seconds:
                logger.warning(
                    "The workspace template prefetch has made no progress for {:.0f}s; cloning instead",
                    stall_timeout_seconds,
                )
                return None
        if cached_clone is None:
            logger.debug("The workspace template cache is {}; cloning instead", status)
        return cached_clone

    def _is_settled(self) -> bool:
        return self._status is not WorkspaceTemplateCacheStatus.FETCHING

    def _publish(self, status: WorkspaceTemplateCacheStatus, cached_clone: CachedTemplateClone | None) -> None:
        with self._condition:
            self._status = status
            self._cached_clone = cached_clone
            self._incoming_dir = None
            self._condition.notify_all()

    def _publish_unavailable_if_fetching(self) -> None:
        with self._condition:
            if self._status is WorkspaceTemplateCacheStatus.FETCHING:
                self._status = WorkspaceTemplateCacheStatus.UNAVAILABLE
                self._cached_clone = None
                self._incoming_dir = None
                self._condition.notify_all()

    def _is_still_at_commit(self, cached_clone: CachedTemplateClone) -> bool:
        """Whether the cached clone still names its commit, so a failed copy was the destination's fault, not the cache's."""
        try:
            return _read_fetched_commit(cached_clone.path, self.concurrency_group) == cached_clone.commit
        except WorkspaceTemplateCacheError as e:
            logger.warning("The cached workspace template at {} is no longer usable: {}", cached_clone.path, e)
            return False

    def _stop_serving(self, cached_clone: CachedTemplateClone) -> None:
        """Stop handing out a clone that failed to copy (a later prefetch may have replaced it already)."""
        with self._condition:
            if self._cached_clone == cached_clone:
                self._status = WorkspaceTemplateCacheStatus.UNAVAILABLE
                self._cached_clone = None
                self._condition.notify_all()


def make_workspace_template_cache(
    *, repo_url: str, pinned_ref: str, cache_dir: Path, concurrency_group: ConcurrencyGroup
) -> WorkspaceTemplateCache | None:
    """The cache of the app's pinned template release, or None (every create clones) if the pin is not a release tag."""
    try:
        release_tag = TemplateReleaseTag(pinned_ref)
    except InvalidTemplateReleaseTagError as e:
        logger.warning("Not caching the workspace template, so every create clones it: {}", e)
        return None
    return WorkspaceTemplateCache(
        repo_url=GitUrl(repo_url), release_tag=release_tag, cache_dir=cache_dir, concurrency_group=concurrency_group
    )
