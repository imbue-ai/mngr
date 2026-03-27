"""Parent lineage tracking for mind repositories.

Tracks the original repository, branch, and commit hash that a mind was
created from by writing a ``.parent`` file (git config format) in the
mind's root directory.

The ``.parent`` file records three values under the ``[parent]`` section::

    [parent]
        url = https://github.com/org/repo.git
        branch = main
        hash = abc123...

This information is used by the ``mind update`` command to fetch and merge
the latest changes from the parent repository.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.errors import ParentTrackingError
from imbue.minds.forwarding_server.vendor_mngr import ensure_git_identity
from imbue.minds.forwarding_server.vendor_mngr import run_git
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitCommitHash
from imbue.minds.primitives import GitUrl

PARENT_FILE_NAME: Final[str] = ".parent"

MIND_BRANCH_PREFIX: Final[str] = "minds/"

_ERR = ParentTrackingError


class ParentInfo(FrozenModel):
    """Lineage information for a mind's parent repository.

    Stores the URL, branch, and commit hash of the repository that was
    cloned to create the mind.
    """

    url: GitUrl = Field(description="Git URL of the parent repository")
    branch: GitBranch = Field(description="Branch name in the parent repository")
    hash: GitCommitHash = Field(description="Commit hash at the time of creation or last update")


def get_current_branch(repo_dir: Path) -> GitBranch:
    """Return the current branch name of a git repository.

    Returns ``HEAD`` if the repository is in detached HEAD state.
    """
    return GitBranch(
        run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_dir,
            error_message="Failed to get current branch of {}".format(repo_dir),
            error_class=_ERR,
        ).strip()
    )


def get_current_commit_hash(repo_dir: Path) -> GitCommitHash:
    """Return the full commit hash of HEAD in a git repository."""
    return GitCommitHash(
        run_git(
            ["rev-parse", "HEAD"],
            cwd=repo_dir,
            error_message="Failed to get current commit hash of {}".format(repo_dir),
            error_class=_ERR,
        ).strip()
    )


def checkout_mind_branch(
    repo_dir: Path,
    mind_name: AgentName,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Create and switch to a new branch named ``minds/<mind_name>``.

    Raises ParentTrackingError if the branch already exists or the checkout fails.
    """
    branch_name = "{}{}".format(MIND_BRANCH_PREFIX, mind_name)
    logger.debug("Checking out new branch: {}", branch_name)
    run_git(
        ["checkout", "-b", branch_name],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to create branch {}".format(branch_name),
        error_class=_ERR,
    )


