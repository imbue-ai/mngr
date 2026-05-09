"""Consolidate individual changelog entry files into UNABRIDGED_CHANGELOG.md.

Reads all .md files in the changelog/ directory (excluding .gitkeep),
prepends a new date-headed section to UNABRIDGED_CHANGELOG.md with their contents,
and deletes the individual files.

The section's date is the most recent of the consolidated entries' git-add
dates (the author date of the commit that first added each file), in
America/Los_Angeles timezone -- so the heading reflects when the entries
were actually written rather than when the consolidator happened to run.

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
    """Return when the entry was added to the repo, as a Pacific-time datetime.

    Uses ``git log --diff-filter=A --format=%aI -- <path>`` to find the
    author date of the commit that added the file. If the file has been
    added more than once (added, deleted, re-added), takes the most recent.
    Falls back to the file's mtime if git has no record (e.g. uncommitted
    file on a local dev box).
    """
    rel = path.relative_to(repo_root)
    result = subprocess.run(
        ["git", "log", "--diff-filter=A", "--format=%aI", "--", str(rel)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    iso_lines = [line for line in result.stdout.splitlines() if line.strip()]
    if iso_lines:
        # `git log` prints newest-first, so iso_lines[0] is the most recent
        # add. fromisoformat parses the offset, giving an aware datetime.
        return datetime.fromisoformat(iso_lines[0]).astimezone(_PACIFIC)
    return datetime.fromtimestamp(path.stat().st_mtime, tz=_PACIFIC)


def _latest_entry_date_str(entries: list[tuple[Path, str]], repo_root: Path) -> str:
    """Return the YYYY-MM-DD (Pacific) of the most recently added entry."""
    return max(_get_entry_added_datetime(path, repo_root) for path, _ in entries).strftime("%Y-%m-%d")


def _build_new_section(date_str: str, entries: list[tuple[Path, str]]) -> str:
    """Build a new changelog section from the collected entries."""
    lines: list[str] = [f"## {date_str}", ""]
    for _path, content in entries:
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


def _insert_section_into_changelog(changelog_path: Path, new_section: str) -> None:
    """Insert a new section after the header of the existing changelog file."""
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

    # Ensure exactly one blank line before the new section and before any
    # existing sections that follow it.
    result = before + "\n\n" + new_section
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

    date_str = _latest_entry_date_str(entries, _REPO_ROOT)
    new_section = _build_new_section(date_str, entries)
    _insert_section_into_changelog(_CHANGELOG_FILE, new_section)

    # Delete the individual entry files
    for path, _content in entries:
        path.unlink()

    print(f"Consolidated {len(entries)} changelog entries into {_CHANGELOG_FILE.name} under {date_str}.")
    entry_names = [path.name for path, _ in entries]
    print(f"Deleted: {', '.join(entry_names)}")


if __name__ == "__main__":
    sys.exit(main() or 0)
