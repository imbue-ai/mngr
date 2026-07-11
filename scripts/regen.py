#!/usr/bin/env python3
"""Regenerate (or --check) every code-derived artifact in the repo.

Usage:
    uv run python scripts/regen.py            # regenerate all generated files in place
    uv run python scripts/regen.py --check    # exit non-zero if any are out of date

This is the single umbrella over the individual generators (the CLI docs, the
agent capability matrix doc, and the constraints file). Each generator exposes a
``collect_generated_files(repo_root) -> {path: content}`` function; this script
aggregates them so one command regenerates everything and one check guards them
all. ``just regenerate`` runs this script.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

from make_agent_capabilities_doc import collect_generated_files as collect_agent_capabilities_doc
from make_cli_docs import collect_generated_files as collect_cli_docs
from make_constraints_file import collect_generated_files as collect_constraints_file

REGEN_COMMAND: Final[str] = "just regenerate"

_GENERATORS: Final[tuple[Callable[[Path], dict[Path, str]], ...]] = (
    collect_cli_docs,
    collect_agent_capabilities_doc,
    collect_constraints_file,
)


def collect_all_generated_files(repo_root: Path) -> dict[Path, str]:
    """Aggregate every generator's expected {path: content} into one mapping."""
    generated: dict[Path, str] = {}
    for collect_generated_files in _GENERATORS:
        generated.update(collect_generated_files(repo_root))
    return generated


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
        help="Do not write any files; exit non-zero if any generated artifact is out of date.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    generated = collect_all_generated_files(repo_root)
    stale = _find_stale_files(generated)

    if args.check:
        if stale:
            print("The following generated artifacts are out of date:")
            for path in stale:
                print(f"  - {path.relative_to(repo_root)}")
            print(f"\nRun this to regenerate them:\n  {REGEN_COMMAND}")
            sys.exit(1)
        return

    for path in stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated[path])
        print(f"Updated: {path.relative_to(repo_root)}")


if __name__ == "__main__":
    main()
