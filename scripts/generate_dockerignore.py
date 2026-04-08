"""Generate .dockerignore from .gitignore.

Since .gitignore is normalized (all patterns use **/ or / prefixes), the
transformation is straightforward: strip leading / (root-only in gitignore
is already the default in dockerignore) and append docker-specific entries.

Usage:
    python scripts/generate_dockerignore.py          # write .dockerignore
    python scripts/generate_dockerignore.py --check  # exit 1 if stale
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = REPO_ROOT / ".gitignore"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

HEADER = """\
# AUTO-GENERATED from .gitignore by scripts/generate_dockerignore.py
# Do not edit manually -- edit .gitignore instead, then run:
#   uv run python scripts/generate_dockerignore.py
#
# The Docker image only needs current.tar.gz; all other repo files are
# delivered inside the tarball. We exclude everything .gitignore excludes
# plus docker-specific entries at the bottom.
"""

# Patterns to add that are not in .gitignore (docker-specific).
DOCKER_EXTRA = """\

# Docker-specific (not in .gitignore)
.git/
"""

# Patterns from .gitignore that must NOT appear in .dockerignore because
# they are needed in the Docker build context.
EXCLUDE_PATTERNS = {
    "/current.tar.gz",
}


def transform_line(line: str) -> str | None:
    """Transform a single .gitignore line to .dockerignore format.

    Returns None if the line should be excluded from .dockerignore.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return line.rstrip()

    # Check if this pattern should be excluded.
    if stripped in EXCLUDE_PATTERNS or stripped.lstrip("!") in EXCLUDE_PATTERNS:
        return None

    # Strip leading / and ! prefix, then reconstruct.
    # / means root-only in gitignore, which is already the default in dockerignore.
    return ("!" if stripped.startswith("!") else "") + stripped.lstrip("!/")


def generate() -> str:
    """Generate .dockerignore content from .gitignore."""
    lines = GITIGNORE.read_text().splitlines()
    out = [HEADER]
    for line in lines:
        transformed = transform_line(line)
        if transformed is not None:
            out.append(transformed)
    out.append(DOCKER_EXTRA)
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    expected = generate()

    if args.check:
        if not DOCKERIGNORE.exists():
            print(".dockerignore does not exist", file=sys.stderr)
            sys.exit(1)
        actual = DOCKERIGNORE.read_text()
        if actual != expected:
            print(
                ".dockerignore is stale -- run: uv run python scripts/generate_dockerignore.py",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.exit(0)

    DOCKERIGNORE.write_text(expected)


if __name__ == "__main__":
    main()
