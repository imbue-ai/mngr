"""Consolidate per-project changelog entry files into each project's
``UNABRIDGED_CHANGELOG.md``.

Reads all ``.md`` files under ``<project_dir>/changelog/`` (excluding
``.gitkeep``) for every known project (``libs/<name>``, ``apps/<name>``,
or ``dev``), groups them by the date the entry's PR landed on the
current branch (committer date of the introducing commit on the
first-parent line, in America/Los_Angeles), and prepends one
date-headed section per distinct date to that project's
``<project_dir>/UNABRIDGED_CHANGELOG.md`` (newest first). Deletes the
individual entry files once routed.

Exits with code 0 and no changes if there are no changelog entries to consolidate.
"""

import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from changelog_projects import all_known_projects
from changelog_projects import project_dir
from changelog_projects import project_entries_dir

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACIFIC = ZoneInfo("America/Los_Angeles")


def _collect_project_entries(entries_dir: Path) -> list[tuple[Path, str]]:
    """Collect all entry files in ``entries_dir`` (a ``<project_dir>/changelog/``).

    Returns a list of (path, content) tuples sorted by filename, excluding
    ``.gitkeep``, non-``.md`` files, and empty-content entries.
    """
    entries: list[tuple[Path, str]] = []
    if not entries_dir.is_dir():
        return entries
    for path in sorted(entries_dir.iterdir()):
        if path.name == ".gitkeep" or not path.name.endswith(".md") or not path.is_file():
            continue
        content = path.read_text().strip()
        if content:
            entries.append((path, content))
    return entries


def pending_changelog_entries(repo_root: Path) -> list[Path]:
    """Return all changelog entry files awaiting consolidation, across every project.

    Walks each known project's ``<project_dir>/changelog/`` directory so
    ``release.py`` can ask "is there work to do?" without duplicating the
    filter rule.
    """
    pending: list[Path] = []
    for project in all_known_projects(repo_root):
        pending.extend(path for path, _content in _collect_project_entries(project_entries_dir(project, repo_root)))
    return pending


