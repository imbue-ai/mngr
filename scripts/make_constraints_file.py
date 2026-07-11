#!/usr/bin/env python3
"""Generate libs/mngr/constraints.txt from the workspace uv.lock.

Usage:
    uv run python scripts/make_constraints_file.py            # regenerate in place
    uv run python scripts/make_constraints_file.py --check    # exit non-zero if stale

The constraints file re-applies the lockfile's third-party pins at install time.
``scripts/install.sh`` and ``mngr plugin add`` pass it to
``uv tool install --constraint``, so end-user installs resolve to the same
third-party versions CI tested -- uv.lock and the ``exclude-newer`` cutoff only
constrain resolution inside a checkout, never a ``uv tool install`` from PyPI.

Flags:
- ``--all-packages``: cover every workspace package's transitive deps, so plugin
  dependencies (e.g. azure-mgmt-resource) are pinned too, not just base mngr.
- ``--no-emit-workspace``: emit only third-party pins; first-party members are
  pinned by their own published versions. A superset constraints file is harmless
  because uv ignores constraints for packages it does not install.
- ``--no-hashes``: a hash on any line would force hashes for every installed
  package and break resolution of a superset constraints file.
- ``--frozen``: read the existing lock; never re-resolve or mutate uv.lock.

This is one of several code-derived artifacts; regenerate them all with
``just regenerate`` (see scripts/regen.py).
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Final

# The exact command a developer runs to regenerate every generated artifact.
REGEN_COMMAND: Final[str] = "just regenerate"

_UV_EXPORT_COMMAND: Final[tuple[str, ...]] = (
    "uv",
    "export",
    "--all-packages",
    "--no-emit-workspace",
    "--no-dev",
    "--no-hashes",
    "--no-annotate",
    "--frozen",
    "--format",
    "requirements-txt",
)


def constraints_path(repo_root: Path) -> Path:
    """Path to the committed constraints file, relative to the repo root."""
    return repo_root / "libs" / "mngr" / "constraints.txt"


def generate_constraints(repo_root: Path) -> str:
    """Return the constraints file content exported from uv.lock."""
    result = subprocess.run(
        _UV_EXPORT_COMMAND,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def collect_generated_files(repo_root: Path) -> dict[Path, str]:
    """Return the generated constraints file mapped to its expected content.

    Same shape as the other generators (see scripts/regen.py) so the umbrella can
    aggregate them: a single source of truth shared by the writer and the checker.
    """
    return {constraints_path(repo_root): generate_constraints(repo_root)}


def _find_stale_files(generated: dict[Path, str]) -> list[Path]:
    """Return the generated files whose on-disk content differs from what we'd write."""
    stale: list[Path] = []
    for path, content in generated.items():
        existing_content = path.read_text() if path.exists() else None
        if content != existing_content:
            stale.append(path)
    return stale


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write any files; exit non-zero if the constraints file is out of date.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    generated = collect_generated_files(repo_root)
    stale = _find_stale_files(generated)

    if args.check:
        if stale:
            print("libs/mngr/constraints.txt is out of date relative to uv.lock.")
            print(f"\nRun this to regenerate it:\n  {REGEN_COMMAND}")
            sys.exit(1)
        return

    for path, content in generated.items():
        path.write_text(content)
        print(f"Updated: {path}")


if __name__ == "__main__":
    main()
