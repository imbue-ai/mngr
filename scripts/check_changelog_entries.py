"""Enforce that a PR adds one changelog entry per project it touches.

This is the changelog gate. It runs on the *orchestrator* (a
real local checkout, or the GitHub Actions runner) -- the only place a real
base ref exists -- and never inside an offload sandbox. The offload sandbox
does a fresh ``git init`` (so ``main == HEAD``) and never fetches ``origin``,
so a base-branch diff computed there is structurally meaningless: it comes
back empty and the check passes vacuously. See
``scripts/check_changelog_entries_test.py`` for the regression coverage.

The check's input is what this PR changed relative to its base: the files it
touches, and the release headings it adds to a project's ``CHANGELOG.md``,
each of which excuses that project from an entry. That input is computed here,
where the base ref is present, and the gate refuses to run (loud non-zero exit)
rather than pass vacuously when it cannot establish a base distinct from HEAD.

Run it from the repo root as a module so ``scripts`` is importable::

    python -m scripts.check_changelog_entries

Exit codes:
    0  -- ok (every required entry present, or nothing to check: not a PR
          branch / exempt / no project files changed)
    1  -- one or more touched projects are missing their entry file
    2  -- cannot establish a usable diff base (misconfiguration); never a
          silent pass
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

from scripts.changelog_projects import DEV_PROJECT
from scripts.changelog_projects import all_known_projects
from scripts.changelog_projects import project_dir
from scripts.changelog_projects import project_entries_dir
from scripts.changelog_projects import project_for_path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Branch prefixes exempt from the changelog requirement. The consolidation
# agent's own PRs rewrite the consolidated changelogs and would otherwise be
# caught requiring an entry for every project they touch. Dependabot's
# version-bump PRs cannot write an entry.
_EXEMPT_BRANCH_PREFIXES: tuple[str, ...] = ("mngr/changelog-consolidation", "dependabot/")

_ADDED_RELEASE_HEADING_PREFIX: Final[str] = "+## ["
_ADDED_UNRELEASED_HEADING: Final[str] = "+## [Unreleased]"


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

    Tries ``$CHANGELOG_DIFF_BASE_REF`` (an explicit override the CI workflow
    sets from the PR's *live* base branch -- for PRs in a native GitHub stack
    the webhook payload reports the trunk as ``GITHUB_BASE_REF`` even when the
    PR is stacked on another branch), then ``origin/$GITHUB_BASE_REF``,
    ``$GITHUB_BASE_REF``, ``origin/main``, then ``main``, and returns the first
    that resolves to a commit *distinct from HEAD*. A base equal to HEAD yields
    an empty diff and a vacuous pass (the exact bug this gate exists to
    prevent), so such candidates are rejected. Raises ``RuntimeError`` if none
    qualify -- the caller turns that into a loud non-zero exit, never a pass.
    """
    head = _rev_parse("HEAD", repo_root)
    candidates: list[str] = []
    for base_ref in (os.environ.get("CHANGELOG_DIFF_BASE_REF", ""), os.environ.get("GITHUB_BASE_REF", "")):
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


def _diff_against_base(base: str, option: str, paths: list[str], repo_root: Path) -> str:
    """Return the output of ``git diff <option> <base>...HEAD -- <paths>``.

    Raises ``RuntimeError`` if ``git diff`` itself fails.
    """
    result = subprocess.run(
        ["git", "diff", option, f"{base}...HEAD", "--", *paths],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed against {base} (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout


def changed_files_against_base(base: str, repo_root: Path) -> list[str]:
    """Return the repo-relative paths this branch changes vs. ``base``.

    Uses ``git diff --name-only <base>...HEAD`` (three-dot / merge-base form).
    Raises ``RuntimeError`` if ``git diff`` itself fails.
    """
    diff = _diff_against_base(base, "--name-only", [], repo_root)
    return [line for line in diff.splitlines() if line.strip()]


def touched_projects(changed_files: list[str], repo_root: Path) -> set[str]:
    """Return the set of projects this PR touches.

    A project is "touched" iff the PR changes any file under it. Files that are
    themselves changelog artifacts are intentionally *not* excluded -- adding
    an entry file inherently satisfies the requirement, and a PR that only
    edits a project's consolidated ``CHANGELOG.md`` still owes a per-PR entry
    unless it adds a release heading (``projects_stamping_a_release``).
    """
    known = set(all_known_projects(repo_root))
    touched: set[str] = set()
    for rel_path in changed_files:
        project = project_for_path(rel_path, repo_root)
        if project in known:
            touched.add(project)
    return touched


def projects_stamping_a_release(base: str, touched: set[str], repo_root: Path) -> set[str]:
    """Return the ``touched`` projects whose ``CHANGELOG.md`` gains a release heading vs. ``base``.

    A release heading is an added line starting ``## [`` other than
    ``## [Unreleased]``, e.g. ``## [v1.2.3] - 2026-05-11``. Raises
    ``RuntimeError`` if ``git diff`` itself fails.
    """
    stamping: set[str] = set()
    for project in sorted(touched):
        changelog = (project_dir(project, repo_root) / "CHANGELOG.md").relative_to(repo_root)
        diff = _diff_against_base(base, "-U0", [str(changelog)], repo_root)
        if any(
            line.startswith(_ADDED_RELEASE_HEADING_PREFIX) and line != _ADDED_UNRELEASED_HEADING
            for line in diff.splitlines()
        ):
            stamping.add(project)
    return stamping


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
    touched = touched_projects(changed_files, repo_root)
    stamping = projects_stamping_a_release(diff_base, touched, repo_root)
    missing = find_missing_entries(branch, touched - stamping, repo_root)

    if not missing:
        stamping_note = (
            f" {sorted(stamping)} need none: their CHANGELOG.md gains a release section." if stamping else ""
        )
        print(
            f"changelog gate: ok. Branch '{branch}' touches {sorted(touched)} "
            f"(diff base '{diff_base}'); all required entries present.{stamping_note}"
        )
        return 0

    print(
        f"changelog gate FAILED: missing changelog entries for branch '{branch}' "
        f"(diff base '{diff_base}', via git diff --name-only {diff_base}...HEAD). "
        f"This PR touches project(s) {sorted(touched - stamping)}; each needs its own entry file.\n"
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
