import os
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError

_GIT_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "ssh", "git", "file"})
# SCP-like SSH URL: user@host:path. Host part allows [\w.-]; path must be nonempty
# and must not start with / or : (to avoid matching "user@host:/abs/path" handled by ssh-style parse).
_SCP_URL_RE: Final[re.Pattern[str]] = re.compile(r"^[\w.-]+@[\w.-]+:[^/:][^:]*$")
_GIT_CLONE_TIMEOUT_SECONDS: Final[float] = 600.0

# Refspecs that replicate `git push --mirror` behavior for branches and tags,
# without pushing remote-tracking refs (refs/remotes/*). Pushing symbolic
# remote-tracking refs like refs/remotes/origin/HEAD causes "inconsistent
# aliased update" errors on git 2.45+.
GIT_MIRROR_PUSH_REFSPECS: Final[list[str]] = [
    "+refs/heads/*:refs/heads/*",
    "+refs/tags/*:refs/tags/*",
]


@pure
def parse_worktree_git_file(content: str) -> Path | None:
    """Parse the content of a worktree's .git file to find the source repo.

    A worktree's .git file contains a line like:
        gitdir: /path/to/main/repo/.git/worktrees/<id>

    Returns the source repo directory, or None if the content doesn't match.
    """
    content = content.strip()
    if not content.startswith("gitdir: "):
        return None

    gitdir = Path(content.removeprefix("gitdir: ").strip())
    # gitdir points to: <repo>/.git/worktrees/<agent-id>
    dot_git = gitdir.parent.parent
    if dot_git.name != ".git":
        return None
    return dot_git.parent


def find_source_repo_of_worktree(worktree_path: Path) -> Path | None:
    """Find the source repository of a git worktree by reading its .git file.

    Returns the source repo directory, or None if the path is not a worktree.
    """
    try:
        content = (worktree_path / ".git").read_text()
    except (FileNotFoundError, OSError):
        return None
    return parse_worktree_git_file(content)


def remove_worktree(worktree_path: Path, source_repo_path: Path, cg: ConcurrencyGroup) -> None:
    """Remove a git worktree, running git from the source repository.

    Raises ProcessError if the removal fails.
    """
    cg.run_process_to_completion(
        ["git", "-C", str(source_repo_path), "worktree", "remove", "--force", str(worktree_path)],
    )


def delete_git_branch(branch_name: str, source_repo_path: Path, cg: ConcurrencyGroup) -> bool:
    """Delete a git branch from the source repository.

    Returns True on successful deletion, False otherwise. Failures are logged
    as warnings; this never raises.
    """
    try:
        result = cg.run_process_to_completion(
            ["git", "-C", str(source_repo_path), "branch", "-D", branch_name],
            is_checked_after=False,
        )
    except ProcessError as e:
        logger.warning("Failed to delete branch {}: {}", branch_name, e)
        return False
    if result.returncode == 0:
        return True
    logger.warning("Failed to delete branch {}: {}", branch_name, result.stderr.strip())
    return False


# Exclude patterns used when rsyncing a worktree's working tree over
# the standalone clone produced for a docker build context. Mirrors the
# defaults of dev-iteration rsyncs (.git, __pycache__, etc.) so the
# build context isn't bloated with caches / virtualenvs / installed
# node modules. ``.git`` is excluded because the clone already has a
# standalone ``.git`` directory we must not clobber.
RSYNC_WORKTREE_OVERLAY_EXCLUDES: Final[tuple[str, ...]] = (
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".test_output",
)


