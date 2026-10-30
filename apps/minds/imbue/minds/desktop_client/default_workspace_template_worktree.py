"""Materialize a paired default-workspace-template working tree for tests.

Workspace-creation tests build their Docker workspace from a DEFAULT_WORKSPACE_TEMPLATE
working tree. To let an Imbue Studio change be tested against its paired DEFAULT_WORKSPACE_TEMPLATE
change, this module clones the *paired* DEFAULT_WORKSPACE_TEMPLATE branch (the default-workspace-template-remote
branch whose name matches the current mngr branch, else DEFAULT_WORKSPACE_TEMPLATE ``main``). The
workspace container runs the mngr that tree pins.

The materialize step runs where git works -- the CI runner (before the snapshot
image is staged) or a local machine -- never inside the crippled snapshot
sandbox (whose uploaded ``.git`` is non-functional). The tree is then baked into
the snapshot image / left at ``.external_worktrees/default-workspace-template`` so
the create flow's ``resolve_default_workspace_template_path`` finds it via its worktree short-circuit.

Kept deliberately free of heavy imports (no playwright) so the snapshot bake
script can import it on the runner without pulling in the Electron toolchain.
"""

import os
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# This file lives at apps/minds/imbue/minds/desktop_client/default_workspace_template_worktree.py, so
# parents[5] hops up over desktop_client, minds, imbue, minds, apps to the repo
# root (same computation as e2e_workspace_runner.py in this directory).
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]
DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE: Final[Path] = (
    _REPO_ROOT / ".external_worktrees" / "default-workspace-template"
)
_DEFAULT_WORKSPACE_TEMPLATE_REMOTE: Final[str] = "https://github.com/imbue-ai/default-workspace-template.git"
_DEFAULT_WORKSPACE_TEMPLATE_FALLBACK_BRANCH: Final[str] = "main"
_DEFAULT_WORKSPACE_TEMPLATE_NAME: Final[str] = "default-workspace-template"


class MaterializedTemplateWorktree(FrozenModel):
    """A materialized DEFAULT_WORKSPACE_TEMPLATE working tree, and which tree it came from.

    ``ref`` and ``commit`` are the run's template *coordinate*: which tree built the
    workspace, which the paired branch's name alone does not say.
    """

    path: Path = Field(description="Where the working tree was materialized")
    ref: str = Field(
        description="DEFAULT_WORKSPACE_TEMPLATE branch the tree came from, or its commit when the tree is detached"
    )
    commit: str = Field(description="Commit the tree was taken from, before the pytest opt-in commit added on top")


@pure
def template_configuration_identity(ref: str) -> str:
    """The identity a run reports for the DEFAULT_WORKSPACE_TEMPLATE tree it built from.

    One comparable string, because what the flake reconciler needs is to group a
    cluster's occurrences by the test input they carried: two runs are independent
    reproductions of each other only when this matches.
    """
    return f"{_DEFAULT_WORKSPACE_TEMPLATE_NAME}@{ref}"


