import shlex
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.pure import pure
from imbue.mng.errors import MngError
from imbue.mng.interfaces.host import OnlineHostInterface


def add_safe_directory_on_remote(host: OnlineHostInterface, path: Path) -> None:
    """Add a git safe.directory entry on a remote host.

    On remote hosts (Docker/Modal), file ownership may differ from the SSH user
    (e.g., after rsync from a local machine with a different UID). This tells
    git to trust the given directory regardless of ownership.

    No-op for local hosts, where the current user already owns the directories.
    """
    if host.is_local:
        return
    host.execute_command(
        f"git config --global --add safe.directory {shlex.quote(str(path))}",
    )


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


def get_current_git_branch(path: Path | None, cg: ConcurrencyGroup) -> str | None:
    """Get the current git branch name for the repository at the given path.

    Returns None if the path is not a git repository or an error occurs.
    """
    try:
        cwd = path or Path.cwd()
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
        )
        return result.stdout.strip()
    except ProcessError as e:
        logger.trace("Failed to get current git branch: {}", e)
        return None


def derive_project_name_from_path(path: Path, cg: ConcurrencyGroup) -> str:
    """Derive a project name from a path.

    Attempts to extract the project name from the git remote origin URL if available.
    Falls back to the folder name if there is no git repository or the URL structure
    is not recognized.
    """
    # Try to get the git remote origin URL
    git_project_name = _get_project_name_from_git_remote(path, cg)
    if git_project_name is not None:
        return git_project_name

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
        return _parse_project_name_from_url(result.stdout.strip())
    except ProcessError as e:
        logger.trace("Failed to get project name from git remote URL: {}", e)
        return None


@pure
def _parse_project_name_from_url(url: str) -> str | None:
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


def _get_git_config_value(path: Path, key: str, cg: ConcurrencyGroup) -> str | None:
    """Get a git config value for the repository at the given path."""
    try:
        result = cg.run_process_to_completion(
            ["git", "config", key],
            cwd=path,
        )
    except ProcessError:
        return None
    if result.stdout.strip():
        return result.stdout.strip()
    return None


def get_git_author_info(path: Path, cg: ConcurrencyGroup) -> tuple[str | None, str | None]:
    """Get the git author name and email for the repository at the given path."""
    return _get_git_config_value(path, "user.name", cg), _get_git_config_value(path, "user.email", cg)


def get_git_remote_url(path: Path, remote_name: str, cg: ConcurrencyGroup) -> str | None:
    """Get the URL of a git remote for the repository at the given path.

    Returns None if the remote does not exist or the path is not a git repo.
    """
    try:
        result = cg.run_process_to_completion(
            ["git", "remote", "get-url", remote_name],
            cwd=path,
        )
    except ProcessError:
        return None
    url = result.stdout.strip()
    return url if url else None


def find_git_worktree_root(start: Path | None, cg: ConcurrencyGroup) -> Path | None:
    """Find the git worktree root."""
    cwd = start or Path.cwd()
    try:
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
        )
        return Path(result.stdout.strip())
    except ProcessError as e:
        logger.trace("Failed to find worktree root: {}", e)
        return None


def is_git_repository(path: Path, cg: ConcurrencyGroup) -> bool:
    """Check if the given path is inside a git repository.

    Works from any subdirectory within a git worktree.
    Returns False if the path does not exist.
    """
    if not path.exists():
        return False
    try:
        cg.run_process_to_completion(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
        )
        return True
    except ProcessError:
        return False


def get_current_branch(path: Path, cg: ConcurrencyGroup) -> str:
    """Get the current branch name for a git repository.

    Unlike get_current_git_branch, this function raises an error if the operation
    fails rather than returning None. Also raises if HEAD is detached (no branch),
    since callers need an actual branch name for push/pull operations.
    """
    try:
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
        )
    except ProcessError as e:
        raise MngError(f"Failed to get current branch: {e.stderr}") from e
    branch = result.stdout.strip()
    if branch == "HEAD":
        raise MngError(f"HEAD is detached in {path}. A branch checkout is required for sync operations.")
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
    """
    try:
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=path,
        )
        git_common_dir = Path(result.stdout.strip())
        if not git_common_dir.is_absolute():
            git_common_dir = (path / git_common_dir).resolve()
        return git_common_dir
    except ProcessError as e:
        logger.trace("Failed to find main .git dir: {}", e)
        return None
