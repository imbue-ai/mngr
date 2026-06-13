"""Finalize the CHANGELOG.md [Unreleased] section.

``finalize_changelog_unreleased`` is called by scripts/release.py during a
release: it renames the [Unreleased] heading to a versioned heading and
inserts a fresh empty [Unreleased] heading above it so the next
consolidation cron run has somewhere to append.

``cut_changelog_unreleased_to_date`` is the date-organized counterpart used
by the nightly consolidation for the synthetic ``dev`` project. ``dev`` is
never released -- its tooling/CI/build changes are effectively "released" the
moment they merge -- so its concise changelog is organized per date (like
``UNABRIDGED_CHANGELOG.md``) rather than per version. This function renames
[Unreleased] to a ``## <date>`` heading and, deliberately, does NOT re-insert
an empty [Unreleased]: ``dev`` carries no standing [Unreleased] section
between runs.
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


def _find_sole_unreleased_index(lines: list[str], changelog_path: Path) -> int:
    """Return the index of the single ``## [Unreleased]`` heading line.

    Raises ``RuntimeError`` if the heading is missing or appears more than once.
    """
    matches = [i for i, line in enumerate(lines) if line == UNRELEASED_HEADING]
    if not matches:
        raise RuntimeError(f"{UNRELEASED_HEADING} heading not found in {changelog_path}")
    if len(matches) > 1:
        line_numbers = ", ".join(str(i + 1) for i in matches)
        raise RuntimeError(f"Multiple {UNRELEASED_HEADING} headings in {changelog_path} (lines {line_numbers})")
    return matches[0]


def _unreleased_has_content(lines: list[str], idx: int) -> bool:
    """An [Unreleased] section is empty when every line between its heading and
    the next ``## `` heading (or EOF) is blank.
    """
    for line in lines[idx + 1 :]:
        if line.startswith("## "):
            break
        if line.strip():
            return True
    return False


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
    idx = _find_sole_unreleased_index(lines, changelog_path)
    has_content = _unreleased_has_content(lines, idx)

    new_heading = f"## [v{version}] - {release_date}"
    lines[idx] = f"{UNRELEASED_HEADING}\n\n{new_heading}"
    changelog_path.write_text("\n".join(lines))
    return has_content


def cut_changelog_unreleased_to_date(changelog_path: Path, date_str: str) -> bool:
    """Rename ``## [Unreleased]`` to ``## <date_str>`` for a never-released
    (date-organized) changelog, WITHOUT re-inserting an empty [Unreleased].

    Used by the nightly consolidation for the ``dev`` project. Returns True if
    a section was cut, False (a no-op) when there is nothing to cut -- i.e. the
    file has no [Unreleased] heading (the normal between-runs state for ``dev``)
    or its [Unreleased] section is empty. Like ``UNABRIDGED_CHANGELOG.md``, this
    does not merge into a pre-existing same-date section; a same-day re-run can
    leave two ``## <date>`` headings, which is tolerated.

    Raises ``FileNotFoundError`` if the file is missing, ``RuntimeError`` if the
    [Unreleased] heading appears more than once.
    """
    if not changelog_path.exists():
        raise FileNotFoundError(f"Changelog file not found: {changelog_path}")
    lines = changelog_path.read_text().split("\n")
    if UNRELEASED_HEADING not in lines:
        return False
    idx = _find_sole_unreleased_index(lines, changelog_path)
    if not _unreleased_has_content(lines, idx):
        return False

    lines[idx] = f"## {date_str}"
    changelog_path.write_text("\n".join(lines))
    return True
