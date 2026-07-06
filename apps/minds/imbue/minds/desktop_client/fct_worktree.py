"""Materialize a paired forever-claude-template (FCT) working tree for tests.

Workspace-creation tests (the minds snapshot bake + resume, the create+chat
acceptance test, the full-flow harness) build their Docker workspace from an FCT
working tree. To let a coordinated mngr+FCT change be tested together, this
module reproduces the ``just minds-start`` debug state ahead of time: it clones
the *paired* FCT branch (the FCT-remote branch whose name matches the current
mngr branch, else FCT ``main``) and vendors this mngr checkout's HEAD into the
tree's ``vendor/mngr`` so the workspace container runs the mngr code under test.

The materialize step runs where git works -- the CI runner (before the snapshot
image is staged) or a local machine -- never inside the crippled snapshot
sandbox (whose uploaded ``.git`` is non-functional). The tree is then baked into
the snapshot image / left at ``.external_worktrees/forever-claude-template`` so
the create flow's ``resolve_fct_path`` finds it via its worktree short-circuit.

Kept deliberately free of heavy imports (no playwright) so the snapshot bake
script can import it on the runner without pulling in the Electron toolchain.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger

# This file lives at apps/minds/imbue/minds/desktop_client/fct_worktree.py, so
# parents[5] hops up over desktop_client, minds, imbue, minds, apps to the repo
# root (same computation as e2e_workspace_runner.py in this directory).
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]
FCT_EXTERNAL_WORKTREE: Final[Path] = _REPO_ROOT / ".external_worktrees" / "forever-claude-template"
_FCT_REMOTE: Final[str] = "https://github.com/imbue-ai/forever-claude-template.git"
_FCT_FALLBACK_BRANCH: Final[str] = "main"


class FctWorktreeError(RuntimeError):
    """Raised when the paired FCT worktree cannot be materialized."""


def _current_mngr_branch() -> str | None:
    """Return the current branch name of the mngr repo, or None if unknown.

    GitHub Actions exposes the real branch via the environment even when the
    checkout is a detached HEAD: ``GITHUB_HEAD_REF`` is the PR source branch
    (set only for pull_request events); ``GITHUB_REF_NAME`` is the branch for
    push events (but a ``<n>/merge`` ref for PRs, which we ignore). Consult those
    first, then fall back to ``git rev-parse``. Any failure to determine a real
    branch returns None, which routes the caller to FCT ``main``.
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


def _fct_remote_has_branch(branch: str) -> bool:
    """Return True iff the FCT public remote currently has ``branch``.

    ``git ls-remote`` exits 0 either way; presence is signalled by non-empty
    stdout. Network-level failures are logged and treated as "no such branch"
    so the caller falls back to ``main`` rather than crashing on a transient
    probe failure.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", _FCT_REMOTE, branch],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "Failed to query FCT remote for branch {!r}; treating as absent so main fallback runs: {!r}",
            branch,
            exc,
        )
        return False
    return bool(result.stdout.strip())


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


def _write_pytest_config_opt_in(settings_path: Path) -> None:
    """Prepend ``is_allowed_in_pytest = true`` to a throwaway FCT ``settings.toml``.

    mngr's config guard refuses to load a project config under
    ``PYTEST_CURRENT_TEST`` unless it opts in. The materialized tree is
    throwaway, so writing the top-level key in place is safe. Prepended (a
    top-level key must precede any ``[table]``); only ever called on a fresh
    clone whose FCT settings.toml has no such key, so it never duplicates one.
    """
    existing = settings_path.read_text() if settings_path.exists() else ""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(f"is_allowed_in_pytest = true\n\n{existing}")


def _vendor_mngr_into_fct(fct_dir: Path) -> None:
    """Replace ``fct_dir/vendor/mngr`` with an archive of this mngr checkout's HEAD.

    Mirrors ``just sync-vendor-mngr``: ``git archive HEAD`` of the mngr repo into
    ``vendor/mngr`` so the workspace container runs the mngr under test rather
    than whatever mngr the FCT ref vendored. Requires the mngr checkout's git to
    work, so it runs only on the runner / a local machine, never in the sandbox.
    """
    vendor = fct_dir / "vendor" / "mngr"
    if not vendor.parent.is_dir():
        raise FctWorktreeError(f"FCT clone at {fct_dir} has no vendor/ directory to sync mngr into")
    archive = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "archive", "--format=tar", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        timeout=180,
    )
    if vendor.exists():
        shutil.rmtree(vendor)
    vendor.mkdir(parents=True)
    subprocess.run(["tar", "-x", "-C", str(vendor)], input=archive.stdout, check=True, timeout=180)


def materialize_paired_fct_worktree(
    destination: Path = FCT_EXTERNAL_WORKTREE,
    *,
    mngr_branch: str | None = None,
) -> Path:
    """Materialize a paired-branch FCT working tree at ``destination`` if absent.

    Clones the paired FCT branch (``mngr_branch`` or :func:`_current_mngr_branch`
    if it exists on the FCT remote, else ``main``), vendors this mngr checkout's
    HEAD into ``vendor/mngr``, writes the pytest config opt-in, and commits both
    so the create flow's ``git checkout -B <branch> FETCH_HEAD`` transfers them
    into the workspace container cleanly (FETCH_HEAD is aligned to the commit so
    that checkout is a content-preserving no-op).

    An existing ``destination`` is left untouched -- an operator's ``minds-start``
    worktree is never clobbered, and re-runs are idempotent. Runs only where git
    works; genuine failures (clone / vendor / commit) propagate loudly rather
    than silently falling back to the released FCT tag.
    """
    if destination.exists():
        logger.info("FCT worktree already present at {}; leaving it untouched", destination)
        return destination

    mngr_branch = mngr_branch if mngr_branch is not None else _current_mngr_branch()
    if mngr_branch is not None and _fct_remote_has_branch(mngr_branch):
        fct_ref = mngr_branch
    else:
        fct_ref = _FCT_FALLBACK_BRANCH
    logger.info(
        "Materializing FCT worktree at {} from FCT branch {!r} (paired mngr branch {!r})",
        destination,
        fct_ref,
        mngr_branch,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", fct_ref, _FCT_REMOTE, str(destination)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    _vendor_mngr_into_fct(destination)
    _write_pytest_config_opt_in(destination / ".mngr" / "settings.toml")
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
            "test: vendor mngr HEAD + pytest opt-in",
        ],
    )
    # Align FETCH_HEAD to the commit so the create flow's ``checkout -B <ref>
    # FETCH_HEAD`` (run in this clone) is a no-op that keeps the vendored mngr
    # and opt-in. Fetching from ``.`` is local-only.
    _run_git(destination, ["fetch", "--no-tags", ".", "HEAD"])
    return destination
