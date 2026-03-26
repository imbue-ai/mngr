"""Vendor repositories into a mind's vendor directory as git subtrees.

Reads ``[[vendor]]`` entries from minds.toml (via ``ClaudeMindSettings``) and
adds each repository as a git subtree under ``vendor/<name>/``.

When no vendor configuration is present, falls back to vendoring the mng
repository from the public GitHub URL.

The ``MINDS_VENDOR_PATH`` environment variable can override vendor sources
for development.  Format: ``name@/path/to/repo:other@/another/path``.
Each ``name@/path`` pair overrides (or adds) a vendor entry to use a local
path instead of whatever was configured.

Local/editable repos must be "clean" (no uncommitted changes, no untracked
files) before they can be vendored.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.errors import DirtyRepoError
from imbue.minds.errors import GitOperationError
from imbue.minds.errors import VendorError
from imbue.mng_claude_mind.data_types import VendorRepoConfig

MNG_GITHUB_URL: Final[str] = "https://github.com/imbue-ai/mng.git"

VENDOR_DIR_NAME: Final[str] = "vendor"

VENDOR_MNG_DIR_NAME: Final[NonEmptyStr] = NonEmptyStr("mng")

VENDOR_PATH_ENV_VAR: Final[str] = "MINDS_VENDOR_PATH"


def parse_vendor_path_env(raw: str) -> dict[str, Path]:
    """Parse the MINDS_VENDOR_PATH env var into a name-to-path mapping.

    Format: ``name@/path/to/repo:other_name@/another/path``

    Raises VendorError if an entry is malformed (missing ``@`` separator
    or empty name/path).
    """
    result: dict[str, Path] = {}
    for entry in raw.split(":"):
        entry = entry.strip()
        if not entry:
            continue
        if "@" not in entry:
            raise VendorError(
                "Malformed {} entry '{}': expected 'name@/path' format".format(VENDOR_PATH_ENV_VAR, entry)
            )
        name, path_str = entry.split("@", 1)
        name = name.strip()
        path_str = path_str.strip()
        if not name or not path_str:
            raise VendorError(
                "Malformed {} entry '{}': name and path must both be non-empty".format(VENDOR_PATH_ENV_VAR, entry)
            )
        result[name] = Path(path_str)
    return result


def apply_vendor_overrides(
    configs: tuple[VendorRepoConfig, ...],
) -> tuple[VendorRepoConfig, ...]:
    """Apply MINDS_VENDOR_PATH overrides to a set of vendor configs.

    For each override in the env var:
    - If a config with that name already exists, replace its source with the local path.
    - If no config with that name exists, append a new entry.

    Returns the original configs unchanged when MINDS_VENDOR_PATH is not set.
    """
    raw = os.environ.get(VENDOR_PATH_ENV_VAR)
    if not raw:
        return configs

    overrides = parse_vendor_path_env(raw)
    if not overrides:
        return configs

    existing_names = {c.name for c in configs}

    replaced: list[VendorRepoConfig] = []
    for config in configs:
        if config.name in overrides:
            replaced.append(
                VendorRepoConfig(
                    name=config.name,
                    path=str(overrides[config.name]),
                )
            )
        else:
            replaced.append(config)

    for name, path in overrides.items():
        if name not in existing_names:
            replaced.append(
                VendorRepoConfig(
                    name=NonEmptyStr(name),
                    path=str(path),
                )
            )

    return tuple(replaced)


def default_vendor_configs() -> tuple[VendorRepoConfig, ...]:
    """Build the default vendor config (mng repo from GitHub) when none is configured."""
    return (
        VendorRepoConfig(
            name=VENDOR_MNG_DIR_NAME,
            url=MNG_GITHUB_URL,
        ),
    )


_VENDOR_GIT_USER_NAME: Final[str] = "minds"
_VENDOR_GIT_USER_EMAIL: Final[str] = "minds@localhost"


def ensure_git_identity(repo_dir: Path) -> None:
    """Ensure git user.name and user.email are configured in the repo.

    ``git subtree add`` creates merge commits, which require a committer
    identity.  When running in environments without a global git config
    (e.g. CI containers), this sets a repo-local identity so the subtree
    operation can succeed.
    """
    cg = ConcurrencyGroup(name="vendor-git-identity")
    with cg:
        name_result = cg.run_process_to_completion(
            command=["git", "config", "user.name"],
            cwd=repo_dir,
            is_checked_after=False,
        )
    if name_result.returncode != 0:
        run_git(
            ["config", "user.name", _VENDOR_GIT_USER_NAME],
            cwd=repo_dir,
            error_message="Failed to set git user.name",
        )
        run_git(
            ["config", "user.email", _VENDOR_GIT_USER_EMAIL],
            cwd=repo_dir,
            error_message="Failed to set git user.email",
        )


def vendor_repos(
    mind_dir: Path,
    configs: tuple[VendorRepoConfig, ...],
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Add each configured repository as a git subtree under vendor/.

    Skips any repo whose ``vendor/<name>`` directory already exists.
    Raises DirtyRepoError if a local repo has uncommitted or untracked changes.
    Raises VendorError if any git operation fails.
    """
    ensure_git_identity(mind_dir)
    for config in configs:
        vendor_subdir = mind_dir / VENDOR_DIR_NAME / config.name
        if vendor_subdir.exists():
            logger.debug("vendor/{} already exists, skipping", config.name)
            continue

        if config.is_local:
            repo_path = _resolve_local_path(config.path)
            check_repo_is_clean(repo_path)
            ref = _resolve_ref_local(repo_path, config.ref)
            logger.debug("Vendoring {} from local repo {} at {}", config.name, repo_path, ref)
            _add_subtree(mind_dir, config.name, str(repo_path), ref, on_output)
        else:
            url = _require_url(config.url)
            ref = _resolve_ref_remote(url, config.ref, on_output)
            logger.debug("Vendoring {} from {} at {}", config.name, url, ref)
            _add_subtree(mind_dir, config.name, url, ref, on_output)


