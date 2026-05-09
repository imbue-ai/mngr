"""Consolidate individual changelog entry files into UNABRIDGED_CHANGELOG.md.

Reads all .md files in the changelog/ directory (excluding .gitkeep),
groups them by the date the entry's PR landed on the current branch
(committer date of the introducing commit on the first-parent line, in
America/Los_Angeles), prepends one date-headed section per distinct date
to UNABRIDGED_CHANGELOG.md (newest first), and deletes the individual
files.

Exits with code 0 and no changes if there are no changelog entries to consolidate.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG_DIR = _REPO_ROOT / "changelog"
_CHANGELOG_FILE = _REPO_ROOT / "UNABRIDGED_CHANGELOG.md"
_PACIFIC = ZoneInfo("America/Los_Angeles")


def _collect_entries(changelog_dir: Path) -> list[tuple[Path, str]]:
    """Collect all changelog entry files and their contents.

    Returns a sorted list of (path, content) tuples. Excludes .gitkeep.
    """
    entries: list[tuple[Path, str]] = []
    for path in sorted(changelog_dir.iterdir()):
        if path.name == ".gitkeep" or not path.name.endswith(".md"):
            continue
        content = path.read_text().strip()
        if content:
            entries.append((path, content))
    return entries


def _get_entry_added_datetime(path: Path, repo_root: Path) -> datetime:
    """Return when the entry's PR landed on the current branch, as PT.

    Uses ``git log --first-parent --diff-filter=A --format=%cI -- <path>``
    to find the committer date of the commit that introduced the file on
    the current branch's first-parent line. For files merged in via a PR
    merge commit, that's the merge commit's committer date -- i.e. when
    the PR landed -- not the feature-branch author date. If the file has
    been added more than once on the first-parent line, takes the most
    recent.
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
    by ``_collect_entries``).
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


def main() -> None:
    if not _CHANGELOG_DIR.is_dir():
        print("No changelog/ directory found. Nothing to consolidate.")
        return

    entries = _collect_entries(_CHANGELOG_DIR)
    if not entries:
        print("No changelog entries found. Nothing to consolidate.")
        return

    by_date = _group_entries_by_date(entries, _REPO_ROOT)
    new_block = _build_dated_sections(by_date)
    _insert_section_into_changelog(_CHANGELOG_FILE, new_block)

    # Delete the individual entry files
    for path, _content in entries:
        path.unlink()

    dates_added = sorted(by_date, reverse=True)
    print(f"Consolidated {len(entries)} changelog entries into {_CHANGELOG_FILE.name}.")
    # Machine-readable line for the orchestration prompt to parse.
    print(f"Sections added: {', '.join(dates_added)}")
    entry_names = [path.name for path, _ in entries]
    print(f"Deleted: {', '.join(entry_names)}")


if __name__ == "__main__":
    sys.exit(main() or 0)