def _get_entry_added_datetime(path: Path, repo_root: Path) -> datetime:
    """Return when the entry's PR landed on the current branch, as PT.

    Uses ``git log --first-parent --diff-filter=A --format=%cI -- <path>``
    to find the committer date of the commit that introduced the file on
    the current branch's first-parent line. For files merged in via a PR
    merge commit, that's the merge commit's committer date -- i.e. when
    the PR landed -- not the feature-branch author date. If the file has
    been added more than once on the first-parent line, takes the most
    recent.

    Raises ``RuntimeError`` if ``git log`` fails or if no commit on the
    first-parent line introduces the file.
    """
    rel = path.relative_to(repo_root)
    result = subprocess.run(
        ["git", "log", "--first-parent", "--diff-filter=A", "--format=%cI", "--", str(rel)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git log failed for {rel} (exit {result.returncode}): {result.stderr.strip()}")
    iso_lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not iso_lines:
        raise RuntimeError(f"no commit found that adds {rel} on the first-parent line")
    # `git log` prints newest-first, so iso_lines[0] is the most recent
    # add. fromisoformat parses the offset, giving an aware datetime.
    return datetime.fromisoformat(iso_lines[0]).astimezone(_PACIFIC)


def _group_entries_by_date(entries: list[tuple[Path, str]], repo_root: Path) -> dict[str, list[tuple[Path, str]]]:
    """Group entries by the YYYY-MM-DD (Pacific) their PR landed on the current branch.

    Within each date the entries keep their input order (i.e. filename-sorted
    by ``_collect_project_entries``).
    """
    by_date: dict[str, list[tuple[Path, str]]] = {}
    for path, content in entries:
        date_str = _get_entry_added_datetime(path, repo_root).strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append((path, content))
    return by_date


def _build_dated_sections(by_date: dict[str, list[tuple[Path, str]]]) -> str:
    """Build one ``## YYYY-MM-DD`` section per date, newest first."""
    parts: list[str] = []
    for date_str in sorted(by_date, reverse=True):
        section_lines = [f"## {date_str}", ""]
        for _path, content in by_date[date_str]:
            section_lines.append(content)
            section_lines.append("")
        parts.append("\n".join(section_lines))
    return "\n".join(parts)


def _insert_section_into_changelog(changelog_path: Path, new_block: str) -> None:
    """Insert a pre-built block (one or more ``## YYYY-MM-DD`` sections) after
    the header of the existing changelog file, before any pre-existing date
    sections.
    """
    if not changelog_path.exists():
        raise FileNotFoundError(f"Changelog file does not exist: {changelog_path}")
    existing = changelog_path.read_text()

    # Find where to insert: right before the first existing ## section.
    # If there are no ## sections, append at the end.
    lines = existing.split("\n")
    insert_index = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## "):
            insert_index = i
            break

    before = "\n".join(lines[:insert_index]).rstrip("\n")
    after = "\n".join(lines[insert_index:])

    # Ensure exactly one blank line before the new block and before any
    # existing sections that follow it.
    result = before + "\n\n" + new_block
    if after:
        result += "\n" + after

    changelog_path.write_text(result)


def _format_section_line(project: str, dates_added: Sequence[str]) -> str:
    """Format the per-project stdout signal the consolidation prompt parses.

    One line per project -- ``SECTION <project> <date> [<date> ...]`` -- with
    the inserted dates (newest first) space-separated.
    """
    return f"SECTION {project} {' '.join(dates_added)}"


def _consolidate_project(project: str, repo_root: Path) -> tuple[list[str], list[str]]:
    """Consolidate one project's pending entries into its UNABRIDGED_CHANGELOG.md.

    Returns ``(dates_added, entry_filenames)``. Empty lists if the project
    had no entries to consolidate. Raises ``FileNotFoundError`` if the
    project's ``UNABRIDGED_CHANGELOG.md`` is missing (the file is the
    contract -- creating it would mask a project that was added without
    setting up its changelog).
    """
    entries_dir = project_entries_dir(project, repo_root)
    entries = _collect_project_entries(entries_dir)
    if not entries:
        return [], []

    project_root = project_dir(project, repo_root)
    unabridged_path = project_root / "UNABRIDGED_CHANGELOG.md"
    if not unabridged_path.exists():
        raise FileNotFoundError(
            f"Project {project!r} has pending changelog entries under "
            f"{entries_dir.relative_to(repo_root)}/ but is missing "
            f"{unabridged_path.relative_to(repo_root)}. Create the file (with a "
            f"header and no date sections) before re-running."
        )

    by_date = _group_entries_by_date(entries, repo_root)
    new_block = _build_dated_sections(by_date)
    _insert_section_into_changelog(unabridged_path, new_block)

    for path, _content in entries:
        path.unlink()

    dates_added = sorted(by_date, reverse=True)
    entry_names = [path.name for path, _ in entries]
    return dates_added, entry_names


def main() -> None:
    total_entries = 0
    consolidated_any = False
    projects_with_entries = 0
    # Emit one "SECTION <project> <date> [<date> ...]" line per project the
    # consolidator just touched, listing (newest first) the dates whose
    # "## YYYY-MM-DD" sections were inserted into that project's
    # UNABRIDGED_CHANGELOG.md. The consolidation prompt parses these to know
    # which projects to summarize and which dated sections to read; one line
    # per project matches how it summarizes (per project, pooling all dates).
    for project in all_known_projects(_REPO_ROOT):
        dates_added, entry_names = _consolidate_project(project, _REPO_ROOT)
        if not dates_added:
            continue
        consolidated_any = True
        projects_with_entries += 1
        total_entries += len(entry_names)
        target = project_dir(project, _REPO_ROOT).relative_to(_REPO_ROOT)
        print(f"Consolidated {len(entry_names)} entries for {project!r} into {target}/UNABRIDGED_CHANGELOG.md.")
        print(f"  Deleted: {', '.join(entry_names)}")
        print(_format_section_line(project, dates_added))

    if not consolidated_any:
        print("No changelog entries found. Nothing to consolidate.")
        return

    print(f"Total: consolidated {total_entries} entries across {projects_with_entries} project(s).")


if __name__ == "__main__":
    sys.exit(main() or 0)