def write_parent_info(
    repo_dir: Path,
    parent_info: ParentInfo,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Write parent lineage information to the ``.parent`` file.

    Uses ``git config --file`` to write a git-config-formatted file.
    Overwrites any existing ``.parent`` file.
    """
    parent_file = str(repo_dir / PARENT_FILE_NAME)
    logger.debug(
        "Writing parent info to {}: url={}, branch={}, hash={}",
        parent_file,
        parent_info.url,
        parent_info.branch,
        parent_info.hash,
    )

    run_git(
        ["config", "--file", parent_file, "parent.url", str(parent_info.url)],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to write parent.url to {}".format(parent_file),
        error_class=_ERR,
    )
    run_git(
        ["config", "--file", parent_file, "parent.branch", str(parent_info.branch)],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to write parent.branch to {}".format(parent_file),
        error_class=_ERR,
    )
    run_git(
        ["config", "--file", parent_file, "parent.hash", str(parent_info.hash)],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to write parent.hash to {}".format(parent_file),
        error_class=_ERR,
    )


def read_parent_info(repo_dir: Path) -> ParentInfo:
    """Read parent lineage information from the ``.parent`` file.

    Raises ParentTrackingError if the file does not exist or cannot be read.
    """
    parent_file = str(repo_dir / PARENT_FILE_NAME)

    url = run_git(
        ["config", "--file", parent_file, "parent.url"],
        cwd=repo_dir,
        error_message="Failed to read parent.url from {}".format(parent_file),
        error_class=_ERR,
    ).strip()
    branch = run_git(
        ["config", "--file", parent_file, "parent.branch"],
        cwd=repo_dir,
        error_message="Failed to read parent.branch from {}".format(parent_file),
        error_class=_ERR,
    ).strip()
    hash_value = run_git(
        ["config", "--file", parent_file, "parent.hash"],
        cwd=repo_dir,
        error_message="Failed to read parent.hash from {}".format(parent_file),
        error_class=_ERR,
    ).strip()

    return ParentInfo(url=GitUrl(url), branch=GitBranch(branch), hash=GitCommitHash(hash_value))


def commit_parent_file(
    repo_dir: Path,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Stage and commit the ``.parent`` file if it has changes.

    Creates a commit with a descriptive message. Ensures git identity
    is configured before committing. Skips the commit if the file has
    no staged changes (e.g. when the content is unchanged).
    """
    ensure_git_identity(repo_dir)
    run_git(
        ["add", PARENT_FILE_NAME],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to stage {}".format(PARENT_FILE_NAME),
        error_class=_ERR,
    )

    # Check if there are staged changes before committing
    diff_output = run_git(
        ["diff", "--cached", "--name-only"],
        cwd=repo_dir,
        error_message="Failed to check staged changes",
        error_class=_ERR,
    ).strip()

    if not diff_output:
        logger.debug("No changes to .parent file, skipping commit")
        return

    run_git(
        ["commit", "-m", "Record parent repository lineage"],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to commit {}".format(PARENT_FILE_NAME),
        error_class=_ERR,
    )


def setup_mind_branch_and_parent(
    repo_dir: Path,
    mind_name: AgentName,
    git_url: GitUrl,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Set up a mind's branch and parent tracking after cloning.

    Performs the following steps:
    1. Records the current branch and commit hash (the parent info)
    2. Creates and switches to a ``minds/<mind_name>`` branch
    3. Writes the ``.parent`` file with the parent lineage
    4. Commits the ``.parent`` file
    """
    parent_branch = get_current_branch(repo_dir)
    parent_hash = get_current_commit_hash(repo_dir)

    logger.debug("Parent branch: {}, hash: {}", parent_branch, parent_hash)

    checkout_mind_branch(repo_dir, mind_name, on_output)

    parent_info = ParentInfo(url=git_url, branch=parent_branch, hash=parent_hash)
    write_parent_info(repo_dir, parent_info, on_output)
    commit_parent_file(repo_dir, on_output)


def fetch_and_merge_parent(
    repo_dir: Path,
    parent_info: ParentInfo,
    on_output: Callable[[str, bool], None] | None = None,
) -> GitCommitHash:
    """Fetch the latest code from the parent repository and merge it.

    Fetches the parent branch and merges FETCH_HEAD into the current branch.
    After merging, updates the ``.parent`` file with the new commit hash
    and commits the change.

    Returns the new parent commit hash (the fetched HEAD).

    Raises ParentTrackingError if the fetch or merge fails (e.g. due to conflicts).
    """
    ensure_git_identity(repo_dir)

    logger.debug("Fetching from {} branch {}", parent_info.url, parent_info.branch)
    run_git(
        ["fetch", str(parent_info.url), str(parent_info.branch)],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to fetch from {} branch {}".format(parent_info.url, parent_info.branch),
        error_class=_ERR,
    )

    new_hash = GitCommitHash(
        run_git(
            ["rev-parse", "FETCH_HEAD"],
            cwd=repo_dir,
            error_message="Failed to resolve FETCH_HEAD",
            error_class=_ERR,
        ).strip()
    )

    logger.debug("Merging FETCH_HEAD ({})", new_hash)
    run_git(
        ["merge", "FETCH_HEAD", "--no-edit"],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to merge parent changes (there may be conflicts to resolve manually)",
        error_class=_ERR,
    )

    updated_info = ParentInfo(url=parent_info.url, branch=parent_info.branch, hash=new_hash)
    write_parent_info(repo_dir, updated_info, on_output)
    commit_parent_file(repo_dir, on_output)

    return new_hash