def _current_mngr_branch() -> str | None:
    """Return the current branch name of the mngr repo, or None if unknown.

    GitHub Actions exposes the real branch via the environment even when the
    checkout is a detached HEAD: ``GITHUB_HEAD_REF`` is the PR source branch
    (set only for pull_request events); ``GITHUB_REF_NAME`` is the branch for
    push events (but a ``<n>/merge`` ref for PRs, which we ignore). Consult those
    first, then fall back to ``git rev-parse``. Any failure to determine a real
    branch returns None, which routes the caller to DEFAULT_WORKSPACE_TEMPLATE ``main``.
    """
    ci_head_ref = os.environ.get("GITHUB_HEAD_REF")
    if ci_head_ref:
        return ci_head_ref
    ci_ref_name = os.environ.get("GITHUB_REF_NAME")
    if ci_ref_name and not ci_ref_name.endswith("/merge"):
        return ci_ref_name
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Could not determine current mngr branch ({!r}); treating as unknown", exc)
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _default_workspace_template_remote_has_branch(branch: str) -> bool:
    """Return True iff the DEFAULT_WORKSPACE_TEMPLATE public remote currently has ``branch``.

    ``git ls-remote`` exits 0 either way; presence is signalled by non-empty
    stdout. Network-level failures are logged and treated as "no such branch"
    so the caller falls back to ``main`` rather than crashing on a transient
    probe failure.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", _DEFAULT_WORKSPACE_TEMPLATE_REMOTE, branch],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "Failed to query DEFAULT_WORKSPACE_TEMPLATE remote for branch {!r}; treating as absent so main fallback runs: {!r}",
            branch,
            exc,
        )
        return False
    return bool(result.stdout.strip())


def _read_materialized_worktree(worktree: Path, ref: str | None) -> MaterializedTemplateWorktree:
    """Describe an existing template tree by its checked-out branch, else by its commit."""
    commit = _read_git(worktree, ["rev-parse", "HEAD"])
    return MaterializedTemplateWorktree(path=worktree, ref=ref if ref is not None else commit, commit=commit)


def current_worktree_branch(worktree: Path) -> str | None:
    """Return the checked-out branch name of ``worktree``, or None if unknown."""
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _run_git(repo: Path, args: list[str]) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=120)


def _read_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=120)
    return result.stdout.strip()


def write_pytest_config_opt_in(settings_path: Path) -> None:
    """Prepend ``is_allowed_in_pytest = true`` to a throwaway DEFAULT_WORKSPACE_TEMPLATE ``settings.toml``.

    mngr's config guard refuses to load a project config under
    ``PYTEST_CURRENT_TEST`` unless it opts in. The materialized tree is
    throwaway, so writing the top-level key in place is safe. Prepended (a
    top-level key must precede any ``[table]``); only ever called on a fresh
    clone whose DEFAULT_WORKSPACE_TEMPLATE settings.toml has no such key, so it never duplicates one.
    """
    existing = settings_path.read_text() if settings_path.exists() else ""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(f"is_allowed_in_pytest = true\n\n{existing}")


def materialize_paired_default_workspace_template_worktree(
    destination: Path = DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE,
    *,
    mngr_branch: str | None = None,
) -> MaterializedTemplateWorktree:
    """Materialize a paired-branch DEFAULT_WORKSPACE_TEMPLATE working tree at ``destination`` if absent.

    Clones the paired DEFAULT_WORKSPACE_TEMPLATE branch (``mngr_branch`` or :func:`_current_mngr_branch`
    if it exists on the DEFAULT_WORKSPACE_TEMPLATE remote, else ``main``), writes the pytest config
    opt-in, and commits it so the create flow's ``git checkout -B <branch> FETCH_HEAD`` transfers
    it into the workspace container cleanly (FETCH_HEAD is aligned to the commit so that checkout
    is a content-preserving no-op). The workspace runs the mngr the template pins.

    An existing ``destination`` is left untouched -- an operator's ``minds-start``
    worktree is never clobbered, and re-runs are idempotent. Runs only where git
    works; genuine failures (clone / commit) propagate loudly rather than silently
    falling back to the released DEFAULT_WORKSPACE_TEMPLATE tag.

    Returns the tree's coordinate along with its path, so the caller can report which
    template tree built the workspace. A tree that was already present is read back
    rather than assumed, since nothing here chose it.
    """
    if destination.exists():
        logger.info("DEFAULT_WORKSPACE_TEMPLATE worktree already present at {}; leaving it untouched", destination)
        return _read_materialized_worktree(destination, ref=current_worktree_branch(destination))

    mngr_branch = mngr_branch if mngr_branch is not None else _current_mngr_branch()
    if mngr_branch is not None and _default_workspace_template_remote_has_branch(mngr_branch):
        default_workspace_template_ref = mngr_branch
    else:
        default_workspace_template_ref = _DEFAULT_WORKSPACE_TEMPLATE_FALLBACK_BRANCH
    logger.info(
        "Materializing DEFAULT_WORKSPACE_TEMPLATE worktree at {} from DEFAULT_WORKSPACE_TEMPLATE branch {!r} (paired mngr branch {!r})",
        destination,
        default_workspace_template_ref,
        mngr_branch,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Disable auto-gc / auto-maintenance in the clone (``--config`` persists into the
    # new repo). The later ``add -A`` + ``commit`` + ``fetch`` steps would otherwise
    # trip git's automatic maintenance, which runs detached in the background
    # (``gc.autoDetach`` defaults to true) and rewrites ``.git`` (e.g. ``update-server-info``
    # regenerates ``.git/info/refs``). The snapshot build uploads this worktree with its
    # live ``.git`` via Modal's ``add_local_dir``, so a background rewrite mid-upload aborts
    # the build with "was modified during build process". This tree is a throwaway baked
    # into a one-shot e2e image, so it never needs gc/packing.
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--config",
            "gc.auto=0",
            "--config",
            "maintenance.auto=false",
            "--branch",
            default_workspace_template_ref,
            _DEFAULT_WORKSPACE_TEMPLATE_REMOTE,
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    # Read before the opt-in commit below moves HEAD off the cloned commit. ``--depth 1``
    # from a branch gives no other record of which commit that branch pointed at.
    cloned_commit = _read_git(destination, ["rev-parse", "HEAD"])
    write_pytest_config_opt_in(destination / ".mngr" / "settings.toml")
    _run_git(destination, ["add", "-A"])
    _run_git(
        destination,
        [
            "-c",
            "user.email=minds-e2e@imbue.com",
            "-c",
            "user.name=minds-e2e",
            "commit",
            "-q",
            "-m",
            "test: pytest opt-in",
        ],
    )
    # Align FETCH_HEAD to the commit so the create flow's ``checkout -B <ref>
    # FETCH_HEAD`` (run in this clone) is a no-op that keeps the opt-in.
    # Fetching from ``.`` is local-only.
    _run_git(destination, ["fetch", "--no-tags", ".", "HEAD"])
    return MaterializedTemplateWorktree(path=destination, ref=default_workspace_template_ref, commit=cloned_commit)