def check_repo_is_clean(repo_path: Path) -> None:
    """Verify that a local repository has no uncommitted changes or untracked files.

    Raises DirtyRepoError if the working tree is not clean.
    """
    status_output = run_git(
        ["status", "--porcelain"],
        cwd=repo_path,
        error_message="Failed to check git status of {}".format(repo_path),
    )
    if status_output.strip():
        dirty_summary = status_output.strip()[:500]
        raise DirtyRepoError(
            "Local repo {} has uncommitted changes or untracked files and cannot be vendored:\n{}".format(
                repo_path, dirty_summary
            )
        )


def _resolve_local_path(path_str: str | None) -> Path:
    """Resolve a local repo path to an absolute path."""
    if path_str is None:
        raise VendorError("local vendor repo has no path")
    resolved = Path(path_str).expanduser().resolve()
    if not resolved.is_dir():
        raise VendorError("local vendor repo path does not exist: {}".format(resolved))
    return resolved


def _resolve_ref_local(repo_path: Path, ref: str | None) -> str:
    """Resolve the git ref for a local repo, defaulting to HEAD."""
    if ref is not None:
        return ref
    return run_git(
        ["rev-parse", "HEAD"],
        cwd=repo_path,
        error_message="Failed to resolve HEAD of {}".format(repo_path),
    ).strip()


def _require_url(url: str | None) -> str:
    """Narrow a url that should be non-None (validated by VendorRepoConfig)."""
    if url is None:
        raise VendorError("remote vendor repo has no url")
    return url


def _resolve_ref_remote(
    url: str,
    ref: str | None,
    on_output: Callable[[str, bool], None] | None = None,
) -> str:
    """Resolve the git ref for a remote repo, defaulting to HEAD."""
    if ref is not None:
        return ref
    ls_output = run_git(
        ["ls-remote", url, "HEAD"],
        cwd=Path.cwd(),
        on_output=on_output,
        error_message="Failed to resolve HEAD of {}".format(url),
    )
    parts = ls_output.strip().split()
    if not parts:
        raise VendorError("git ls-remote returned no output for {}".format(url))
    return parts[0]


def _add_subtree(
    mind_dir: Path,
    name: str,
    url_or_path: str,
    ref: str,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Run ``git subtree add`` to add a repository under vendor/<name>/."""
    prefix = "{}/{}".format(VENDOR_DIR_NAME, name)
    run_git(
        ["subtree", "add", "--prefix", prefix, url_or_path, ref, "--squash"],
        cwd=mind_dir,
        on_output=on_output,
        error_message="Failed to add git subtree for {}".format(name),
    )


def pull_subtree(
    mind_dir: Path,
    name: str,
    url_or_path: str,
    ref: str,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Run ``git subtree pull`` to update a repository under vendor/<name>/.

    Merges the latest changes from the source repository into the existing
    subtree. The subtree must already exist (i.e. was previously added via
    ``vendor_repos``). Raises VendorError if the pull fails.
    """
    prefix = "{}/{}".format(VENDOR_DIR_NAME, name)
    run_git(
        ["subtree", "pull", "--prefix", prefix, url_or_path, ref, "--squash"],
        cwd=mind_dir,
        on_output=on_output,
        error_message="Failed to pull git subtree for {}".format(name),
    )


def update_vendor_repos(
    mind_dir: Path,
    configs: tuple[VendorRepoConfig, ...],
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Pull the latest changes for each vendored git subtree.

    Only updates subtrees whose ``vendor/<name>`` directory already exists.
    Subtrees that were never added are skipped (use ``vendor_repos`` first).

    Raises DirtyRepoError if a local repo has uncommitted or untracked changes.
    Raises VendorError if any git operation fails.
    """
    ensure_git_identity(mind_dir)
    for config in configs:
        vendor_subdir = mind_dir / VENDOR_DIR_NAME / config.name
        if not vendor_subdir.exists():
            logger.debug("vendor/{} does not exist, skipping update", config.name)
            continue

        if config.is_local:
            repo_path = _resolve_local_path(config.path)
            check_repo_is_clean(repo_path)
            ref = _resolve_ref_local(repo_path, config.ref)
            logger.debug("Updating vendor/{} from local repo {} at {}", config.name, repo_path, ref)
            pull_subtree(mind_dir, config.name, str(repo_path), ref, on_output)
        else:
            url = _require_url(config.url)
            ref = _resolve_ref_remote(url, config.ref, on_output)
            logger.debug("Updating vendor/{} from {} at {}", config.name, url, ref)
            pull_subtree(mind_dir, config.name, url, ref, on_output)


def run_git(
    args: list[str],
    cwd: Path,
    on_output: Callable[[str, bool], None] | None = None,
    error_message: str = "git command failed",
    error_class: type[GitOperationError] = VendorError,
) -> str:
    """Run a git command and return stdout.

    Raises the specified error class (default: VendorError) if the command
    exits with a non-zero status. Callers can pass a different error class
    for semantically appropriate error types.
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
        raise error_class(
            "{} (exit code {}):\n{}".format(
                error_message,
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )
    return result.stdout