def rsync_worktree_over_clone(
    worktree_dir: Path,
    clone_dir: Path,
    cg: ConcurrencyGroup,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Rsync a worktree's working directory over a fresh ``git clone`` of it.

    Why: ``git clone file://<worktree>`` produces a build context with a
    proper standalone ``.git`` directory (vs the worktree's 93-byte
    gitlink file), but it only ships *committed* files. Callers that
    rely on uncommitted edits being in the build context (e.g. the
    minds desktop client's ``--mngr-source``-equivalent path, and
    ``mngr_vps``'s docker-build-context assembly) need to
    overlay the worktree's working tree on top of the clone to recover
    them.

    Preserves the clone's ``.git`` (excluded from the rsync) and skips
    the usual caches / virtualenvs / installed node_modules so the
    build context stays small.

    Failures are logged at warning and swallowed -- the clone alone
    still produces a usable (if stale) build context, and we'd rather
    proceed with a possibly-stale build than fail the whole bake on a
    transient rsync error.
    """
    logger.debug("Rsyncing worktree {} over clone {}", worktree_dir, clone_dir)
    command: list[str] = ["rsync", "-a", "--delete"]
    for pattern in RSYNC_WORKTREE_OVERLAY_EXCLUDES:
        command.append(f"--exclude={pattern}")
    command.extend([f"{worktree_dir}/", f"{clone_dir}/"])
    result = cg.run_process_to_completion(
        command=command,
        is_checked_after=False,
        on_output=on_output,
    )
    if result.returncode != 0:
        logger.warning(
            "rsync worktree over clone exited with code {}: {}",
            result.returncode,
            result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
        )


def resolve_project_filter_values(
    values: tuple[str, ...],
    cg: ConcurrencyGroup,
    *,
    project_root: Path | None = None,
) -> tuple[str, ...]:
    """Resolve --project filter values, expanding "." to the current project name.

    The current project is derived from ``project_root`` when provided (typically
    ``MngrContext.project_root``, the git worktree root), falling back to the
    current working directory when not. This is important because running from a
    subdirectory would otherwise miss the git remote and yield the subdirectory's
    name. Other values are returned unchanged. The current project is derived at
    most once. Duplicate values (after expansion) are collapsed while preserving
    insertion order so the resulting CEL clause stays minimal.
    """
    current_project: str | None = None
    resolved: dict[str, None] = {}
    for value in values:
        if value == ".":
            if current_project is None:
                current_project = derive_project_name_from_path(project_root or Path.cwd(), cg)
            resolved[current_project] = None
        else:
            resolved[value] = None
    return tuple(resolved)


def build_project_filter_clause(
    values: tuple[str, ...],
    cg: ConcurrencyGroup,
    *,
    project_root: Path | None = None,
) -> str | None:
    """Build a CEL include clause for filtering agents by project label.

    Returns ``None`` when ``values`` is empty, so callers can simply skip the
    filter append. Otherwise expands "." sentinels via
    ``resolve_project_filter_values`` and returns an OR-joined CEL clause like
    ``labels.project == "foo" || labels.project == "bar"``. ``project_root``
    is forwarded to ``resolve_project_filter_values`` (see its docstring).
    """
    if not values:
        return None
    project_names = resolve_project_filter_values(values, cg, project_root=project_root)
    return " || ".join(f'labels.project == "{p}"' for p in project_names)


def derive_project_name_for_source(
    path: Path,
    *,
    remote_url: str | None = None,
    source_project_label: str | None = None,
) -> str:
    """Derive a project name for a source location.

    Priority:
    1. ``source_project_label`` -- e.g. inherited from a source agent's label.
    2. ``remote_url`` -- useful when the URL has already been fetched (which works
       for remote sources where shelling to a local git binary would not).
    3. Fall back to ``path``'s directory name (resolved to normalize symlinks /
       ``..`` components).

    The path fallback intentionally does *not* shell out to git: callers are
    expected to have already fetched the remote URL via the source's own host
    (which works for both local and remote sources). Re-running git locally
    against a remote-source path would be either redundant or incorrect.
    """
    if source_project_label is not None:
        return source_project_label
    if remote_url is not None:
        from_url = parse_project_name_from_url(remote_url)
        if from_url is not None:
            return from_url
    return path.resolve().name


def derive_project_name_from_path(path: Path, cg: ConcurrencyGroup) -> str:
    """Derive a project name from a path.

    Attempts to extract the project name from the git remote origin URL if available
    (for worktrees, this already checks the source repo's remotes since they share
    git config). Falls back to the source repository's directory name (for worktrees)
    or the given path's directory name.
    """
    # Try to get the project name from the git remote origin URL
    git_project_name = _get_project_name_from_git_remote(path, cg)
    if git_project_name is not None:
        return git_project_name

    # For worktrees, use the source repo's directory name instead of the worktree's
    # (which is often a generated name like "branch-name-<hash>")
    source_repo = find_source_repo_of_worktree(path)
    if source_repo is not None:
        return source_repo.resolve().name

    # Fallback to the folder name
    return path.resolve().name


def _get_project_name_from_git_remote(path: Path, cg: ConcurrencyGroup) -> str | None:
    """Get the project name from the git remote origin URL.

    Supports GitHub and GitLab URL formats:
    - https://github.com/owner/repo.git
    - git@github.com:owner/repo.git
    - https://gitlab.com/owner/repo.git
    - git@gitlab.com:owner/repo.git

    Returns None if not a git repo or URL format is unknown.
    """
    # Check if this is a git repository
    git_dir = path / ".git"
    if not git_dir.exists():
        return None

    # Try to get the remote origin URL
    try:
        result = cg.run_process_to_completion(
            ["git", "remote", "get-url", "origin"],
            cwd=path,
            timeout=5,
        )
        return parse_project_name_from_url(result.stdout.strip())
    except ProcessError as e:
        logger.trace("Failed to get project name from git remote URL: {}", e)
        return None


@pure
def parse_project_name_from_url(url: str) -> str | None:
    """Parse the project name from a git remote URL.

    Returns None if the URL format is not recognized.
    """
    # Handle SSH-style URLs (e.g., git@github.com:owner/repo.git)
    if "@" in url and ":" in url:
        parts = url.split(":")
        if len(parts) == 2:
            path_part = parts[1]
            if path_part.endswith(".git"):
                path_part = path_part[:-4]
            project_name = path_part.split("/")[-1]
            if project_name:
                return project_name

    # Handle HTTPS URLs (e.g., https://github.com/owner/repo.git)
    try:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            if parsed.path:
                path = parsed.path.strip("/")
                if path.endswith(".git"):
                    path = path[:-4]
                project_name = path.split("/")[-1]
                if project_name:
                    return project_name
    except ValueError:
        pass
    return None


@pure
def is_git_url(source: str) -> bool:
    """Return True if `source` looks like a git URL that can be cloned.

    Recognizes explicit schemes (http/https/ssh/git) and the SCP-like SSH form
    (user@host:path). Narrower than the bare-name grammar used for agents:
    SCP-form requires a slash in the path or a `.git` suffix, so `name@host.modal`
    is not mistaken for a git URL.
    """
    if not source:
        return False

    parsed = urlparse(source)
    if parsed.scheme in _GIT_URL_SCHEMES:
        return True

    if _SCP_URL_RE.match(source):
        path_part = source.split(":", 1)[1]
        if source.endswith(".git") or "/" in path_part:
            return True

    return False


def clone_git_url_to_managed_dir(url: str, base_dir: Path, name: str, cg: ConcurrencyGroup) -> Path:
    """Clone a git URL into `base_dir/<name>-<uuid>/` and return the destination path.

    Raises UserInputError on clone failure. Best-effort removes a half-populated
    destination directory on failure so the caller sees a clean filesystem.
    """
    dest = base_dir / f"{name}-{uuid.uuid4().hex}"
    base_dir.mkdir(parents=True, exist_ok=True)
    try:
        cg.run_process_to_completion(
            ["git", "clone", url, str(dest)],
            timeout=_GIT_CLONE_TIMEOUT_SECONDS,
        )
    except ProcessError as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise UserInputError(f"Failed to clone {url}: {e.stderr}") from e
    return dest


def _git_env_with_stable_locale() -> dict[str, str]:
    """The current environment with a forced C locale for git subprocesses.

    The repo-detection helpers below key off git's "not a git repository" stderr
    message to tell the expected no-repo case apart from genuine failures.
    Forcing ``LC_ALL=C`` keeps that message in English regardless of the ambient
    locale, so the check cannot be defeated by a localized git. ``env`` replaces
    the whole environment for the subprocess, so the current environment is
    merged in rather than dropped.
    """
    return {**os.environ, "LC_ALL": "C"}


def _is_not_a_git_repository_error(error: ProcessError) -> bool:
    """Whether a git ``ProcessError`` is git's clean "not a git repository" result.

    That is the one git failure callers treat as a normal answer ("this path is
    not in a git repo") rather than an error. Spawn failures and timeouts
    (``ProcessError`` subclasses whose ``returncode`` is None) and any other
    non-zero exit are not this case and must surface loudly rather than be
    quietly turned into a "no repo" sentinel.
    """
    return error.returncode is not None and "not a git repository" in error.stderr.lower()


def find_git_worktree_root(start: Path | None, cg: ConcurrencyGroup) -> Path | None:
    """Find the git worktree root, or None if `start` is not inside a git repository.

    Only git's own "not a git repository" result maps to None -- that is the one
    case where "no worktree root" is a normal, expected answer, and every caller
    treats None as "not in a repo / no project root". Any other failure (git
    missing, a timeout, a failure to spawn the subprocess, or an unexpected
    non-zero exit) is raised rather than swallowed, so transient or environmental
    problems surface loudly instead of silently dropping the project config layer
    and leaving callers to misbehave with no explanation.
    """
    cwd = start or Path.cwd()
    try:
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            env=_git_env_with_stable_locale(),
        )
    except ProcessError as e:
        if _is_not_a_git_repository_error(e):
            return None
        raise
    return Path(result.stdout.strip())


def is_git_repository(path: Path, cg: ConcurrencyGroup) -> bool:
    """Check if the given path is inside a git repository.

    Works from any subdirectory within a git worktree.
    Returns False if the path does not exist or is cleanly reported by git as not
    being a git repository. Any other git failure is raised rather than swallowed
    into a misleading False (see ``_is_not_a_git_repository_error``).
    """
    if not path.exists():
        return False
    try:
        cg.run_process_to_completion(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            env=_git_env_with_stable_locale(),
        )
    except ProcessError as e:
        if _is_not_a_git_repository_error(e):
            return False
        raise
    return True


def get_current_branch(path: Path, cg: ConcurrencyGroup) -> str:
    """Get the current branch name for a git repository.

    Raises MngrError if the operation fails or if HEAD is detached, since callers
    need an actual branch name for push/pull operations.
    """
    try:
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
        )
    except ProcessError as e:
        raise MngrError(f"Failed to get current branch: {e.stderr}") from e
    branch = result.stdout.strip()
    if branch == "HEAD":
        raise MngrError(f"HEAD is detached in {path}. A branch checkout is required for sync operations.")
    return branch


def get_head_commit(path: Path, cg: ConcurrencyGroup) -> str | None:
    """Get the current HEAD commit hash for a repository."""
    try:
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
        )
    except ProcessError:
        return None
    return result.stdout.strip()


def is_ancestor(path: Path, ancestor_commit: str, descendant_commit: str, cg: ConcurrencyGroup) -> bool:
    """Check if ancestor_commit is an ancestor of descendant_commit."""
    try:
        cg.run_process_to_completion(
            ["git", "merge-base", "--is-ancestor", ancestor_commit, descendant_commit],
            cwd=path,
        )
        return True
    except ProcessError:
        return False


def count_commits_between(path: Path, base_ref: str, head_ref: str, cg: ConcurrencyGroup) -> int:
    """Count the number of commits between two refs (base_ref..head_ref)."""
    try:
        result = cg.run_process_to_completion(
            ["git", "rev-list", "--count", f"{base_ref}..{head_ref}"],
            cwd=path,
        )
    except ProcessError as e:
        logger.debug("Failed to count commits between {} and {}: {}", base_ref, head_ref, e.stderr.strip())
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def find_git_common_dir(path: Path, cg: ConcurrencyGroup) -> Path | None:
    """Find the common .git directory for a repository or worktree.

    For a regular repository, this returns the .git directory.
    For a worktree, this returns the main repository's .git directory,
    not the worktree's .git file.

    Returns None only when git cleanly reports the path is not a git repository;
    any other git failure is raised rather than swallowed (see
    ``_is_not_a_git_repository_error``).
    """
    try:
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=path,
            env=_git_env_with_stable_locale(),
        )
    except ProcessError as e:
        if _is_not_a_git_repository_error(e):
            return None
        raise
    git_common_dir = Path(result.stdout.strip())
    if not git_common_dir.is_absolute():
        git_common_dir = (path / git_common_dir).resolve()
    return git_common_dir


def find_git_source_path(path: Path, cg: ConcurrencyGroup) -> Path | None:
    """Find the source repository root for ``path``, if it is inside a git repo.

    Returns the parent of the git common dir (the source repo root) -- for a
    worktree this is the main repository, for a regular checkout it is the repo
    itself -- or ``None`` if ``path`` is not inside a git repo. The source-path
    concept is what lets a single trust grant cover every worktree of the same
    repo: it is the durable thing agent plugins persist.
    """
    git_common_dir = find_git_common_dir(path, cg)
    if git_common_dir is None:
        return None
    return git_common_dir.parent
