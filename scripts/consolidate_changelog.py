"""Consolidate individual changelog entry files into UNABRIDGED_CHANGELOG.md.

Reads all .md files in the changelog/ directory (excluding .gitkeep),
prepends a new date-headed section to UNABRIDGED_CHANGELOG.md with their contents,
and deletes the individual files.

Exits with code 0 and no changes if there are no changelog entries to consolidate.
"""

import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG_DIR = _REPO_ROOT / "changelog"
_CHANGELOG_FILE = _REPO_ROOT / "UNABRIDGED_CHANGELOG.md"


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

    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
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
