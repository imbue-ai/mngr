"""Finalize the CHANGELOG.md [Unreleased] section at release time.

Called by scripts/release.py during a release: renames the [Unreleased]
heading to a versioned heading and inserts a fresh empty [Unreleased]
heading above it so the next consolidation cron run has somewhere to
append.
"""

from datetime import datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

UNRELEASED_HEADING: Final[str] = "## [Unreleased]"
_PACIFIC: Final[ZoneInfo] = ZoneInfo("America/Los_Angeles")


def today_pacific() -> str:
    """Return today's date in America/Los_Angeles as YYYY-MM-DD.

    Matches the timezone the consolidation cron uses for its UNABRIDGED
    section headings, so the release-date heading lines up with the
    most recently consolidated entries.
    """
    return datetime.now(_PACIFIC).strftime("%Y-%m-%d")


def finalize_changelog_unreleased(changelog_path: Path, version: str, release_date: str) -> bool:
    """Rename ``## [Unreleased]`` to ``## [v<version>] - <release_date>``
    and insert a fresh empty ``## [Unreleased]`` heading above it.

    Returns True if the [Unreleased] section had any non-blank content
    (so the release will have populated release notes), False if it was
    empty. The caller can warn on False without aborting -- the version
    section is emitted either way so the invariant "every release has
    a section" holds.

    Raises ``FileNotFoundError`` if the file is missing, ``RuntimeError``
    if the [Unreleased] heading is missing or appears more than once.
    """
    if not changelog_path.exists():
        raise FileNotFoundError(f"Changelog file not found: {changelog_path}")
    lines = changelog_path.read_text().split("\n")
    matches = [i for i, line in enumerate(lines) if line == UNRELEASED_HEADING]
    if not matches:
        raise RuntimeError(f"{UNRELEASED_HEADING} heading not found in {changelog_path}")
    if len(matches) > 1:
        line_numbers = ", ".join(str(i + 1) for i in matches)
        raise RuntimeError(f"Multiple {UNRELEASED_HEADING} headings in {changelog_path} (lines {line_numbers})")
    idx = matches[0]

    # An [Unreleased] section is empty when every line between its heading and
    # the next ## heading (or EOF) is blank.
    has_content = False
    for line in lines[idx + 1 :]:
        if line.startswith("## "):
            break
        if line.strip():
            has_content = True
            break

    new_heading = f"## [v{version}] - {release_date}"
    lines[idx] = f"{UNRELEASED_HEADING}\n\n{new_heading}"
    changelog_path.write_text("\n".join(lines))
    return has_content
