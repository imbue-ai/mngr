"""Pre-publish helpers for CI: verify versions/graph/pins, and list package dirs.

Called from the publish workflow. In its default mode, verifies:
1. Displays all package versions
2. If --expected-mngr-version is given, checks mngr version matches (for tag/dispatch checks)
3. The hard-coded package graph matches actual pyproject.toml declarations
4. All internal dependency pins are consistent

With --list-package-dirs, instead prints `libs/<dir_name>` for each publishable
package (one per line) and exits without running any verification. This is used
by the publish workflow to drive the per-package build loop.

Usage:
    uv run scripts/verify_publish.py
    uv run scripts/verify_publish.py --expected-mngr-version 0.1.5
    uv run scripts/verify_publish.py --list-package-dirs
"""

import argparse
import sys

from utils import PACKAGES
from utils import get_package_versions
from utils import validate_package_graph
from utils import verify_pin_consistency


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-publish verification.")
    parser.add_argument(
        "--expected-mngr-version",
        help="If set, verify the mngr package version matches this value",
    )
    parser.add_argument(
        "--list-package-dirs",
        action="store_true",
        help="Print one libs/<dir> per line for each publishable package, then exit",
    )
    args = parser.parse_args()

    if args.list_package_dirs:
        for pkg in PACKAGES:
            print(f"libs/{pkg.dir_name}")
        return

    # Display all package versions
    versions = get_package_versions()
    print("=== Package versions ===")
    for name, version in versions.items():
        print(f"  {name}: {version}")

    # Optionally verify mngr version matches an expected value (tag or dispatch input)
    if args.expected_mngr_version is not None:
        mngr_version = versions["imbue-mngr"]
        if mngr_version != args.expected_mngr_version:
            print(
                f"\nERROR: Expected mngr version {args.expected_mngr_version} but found {mngr_version}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"\nmngr version matches expected: {mngr_version}")

    # Verify the hard-coded package graph matches actual pyproject.toml declarations
    print("\n=== Package graph validation ===")
    validate_package_graph()
    print("Package graph is consistent.")

    # Verify pin consistency
    print("\n=== Pin consistency check ===")
    errors = verify_pin_consistency()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
    print("All internal dependency pins are consistent.")


if __name__ == "__main__":
    main()
