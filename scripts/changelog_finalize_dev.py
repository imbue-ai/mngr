"""Cut the ``dev`` project's CHANGELOG.md [Unreleased] section into a dated
``## <date>`` section.

The synthetic ``dev`` project (repo-level CI/build/tooling changes) is never
released, so -- unlike the publishable libs/apps, whose CHANGELOG.md is
version-organized and cut by ``scripts/release.py`` at release time -- its
concise changelog is organized per date, like ``UNABRIDGED_CHANGELOG.md``. The
nightly consolidation writes ``dev``'s summary bullets under a transient
``## [Unreleased]`` heading (uniformly with every other project) and then runs
this script to rename that heading to the run's date, leaving no standing
[Unreleased] section behind.

Run by ``scripts/changelog_consolidation_prompt.md`` after the accuracy review.
A no-op (exit 0, no change) when ``dev`` has no [Unreleased] content this run.

Usage:
    python3 scripts/changelog_finalize_dev.py [--date YYYY-MM-DD]
"""

import argparse
import sys
from pathlib import Path

from changelog_projects import DEV_PROJECT
from changelog_projects import project_dir
from changelog_release_utils import cut_changelog_unreleased_to_date
from changelog_release_utils import today_pacific

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=None,
        help="The YYYY-MM-DD heading to cut [Unreleased] into (default: today in America/Los_Angeles).",
    )
    args = parser.parse_args()
    date_str = args.date if args.date is not None else today_pacific()

    dev_changelog = project_dir(DEV_PROJECT, _REPO_ROOT) / "CHANGELOG.md"
    if cut_changelog_unreleased_to_date(dev_changelog, date_str):
        print(f"Cut {dev_changelog.relative_to(_REPO_ROOT)} [Unreleased] -> '## {date_str}'.")
    else:
        print(f"{dev_changelog.relative_to(_REPO_ROOT)} has no [Unreleased] content to cut; nothing to do.")


if __name__ == "__main__":
    sys.exit(main() or 0)
