#!/usr/bin/env python3
"""Find unmatched blocks between a tutorial shell script and its pytest test directory.

Usage: python scripts/tutorial_matcher.py <script_file> <test_directory>

The script file is a shell script split into "blocks" by empty lines. The test
directory contains pytest functions that reference blocks via their docstrings.
This script identifies blocks without tests and tests without blocks.
"""

import ast
import sys
import textwrap
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
        # Discard the first block if it starts with a shebang.
        if i == 0 and stripped.startswith("#!"):
            continue
        # Discard blocks where every line is empty or a comment.
        lines = stripped.splitlines()
        if all(line.strip() == "" or line.strip().startswith("#") for line in lines):
            continue
        blocks.append(stripped)

    return blocks


def find_pytest_functions(test_dir: Path) -> list[tuple[str, str | None, Path]]:
    """Find all pytest functions in a directory, returning (signature, docstring, file_path) tuples."""
    results: list[tuple[str, str | None, Path]] = []

    for py_file in sorted(test_dir.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue

            # Reconstruct the signature line from source.
            source_lines = py_file.read_text().splitlines()
            # Find the def line(s) -- handle multi-line signatures.
            sig_lines: list[str] = []
            for line_idx in range(node.lineno - 1, min(node.end_lineno or node.lineno, len(source_lines))):
                sig_lines.append(source_lines[line_idx])
                if ")" in source_lines[line_idx] and ":" in source_lines[line_idx]:
                    break

            signature = "\n".join(sig_lines)
            docstring = ast.get_docstring(node, clean=False)
            results.append((signature, docstring, py_file))

    return results


def block_matches_docstring(block: str, docstring: str) -> bool:
    """Check if a script block appears in a pytest function's docstring."""
    # The docstring will have the block indented. Dedent the docstring and check
    # if the block appears as a substring.
    dedented = textwrap.dedent(docstring).strip()
    return block in dedented


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

    # Find blocks with no corresponding pytest function.
    unmatched_blocks: list[str] = []
    for block in blocks:
        has_match = any(
            docstring is not None and block_matches_docstring(block, docstring) for _, docstring, _ in pytest_funcs
        )
        if not has_match:
            unmatched_blocks.append(block)

    # Find pytest functions with no corresponding block.
    unmatched_funcs: list[tuple[str, str | None, Path]] = []
    for signature, docstring, file_path in pytest_funcs:
        if docstring is None:
            unmatched_funcs.append((signature, docstring, file_path))
            continue
        has_match = any(block_matches_docstring(block, docstring) for block in blocks)
        if not has_match:
            unmatched_funcs.append((signature, docstring, file_path))

    # Output results.
    if not unmatched_blocks and not unmatched_funcs:
        print("All script blocks have corresponding pytest functions and vice versa.")
        sys.exit(0)

    if unmatched_blocks:
        print("The following script blocks don't have corresponding pytest functions:\n")
        for block in unmatched_blocks:
            print(f"```\n{block}\n```\n")

    if unmatched_funcs:
        print("The following pytest functions don't correspond to any script block:\n")
        for signature, docstring, file_path in unmatched_funcs:
            print(f"```\n# {file_path}\n{signature}")
            if docstring is not None:
                # Show the docstring as it appears indented in the source.
                indented = textwrap.indent(docstring, "    ")
                print(f'    """\n{indented}\n    """')
            print("```\n")

    sys.exit(1)


if __name__ == "__main__":
    main()
