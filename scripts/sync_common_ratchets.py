#!/usr/bin/env python3
"""Synchronize test_ratchets.py files across all projects in the monorepo.

Reads standard_ratchet_checks.py to discover all check_* functions, then
ensures every project's test_ratchets.py has a corresponding test_prevent_*
function. Missing tests are added with snapshot(0).

Workflow for adding a new common ratchet:
1. Add the RegexRatchetRule/RatchetRuleInfo to common_ratchets.py
2. Add a wrapper function to standard_ratchet_checks.py
3. Run: uv run python scripts/sync_common_ratchets.py
4. Run: uv run pytest --inline-snapshot=update -k test_ratchets
"""

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SECTION_HEADER_RE = re.compile(r"^# --- (.+) ---$")

# Projects excluded from ratchet requirements (scheduled for deletion).
# Keep in sync with _EXCLUDED_PROJECTS in test_meta_ratchets.py
# (verified by test_excluded_projects_in_sync in scripts/sync_common_ratchets_test.py).
EXCLUDED_RATCHET_PROJECTS: frozenset[str] = frozenset({"flexmux"})

# Path to the source of truth for which ratchet checks exist.
STANDARD_RATCHET_CHECKS_PATH = (
    REPO_ROOT / "libs" / "imbue_common" / "imbue" / "imbue_common" / "ratchet_testing" / "standard_ratchet_checks.py"
)

# Canonical section order, used for inserting new sections at the right position.
SECTION_ORDER = [
    "Code safety",
    "Exception handling",
    "Import style",
    "Banned libraries and patterns",
    "Naming conventions",
    "Documentation",
    "Type safety",
    "Pydantic / models",
    "Logging",
    "Testing conventions",
    "Process management",
    "AST-based ratchets",
    "Project-level checks",
]


@dataclass(frozen=True)
class RatchetTemplate:
    """A normalized test function to be inserted into files that are missing it."""

    name: str
    source: str
    section: str


def _find_test_ratchet_files() -> list[Path]:
    """Find all test_ratchets.py files across libs/ and apps/."""
    files: list[Path] = []
    for parent_name in ["libs", "apps"]:
        parent = REPO_ROOT / parent_name
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if not child.is_dir() or child.name in EXCLUDED_RATCHET_PROJECTS:
                continue
            if not (child / "pyproject.toml").exists():
                continue
            matches = list(child.rglob("test_ratchets.py"))
            if len(matches) == 1:
                files.append(matches[0])
            elif len(matches) > 1:
                _warn(f"multiple test_ratchets.py in {child.name}, skipping")
    return files


def _parse_section_headers(text: str) -> list[tuple[int, str]]:
    """Return (line_index, section_name) for each section header."""
    result: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines()):
        m = SECTION_HEADER_RE.match(line.strip())
        if m:
            result.append((i, m.group(1)))
    return result


def _extract_tests(text: str) -> list[RatchetTemplate]:
    """Extract all test functions from a test_ratchets.py file."""
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    sections = _parse_section_headers(text)

    tests: list[RatchetTemplate] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue

        start = node.lineno - 1
        if node.decorator_list:
            start = node.decorator_list[0].lineno - 1
        end = node.end_lineno
        assert end is not None

        source = "".join(lines[start:end])

        section = "Unknown"
        for sec_line, sec_name in sections:
            if sec_line < start:
                section = sec_name

        tests.append(RatchetTemplate(name=node.name, source=source, section=section))

    return tests


def _section_sort_key(section_name: str) -> int:
    """Sort key for section ordering."""
    try:
        return SECTION_ORDER.index(section_name)
    except ValueError:
        return len(SECTION_ORDER)


def _find_section_end(lines: list[str], sections: list[tuple[int, str]], section_name: str) -> int | None:
    """Find the line index where new content should be inserted at the end of a section.

    Returns the line after the last non-blank line of the section, or None if
    the section does not exist in the file.
    """
    section_names = [name for _, name in sections]
    if section_name not in section_names:
        return None

    sec_idx = section_names.index(section_name)
    sec_line = sections[sec_idx][0]

    if sec_idx + 1 < len(sections):
        boundary = sections[sec_idx + 1][0]
    else:
        boundary = len(lines)

    insert_at = boundary
    while insert_at > sec_line and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    return insert_at


