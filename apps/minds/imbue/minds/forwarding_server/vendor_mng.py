"""Vendor the mng repository into a mind's vendor directory.

Detects whether we are running from within the mng monorepo (development mode)
or from an installed package (production mode), and uses the appropriate
strategy to inject a copy of mng into the vendor/mng/ directory of a mind's
repo.

Development mode: clones from the local mng repo, applies any uncommitted
changes (including untracked non-gitignored files), and resets the git remote
to point at GitHub.

Production mode: clones directly from GitHub.
"""

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.errors import VendorError

MNG_GITHUB_URL: Final[str] = "https://github.com/imbue-ai/mng.git"

VENDOR_DIR_NAME: Final[str] = "vendor"

VENDOR_MNG_DIR_NAME: Final[str] = "mng"


def find_mng_repo_root() -> Path | None:
    """Find the mng monorepo root by walking up from this module's source file.

    Returns the repo root if this module is part of an mng checkout
    (regular repo or worktree), or None if running from an installed package.
    """
    current = Path(__file__).resolve().parent
    while current != current.parent:
        git_marker = current / ".git"
        if git_marker.exists():
            if (current / "libs" / "mng").is_dir():
                return current
            return None
        current = current.parent
    return None


def vendor_mng(
    mind_dir: Path,
    mng_repo_root: Path | None,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Inject a copy of the mng repository into mind_dir/vendor/mng/.

    When mng_repo_root is provided (development mode), clones from the local
    repo, applies any uncommitted changes, and resets the remote to GitHub.
    When mng_repo_root is None (production mode), clones directly from GitHub.
    """
    vendor_mng_dir = mind_dir / VENDOR_DIR_NAME / VENDOR_MNG_DIR_NAME
    if vendor_mng_dir.exists():
        logger.debug("vendor/mng already exists at {}, skipping", vendor_mng_dir)
        return

    vendor_mng_dir.parent.mkdir(parents=True, exist_ok=True)

    if mng_repo_root is not None:
        logger.debug("Vendoring mng from local repo at {}", mng_repo_root)
        _vendor_from_local_repo(mng_repo_root, vendor_mng_dir, on_output)
    else:
        logger.debug("Vendoring mng from GitHub")
        _run_git(
            ["clone", MNG_GITHUB_URL, str(vendor_mng_dir)],
            cwd=vendor_mng_dir.parent,
            on_output=on_output,
            error_message="Failed to clone mng from GitHub",
        )


def _run_git(
    args: list[str],
    cwd: Path,
    on_output: Callable[[str, bool], None] | None = None,
    error_message: str = "git command failed",
) -> str:
    """Run a git command and return stdout.

    Raises VendorError if the command exits with a non-zero status.
    """
    cg = ConcurrencyGroup(name="vendor-git")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", *args],
            cwd=cwd,
            is_checked_after=False,
            on_output=on_output,
        )
    if result.returncode != 0:
        raise VendorError(
            "{} (exit code {}):\n{}".format(
                error_message,
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )
    return result.stdout


def _vendor_from_local_repo(
    mng_root: Path,
    vendor_mng_dir: Path,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Clone from the local mng repo, apply uncommitted changes, and fix the remote."""
    _run_git(
        ["clone", str(mng_root), str(vendor_mng_dir)],
        cwd=mng_root,
        on_output=on_output,
        error_message="Failed to clone local mng repo",
    )

    # Apply uncommitted changes (staged + unstaged modifications to tracked files)
    diff_output = _run_git(
        ["diff", "HEAD", "--binary"],
        cwd=mng_root,
        error_message="Failed to get uncommitted changes",
    )
    if diff_output.strip():
        with tempfile.TemporaryDirectory() as tmpdir:
            diff_file = Path(tmpdir) / "mng.patch"
            diff_file.write_text(diff_output)
            _run_git(
                ["apply", str(diff_file)],
                cwd=vendor_mng_dir,
                on_output=on_output,
                error_message="Failed to apply uncommitted changes",
            )

    # Copy untracked non-gitignored files
    untracked_output = _run_git(
        ["ls-files", "--others", "--exclude-standard"],
        cwd=mng_root,
        error_message="Failed to list untracked files",
    )
    for rel_path_str in untracked_output.strip().splitlines():
        if not rel_path_str:
            continue
        src = mng_root / rel_path_str
        dst = vendor_mng_dir / rel_path_str
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # Reset git remote to point at GitHub
    _run_git(
        ["remote", "set-url", "origin", MNG_GITHUB_URL],
        cwd=vendor_mng_dir,
        error_message="Failed to reset git remote to GitHub",
    )
