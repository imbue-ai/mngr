"""Publish tombstone packages for the old mng-* PyPI names.

Each tombstone package contains only a README pointing users to the new
imbue-mngr-* package, plus a dependency on the new package so that
`pip install --upgrade mng` automatically pulls in imbue-mngr.

This is a one-shot script. Run it once after the first imbue-mngr-* release
to claim the old names and redirect users.

Usage:
    uv run scripts/release_tombstones.py                # trigger the publish workflow
    uv run scripts/release_tombstones.py --dry-run      # build locally, don't publish
    uv run scripts/release_tombstones.py --build-to DIR # build into DIR (used by CI)
"""

import argparse
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Old PyPI name -> new PyPI name.
#
# Only packages whose name changed are listed here. Packages that kept their
# name (resource-guards, imbue-common, concurrency-group, modal-proxy) don't
# need tombstones.
# ---------------------------------------------------------------------------
TOMBSTONES: Final[dict[str, str]] = {
    "mng": "imbue-mngr",
    "mng-claude": "imbue-mngr-claude",
    "mng-kanpan": "imbue-mngr-kanpan",
    "mng-modal": "imbue-mngr-modal",
    "mng-opencode": "imbue-mngr-opencode",
    "mng-pair": "imbue-mngr-pair",
    "mng-tutor": "imbue-mngr-tutor",
}

# ---------------------------------------------------------------------------
# Fill in the correct versions before running this script.
#
# Each tombstone version must be higher than the last published version of
# that old package on PyPI (so that `pip install --upgrade` picks it up).
# The dependency version should be the first imbue-mngr-* release version.
#
# Format: (tombstone_version, new_package_version).
# Example: ("0.1.9", "0.2.0") means publish the tombstone at 0.1.9,
# depending on imbue-mngr==0.2.0.
# ---------------------------------------------------------------------------
VERSIONS: Final[dict[str, tuple[str, str]]] = {
    "mng": ("0.1.9", "0.2.0"),
    "mng-claude": ("0.1.1", "0.2.0"),
    "mng-kanpan": ("0.1.4", "0.2.0"),
    "mng-modal": ("0.1.1", "0.2.0"),
    "mng-opencode": ("0.1.5", "0.2.0"),
    "mng-pair": ("0.1.5", "0.2.0"),
    "mng-tutor": ("0.1.4", "0.2.0"),
}

PUBLISH_WORKFLOW: Final[str] = "publish-tombstones.yml"


def _check_versions() -> None:
    """Abort if any version is still a TODO placeholder."""
    missing = [name for name, (tv, dv) in VERSIONS.items() if tv == "TODO" or dv == "TODO"]
    if missing:
        print("ERROR: The following packages still have TODO versions:", file=sys.stderr)
        for name in missing:
            tv, dv = VERSIONS[name]
            print(f"  {name}: tombstone_version={tv}, new_package_version={dv}", file=sys.stderr)
        print(file=sys.stderr)
        print("Fill in VERSIONS in this script before running it.", file=sys.stderr)
        sys.exit(1)


def _make_readme(old_name: str, new_name: str) -> str:
    return textwrap.dedent(f"""\
        # {old_name}

        This package has been renamed to [{new_name}](https://pypi.org/project/{new_name}/).

        Install the new package:

        ```
        pip install {new_name}
        ```
    """)


def _make_pyproject(old_name: str, new_name: str, tombstone_version: str, new_version: str) -> str:
    return textwrap.dedent(f"""\
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [project]
        name = "{old_name}"
        version = "{tombstone_version}"
        description = "Renamed to {new_name}. Install {new_name} instead."
        readme = "README.md"
        requires-python = ">=3.11"
        license = "MIT"
        dependencies = ["{new_name}=={new_version}"]
    """)


def build_tombstones(dist_dir: Path) -> None:
    """Build all tombstone packages into dist_dir."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    for old_name, new_name in sorted(TOMBSTONES.items()):
        print(f"  Building {old_name} -> {new_name}")
        tombstone_version, new_version = VERSIONS[old_name]
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            (pkg_dir / "README.md").write_text(_make_readme(old_name, new_name))
            (pkg_dir / "pyproject.toml").write_text(
                _make_pyproject(old_name, new_name, tombstone_version, new_version)
            )
            # Hatchling requires a package directory matching the project name.
            pkg_name_dir = pkg_dir / old_name.replace("-", "_")
            pkg_name_dir.mkdir()
            (pkg_name_dir / "__init__.py").write_text("")
            subprocess.run(
                ["pyproject-build", str(pkg_dir), "-o", str(dist_dir)],
                check=True,
            )
    print(f"\nBuilt {len(TOMBSTONES)} tombstone packages.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and publish tombstone packages for old mng-* PyPI names.")
    parser.add_argument("--dry-run", action="store_true", help="Build locally to verify, don't publish")
    parser.add_argument("--build-to", metavar="DIR", help="Build packages into DIR (used by CI workflow)")
    args = parser.parse_args()

    _check_versions()

    # --build-to: just build into the given directory (used by the CI workflow)
    if args.build_to is not None:
        build_tombstones(Path(args.build_to))
        return

    # --dry-run: build into a temp directory for local verification
    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="tombstones-dist-") as tmp:
            build_tombstones(Path(tmp))
            print(f"\n(dry run -- packages were in {tmp})")
        return

    # Default: trigger the GitHub Actions workflow for trusted publishing
    print("Triggering publish-tombstones workflow...")
    subprocess.run(
        ["gh", "workflow", "run", PUBLISH_WORKFLOW],
        check=True,
    )
    print("Workflow triggered. Watch it at:")
    print("  https://github.com/imbue-ai/mngr/actions/workflows/publish-tombstones.yml")


if __name__ == "__main__":
    main()
