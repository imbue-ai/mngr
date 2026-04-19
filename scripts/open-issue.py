#!/usr/bin/env python3
"""Open a pre-populated GitHub issue in the browser.

Usage:
    python scripts/open-issue.py --title "Bug: ..." body.md

The diagnostic agent calls this from its worktree to open the issue
for user review before submission.
"""

import argparse
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from imbue.mngr.cli.issue_reporting import build_new_issue_url


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Open a pre-populated GitHub issue in the browser")
    parser.add_argument("body_file", type=Path, help="Path to a markdown file containing the issue body")
    parser.add_argument("--title", required=True, help="Issue title string")

    args = parser.parse_args(argv)

    body = args.body_file.read_text()
    url = build_new_issue_url(args.title, body)
    print(f"Opening issue in browser: {args.title}")
    webbrowser.open(url)


if __name__ == "__main__":
    main()
