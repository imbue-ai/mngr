"""Per-project changelog layout helpers.

Maps repo-relative paths to the project that owns them, and project names
to the directory holding that project's changelog artifacts:
``<project_dir>/changelog/`` (per-PR entry files),
``<project_dir>/CHANGELOG.md`` (consolidated summary),
``<project_dir>/UNABRIDGED_CHANGELOG.md`` (consolidated verbatim).

Shared between the consolidator, the release script, the per-PR ratchet,
and the consolidation prompt so they all agree on what a "project" is.

A "project" is a directory under ``libs/`` or ``apps/`` containing a
``pyproject.toml``, or the synthetic top-level ``dev`` bucket that owns
root-level files (scripts, CI workflows, top-level docs, build tooling).
"""

from pathlib import Path
from typing import Final

DEV_PROJECT: Final[str] = "dev"


def project_for_path(rel_path: Path | str, repo_root: Path) -> str:
    """Return the project that owns ``rel_path`` (a repo-relative path).

    A ``libs/<name>/...`` or ``apps/<name>/...`` path resolves to ``<name>``
    when that directory contains a ``pyproject.toml``; everything else
    falls back to ``dev``. The ``pyproject.toml`` check guards against a
    path like ``libs/garbage/...`` (not an actual project) being treated
    as a real project.
    """
    parts = Path(rel_path).parts
    if len(parts) >= 2 and parts[0] in ("libs", "apps"):
        candidate = repo_root / parts[0] / parts[1]
        if (candidate / "pyproject.toml").exists():
            return parts[1]
    return DEV_PROJECT


def project_dir(project: str, repo_root: Path) -> Path:
    """Return the directory that holds ``project``'s changelog artifacts."""
    if project == DEV_PROJECT:
        return repo_root / DEV_PROJECT
    libs = repo_root / "libs" / project
    if libs.is_dir():
        return libs
    apps = repo_root / "apps" / project
    if apps.is_dir():
        return apps
    raise ValueError(f"Unknown project: {project!r}")


def project_entries_dir(project: str, repo_root: Path) -> Path:
    """Return the ``<project_dir>/changelog/`` directory for per-PR entry files."""
    return project_dir(project, repo_root) / "changelog"


def pyproject_projects(repo_root: Path) -> list[str]:
    """Return every ``libs/<name>`` and ``apps/<name>`` with a ``pyproject.toml``,
    sorted alphabetically. Excludes the synthetic ``dev`` bucket.

    Shared discovery helper for callers that want only the "real" projects
    (e.g. the meta-ratchets that look for per-project ``test_ratchets.py``,
    coverage configuration, PyPI readme, etc., none of which apply to
    ``dev/``).
    """
    names: list[str] = []
    for parent_name in ("libs", "apps"):
        parent = repo_root / parent_name
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and (child / "pyproject.toml").exists():
                names.append(child.name)
    names.sort()
    return names


def all_known_projects(repo_root: Path) -> list[str]:
    """Return every known project name, including the synthetic ``dev``.

    Composed of ``pyproject_projects(repo_root)`` plus ``DEV_PROJECT`` appended
    last, so changelog-aware callers iterate libs/apps in alphabetical order
    and then ``dev``.
    """
    return [*pyproject_projects(repo_root), DEV_PROJECT]
