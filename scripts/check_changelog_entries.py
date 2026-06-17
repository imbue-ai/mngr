"""Enforce that a PR adds one changelog entry per project it touches.

This is the changelog gate. It runs on the *orchestrator* (a
real local checkout, or the GitHub Actions runner) -- the only place a real
base ref exists -- and never inside an offload sandbox. The offload sandbox
does a fresh ``git init`` (so ``main == HEAD``) and never fetches ``origin``,
so a base-branch diff computed there is structurally meaningless: it comes
back empty and the check passes vacuously. See
``scripts/check_changelog_entries_test.py`` for the regression coverage.

The check has exactly one input: the set of files this PR changed relative to
its base. That input is computed here, where the base ref is present, and the
gate refuses to run (loud non-zero exit) rather than pass vacuously when it
cannot establish a base distinct from HEAD.

Run it from the repo root as a module so ``scripts`` is importable::

    python -m scripts.check_changelog_entries

Exit codes:
    0  -- ok (entries present, or nothing to check: not a PR branch / exempt /
          no project files changed)
    1  -- one or more touched projects are missing their entry file
    2  -- cannot establish a usable diff base (misconfiguration); never a
          silent pass
"""

import os
import subprocess
import sys
from pathlib import Path

from scripts.changelog_projects import DEV_PROJECT
from scripts.changelog_projects import all_known_projects
from scripts.changelog_projects import project_entries_dir
from scripts.changelog_projects import project_for_path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Branch prefixes exempt from the changelog requirement. The consolidation
# agent's own PRs rewrite the consolidated changelogs and would otherwise be
# caught requiring an entry for every project they touch.
_EXEMPT_BRANCH_PREFIXES: tuple[str, ...] = ("mngr/changelog-consolidation",)


def detect_branch(repo_root: Path) -> str | None:
    """Return the PR branch name, or ``None`` if it can't be determined.

    Mirrors ``imbue.imbue_common.test_profiles.detect_branch`` but is inlined
    so this gate stays dependency-free (importable with stdlib only, no
    ``uv sync`` needed in CI): GitHub Actions ``GITHUB_HEAD_REF`` (PR source
    branch) first, then ``GITHUB_REF_NAME`` (push target), then local git.
    """
    for env_var in ("GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
        branch = os.environ.get(env_var, "")
        if branch:
            return branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _rev_parse(ref: str, repo_root: Path) -> str | None:
    """Return the commit SHA ``ref`` resolves to, or ``None`` if it doesn't."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def resolve_diff_base(repo_root: Path) -> str:
    """Return a git ref to diff the PR branch against.

    Tries ``origin/$GITHUB_BASE_REF``, ``$GITHUB_BASE_REF``, ``origin/main``,
    then ``main``, and returns the first that resolves to a commit *distinct
    from HEAD*. A base equal to HEAD yields an empty diff and a vacuous pass
    (the exact bug this gate exists to prevent), so such candidates are
    rejected. Raises ``RuntimeError`` if none qualify -- the caller turns that
    into a loud non-zero exit, never a pass.
    """
    head = _rev_parse("HEAD", repo_root)
    candidates: list[str] = []
    base_ref = os.environ.get("GITHUB_BASE_REF", "")
    if base_ref:
        candidates.extend([f"origin/{base_ref}", base_ref])
    candidates.extend(["origin/main", "main"])

    saw_head_collision = False
    for ref in candidates:
        sha = _rev_parse(ref, repo_root)
        if sha is None:
            continue
        if sha == head:
            saw_head_collision = True
            continue
        return ref

    detail = (
        " The only candidates that resolved point at HEAD itself, which would "
        "produce an empty diff and silently pass. Fetch the real base branch "
        "(e.g. `git fetch origin main`) before running this check."
        if saw_head_collision
        else " Fetch the base branch (e.g. `git fetch origin main`) and re-run."
    )
    raise RuntimeError("Cannot resolve a diff base distinct from HEAD: tried " + ", ".join(candidates) + "." + detail)


def changed_files_against_base(base: str, repo_root: Path) -> list[str]:
    """Return the repo-relative paths this branch changes vs. ``base``.

    Uses ``git diff --name-only <base>...HEAD`` (three-dot / merge-base form).
    Raises ``RuntimeError`` if ``git diff`` itself fails.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed against {base} (exit {result.returncode}): {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def projects_requiring_entry(changed_files: list[str], repo_root: Path) -> set[str]:
    """Return the set of projects this PR must produce a changelog entry for.

    A project is "touched" iff the PR changes any file under it. Files that are
    themselves changelog artifacts are intentionally *not* excluded -- adding
    an entry file inherently satisfies the requirement, and a PR that only
    edits a project's consolidated ``CHANGELOG.md`` still owes a per-PR entry
    describing that edit.
    """
    known = set(all_known_projects(repo_root))
    touched: set[str] = set()
    for rel_path in changed_files:
        project = project_for_path(rel_path, repo_root)
        if project in known:
            touched.add(project)
    return touched


def find_missing_entries(branch: str, touched: set[str], repo_root: Path) -> list[str]:
    """Return the repo-relative entry paths that are missing for ``touched``."""
    sanitized = branch.replace("/", "-")
    missing: list[str] = []
    for project in sorted(touched):
        entry_path = project_entries_dir(project, repo_root) / f"{sanitized}.md"
        if not entry_path.exists():
            missing.append(str(entry_path.relative_to(repo_root)))
    return missing


def is_exempt_branch(branch: str) -> bool:
    """Return whether ``branch`` is exempt from the changelog requirement."""
    return any(branch.startswith(prefix) for prefix in _EXEMPT_BRANCH_PREFIXES)


def main(repo_root: Path = _REPO_ROOT) -> int:
    branch = detect_branch(repo_root)
    if not branch or branch == "main":
        print("changelog gate: not a PR branch (branch is empty or 'main'); nothing to check.")
        return 0
    if is_exempt_branch(branch):
        print(f"changelog gate: branch '{branch}' is exempt from the changelog requirement.")
        return 0

    try:
        diff_base = resolve_diff_base(repo_root)
    except RuntimeError as exc:
        print(f"changelog gate ERROR: {exc}", file=sys.stderr)
        return 2

    changed_files = changed_files_against_base(diff_base, repo_root)
    touched = projects_requiring_entry(changed_files, repo_root)
    missing = find_missing_entries(branch, touched, repo_root)

    if not missing:
        print(
            f"changelog gate: ok. Branch '{branch}' touches {sorted(touched)} "
            f"(diff base '{diff_base}'); all required entries present."
        )
        return 0

    print(
        f"changelog gate FAILED: missing changelog entries for branch '{branch}' "
        f"(diff base '{diff_base}', via git diff --name-only {diff_base}...HEAD). "
        f"This PR touches project(s) {sorted(touched)}; each needs its own entry file.\n"
        f"Create:\n" + "\n".join(f"  - {p}" for p in missing) + "\n"
        f"Each file should briefly describe the user-visible changes in this PR that "
        f"pertain to that project. The synthetic '{DEV_PROJECT}' project covers "
        f"root-level files (scripts/, .github/, top-level docs, build tooling).\n"
        f"\n"
        f"If you believe this PR makes NO actual changes to one of the listed "
        f"projects, do NOT add a placebo entry: a stale or misconfigured diff base "
        f"('{diff_base}') can make unrelated files from main appear changed. The fix "
        f"is to correct the diff base, not to add entries for projects you did not touch.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