def _find_section_insertion_point(sections: list[tuple[int, str]], section_name: str, total_lines: int) -> int:
    """Find where to insert a brand-new section header, respecting canonical order."""
    target_order = _section_sort_key(section_name)
    for existing_line, existing_name in sections:
        if _section_sort_key(existing_name) > target_order:
            return existing_line
    return total_lines


def _insert_test(lines: list[str], test: RatchetTemplate) -> list[str]:
    """Insert a test function into the appropriate section, returning updated lines."""
    sections = _parse_section_headers("\n".join(lines))
    test_lines = test.source.rstrip("\n").splitlines()

    end = _find_section_end(lines, sections, test.section)
    if end is not None:
        insertion = ["", ""] + test_lines
        return lines[:end] + insertion + lines[end:]

    insert_at = _find_section_insertion_point(sections, test.section, len(lines))
    # Back up over trailing blank lines before the insertion point.
    while insert_at > 0 and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    section_header = f"# --- {test.section} ---"
    insertion = ["", "", section_header, "", ""] + test_lines
    return lines[:insert_at] + insertion + lines[insert_at:]


def _discover_check_functions() -> list[RatchetTemplate]:
    """Parse standard_ratchet_checks.py to discover all check_* functions and their sections.

    Returns a RatchetTemplate for each check function, with the test name derived
    as test_prevent_{check_name} and a default body of rc.check_{name}(_DIR, snapshot(0)).
    """
    text = STANDARD_RATCHET_CHECKS_PATH.read_text()
    tree = ast.parse(text)
    sections = _parse_section_headers(text)

    templates: list[RatchetTemplate] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("check_"):
            continue

        check_name = node.name
        test_name = f"test_prevent_{check_name.removeprefix('check_')}"
        test_source = f"def {test_name}() -> None:\n    rc.{check_name}(_DIR, snapshot(0))"

        section = "Unknown"
        func_line = node.lineno - 1
        for sec_line, sec_name in sections:
            if sec_line < func_line:
                section = sec_name

        templates.append(RatchetTemplate(name=test_name, source=test_source, section=section))

    return templates


def _warn(msg: str) -> None:
    print(f"  warning: {msg}", file=sys.stderr)


def main() -> int:
    files = _find_test_ratchet_files()
    if not files:
        print("No test_ratchets.py files found.", file=sys.stderr)
        return 1

    print(f"Found {len(files)} test_ratchets.py file(s)")

    # Discover check functions from standard_ratchet_checks.py (the source of truth).
    discovered = _discover_check_functions()
    templates: dict[str, RatchetTemplate] = {t.name: t for t in discovered}
    canonical_names = set(templates.keys())
    print(f"Discovered {len(canonical_names)} check functions in standard_ratchet_checks.py")

    # Parse all files to find which tests each has.
    file_test_names: dict[Path, set[str]] = {}
    for f in files:
        tests = _extract_tests(f.read_text())
        file_test_names[f] = {t.name for t in tests}

    # Sync each file.
    modified_files: list[Path] = []
    for f in files:
        missing = canonical_names - file_test_names[f]
        if not missing:
            continue

        rel = f.relative_to(REPO_ROOT)
        sorted_missing = sorted(missing, key=lambda n: (_section_sort_key(templates[n].section), n))
        print(f"\n  {rel}: adding {len(missing)} test(s):")
        for name in sorted_missing:
            print(f"    + {name} [{templates[name].section}]")

        lines = f.read_text().splitlines()
        for name in sorted_missing:
            lines = _insert_test(lines, templates[name])

        f.write_text("\n".join(lines) + "\n")
        modified_files.append(f)

    if not modified_files:
        print("\nAll test_ratchets.py files are already in sync.")
        return 0

    print(f"\nModified {len(modified_files)} file(s). Running ruff format...")
    subprocess.run(
        ["uv", "run", "ruff", "format"] + [str(f) for f in modified_files],
        cwd=REPO_ROOT,
        check=False,
    )

    print("\nNext steps:")
    print("  uv run pytest --inline-snapshot=update -k test_ratchets")
    print("  (to set the actual violation counts per project)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
