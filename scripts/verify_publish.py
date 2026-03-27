"""Pre-publish verification for CI: check versions, graph, and pin consistency.

Called from the publish workflow before building packages. Verifies:
1. Displays all package versions
2. If --expected-mngr-version is given, checks mngr version matches (for tag/dispatch checks)
3. The hard-coded package graph matches actual pyproject.toml declarations
4. All internal dependency pins are consistent

Usage:
    uv run scripts/verify_publish.py
    uv run scripts/verify_publish.py --expected-mngr-version 0.1.5
"""

import argparse
import sys

from utils import get_package_versions
from utils import validate_package_graph
from utils import verify_pin_consistency


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-publish verification.")
    parser.add_argument(
        "--expected-mngr-version",
        help="If set, verify the mngr package version matches this value",
    )
    args = parser.parse_args()

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
