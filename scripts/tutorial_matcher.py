#!/usr/bin/env python3
"""Find unmatched blocks between a tutorial shell script and its pytest test directory.

Usage: python scripts/tutorial_matcher.py <script_file> <test_directory>

The script file is a shell script split into "blocks" by empty lines. The test
directory contains pytest functions that reference blocks. Matching is done by
checking whether every line of a script block (after stripping leading whitespace)
appears in the function body (also stripped of leading whitespace).
"""

import sys
from pathlib import Path


def parse_script_blocks(script_path: Path) -> list[str]:
    """Parse a shell script into command blocks, filtering out shebangs and comment-only blocks."""
    content = script_path.read_text()
    raw_blocks = content.split("\n\n")

    blocks: list[str] = []
    for i, block in enumerate(raw_blocks):
        stripped = block.strip()
        if not stripped:
            continue
        if i == 0 and stripped.startswith("#!"):
            continue
        lines = stripped.splitlines()
        if all(line.strip() == "" or line.strip().startswith("#") for line in lines):
            continue
        blocks.append(stripped)

    return blocks


def _strip_lines(text: str) -> list[str]:
    """Strip leading whitespace from each line and drop empty lines."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _block_lines_in_body(block: str, body: str) -> bool:
    """Check if all non-empty lines of a block appear in order in the body.

    Both block and body lines are stripped of leading whitespace before comparison.
    """
    block_lines = _strip_lines(block)
    body_lines = _strip_lines(body)

    if not block_lines:
        return False

    bi = 0
    for body_line in body_lines:
        if bi < len(block_lines) and body_line == block_lines[bi]:
            bi += 1
    return bi == len(block_lines)


def _parse_test_functions(source: str) -> list[tuple[str, str]]:
    """Parse test functions from Python source, returning (signature, body) tuples."""
    lines = source.splitlines()
    functions: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if not stripped.startswith("def test_"):
            i += 1
            continue

        sig_lines = [line]
        while i + 1 < len(lines) and (")" not in sig_lines[-1] or ":" not in sig_lines[-1]):
            i += 1
            sig_lines.append(lines[i])
        signature = "\n".join(sig_lines)
        i += 1

        body_lines: list[str] = []
        while i < len(lines):
            if lines[i].strip() == "":
                body_lines.append("")
                i += 1
                continue
            if lines[i][0] in (" ", "\t"):
                body_lines.append(lines[i])
                i += 1
            else:
                break

        body = "\n".join(body_lines)
        functions.append((signature, body))

    return functions


def find_pytest_functions(test_dir: Path) -> list[tuple[str, str, Path]]:
    """Find all test functions in a directory, returning (signature, body, file_path) tuples."""
    results: list[tuple[str, str, Path]] = []

    for py_file in sorted(test_dir.rglob("*.py")):
        if py_file.name in ("conftest.py", "serve_test_output.py"):
            continue
        source = py_file.read_text()
        for signature, body in _parse_test_functions(source):
            results.append((signature, body, py_file))

    return results


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <script_file> <test_directory>", file=sys.stderr)
        sys.exit(1)

    script_path = Path(sys.argv[1])
    test_dir = Path(sys.argv[2])

    if not script_path.is_file():
        print(f"Error: {script_path} is not a file", file=sys.stderr)
        sys.exit(1)
    if not test_dir.is_dir():
        print(f"Error: {test_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    blocks = parse_script_blocks(script_path)
    pytest_funcs = find_pytest_functions(test_dir)

    unmatched_blocks: list[str] = []
    for block in blocks:
        has_match = any(_block_lines_in_body(block, body) for _, body, _ in pytest_funcs)
        if not has_match:
            unmatched_blocks.append(block)

    unmatched_funcs: list[tuple[str, str, Path]] = []
    for signature, body, file_path in pytest_funcs:
        has_match = any(_block_lines_in_body(block, body) for block in blocks)
        if not has_match:
            unmatched_funcs.append((signature, body, file_path))

    if not unmatched_blocks and not unmatched_funcs:
        print("All script blocks have corresponding pytest functions and vice versa.")
        sys.exit(0)

    if unmatched_blocks:
        print("The following script blocks don't have corresponding pytest functions:\n")
        for block in unmatched_blocks:
            print(f"```\n{block}\n```\n")

    if unmatched_funcs:
        print("The following pytest functions don't correspond to any script block:\n")
        for signature, _, file_path in unmatched_funcs:
            print(f"```\n# {file_path}\n{signature}\n```\n")

    sys.exit(1)


if __name__ == "__main__":
    main()
