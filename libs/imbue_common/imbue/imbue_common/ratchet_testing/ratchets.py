import ast
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Final

from importlinter.application.use_cases import create_report
from importlinter.application.use_cases import read_user_options
from importlinter.configuration import configure
from importlinter.contracts.layers import LayersContract
from importlinter.domain.contract import registry as contract_registry

from imbue.imbue_common.pure import pure
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import LineNumber
from imbue.imbue_common.ratchet_testing.core import RatchetMatchChunk
from imbue.imbue_common.ratchet_testing.core import _get_ast_nodes_by_type
from imbue.imbue_common.ratchet_testing.core import _get_non_ignored_files_with_extension

TEST_FILE_PATTERNS: Final[tuple[str, ...]] = ("*_test.py", "test_*.py")


def find_if_elif_without_else(
    source_dir: Path,
    excluded_path_patterns: tuple[str, ...] = (),
) -> tuple[RatchetMatchChunk, ...]:
    """Find all if/elif chains without else clauses using AST analysis."""
    file_paths = _get_non_ignored_files_with_extension(source_dir, FileExtension(".py"), excluded_path_patterns)
    chunks: list[RatchetMatchChunk] = []

    for file_path in file_paths:
        nodes_by_type = _get_ast_nodes_by_type(file_path)
        if_nodes = nodes_by_type.get(ast.If, [])

        visited_if_nodes: set[int] = set()

        for node in if_nodes:
            assert isinstance(node, ast.If)
            if id(node) not in visited_if_nodes and _has_elif_without_else(node):
                _mark_if_chain_as_visited(node, visited_if_nodes)

                start_line = LineNumber(node.lineno)
                end_line = LineNumber(_get_if_chain_end_line(node))

                chunk = RatchetMatchChunk(
                    file_path=file_path,
                    matched_content=f"if/elif chain at line {start_line}",
                    start_line=start_line,
                    end_line=end_line,
                )
                chunks.append(chunk)

    sorted_chunks = sorted(chunks, key=lambda c: (str(c.file_path), c.start_line))
    return tuple(sorted_chunks)


def _mark_if_chain_as_visited(if_node: ast.If, visited: set[int]) -> None:
    """Mark all If nodes in an if/elif chain as visited."""
    visited.add(id(if_node))
    current = if_node
    while current.orelse:
        first_in_orelse = current.orelse[0]
        if isinstance(first_in_orelse, ast.If):
            visited.add(id(first_in_orelse))
            current = first_in_orelse
        else:
            break


@pure
def _has_elif_without_else(if_node: ast.If) -> bool:
    """Check if an If node has elif but no else clause."""
    if not if_node.orelse:
        return False

    first_orelse = if_node.orelse[0]

    if isinstance(first_orelse, ast.If):
        current = if_node
        while current.orelse:
            first_in_orelse = current.orelse[0]
            if isinstance(first_in_orelse, ast.If):
                current = first_in_orelse
            else:
                return False
        return True

    return False


@pure
def _get_if_chain_end_line(if_node: ast.If) -> int:
    """Get the last line number of an if/elif chain."""
    current = if_node
    while current.orelse:
        first_in_orelse = current.orelse[0]
        if isinstance(first_in_orelse, ast.If):
            current = first_in_orelse
        else:
            break

    if hasattr(current, "end_lineno") and current.end_lineno is not None:
        return current.end_lineno

    return current.lineno


@pure
def _is_test_file(file_path: Path) -> bool:
    """Check if a file is a test file."""
    return file_path.name.endswith("_test.py") or file_path.name.startswith("test_")


def _is_exception_or_error_class(
    class_name: str,
    class_bases: dict[str, list[str]],
    visited: set[str] | None = None,
) -> bool:
    """Check if a class is or inherits from an Exception or Error class.

    Recursively checks the inheritance chain within the same file.
    """
    if visited is None:
        visited = set()

    # Avoid infinite recursion
    if class_name in visited:
        return False
    visited.add(class_name)

    # Check if the class name itself ends with Exception or Error
    if class_name.endswith("Exception") or class_name.endswith("Error"):
        return True

    # Recursively check base classes
    if class_name in class_bases:
        for base in class_bases[class_name]:
            if _is_exception_or_error_class(base, class_bases, visited):
                return True

    return False


def find_init_methods_in_non_exception_classes(
    source_dir: Path,
    excluded_path_patterns: tuple[str, ...] = (),
) -> tuple[RatchetMatchChunk, ...]:
    """Find __init__ method definitions in non-Exception/Error classes, excluding test files.

    Most classes should use Pydantic models which don't need __init__ methods.
    Only Exception/Error classes should define __init__ since they can't use Pydantic.
    """
    file_paths = _get_non_ignored_files_with_extension(
        source_dir, FileExtension(".py"), TEST_FILE_PATTERNS + excluded_path_patterns
    )
    chunks: list[RatchetMatchChunk] = []

    for file_path in file_paths:
        nodes_by_type = _get_ast_nodes_by_type(file_path)
        class_def_nodes = nodes_by_type.get(ast.ClassDef, [])

        # Build a map of class names to their base classes
        class_bases: dict[str, list[str]] = {}
        class_nodes: dict[str, ast.ClassDef] = {}

        for node in class_def_nodes:
            assert isinstance(node, ast.ClassDef)
            bases = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    # Handle cases like module.ClassName
                    bases.append(base.attr)
            class_bases[node.name] = bases
            class_nodes[node.name] = node

        # Check each class for __init__ methods
        for class_name, class_node in class_nodes.items():
            # Check if this class has an __init__ method
            for item in class_node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    # Found an __init__ method
                    # Check if this class is an Exception/Error class
                    if not _is_exception_or_error_class(class_name, class_bases):
                        start_line = LineNumber(item.lineno)
                        end_line = LineNumber(item.end_lineno if item.end_lineno else item.lineno)

                        chunk = RatchetMatchChunk(
                            file_path=file_path,
                            matched_content=f"__init__ method in non-Exception/Error class '{class_name}'",
                            start_line=start_line,
                            end_line=end_line,
                        )
                        chunks.append(chunk)

    sorted_chunks = sorted(chunks, key=lambda c: (str(c.file_path), c.start_line))
    return tuple(sorted_chunks)


@pure
def _has_functools_wraps_decorator(func_node: ast.FunctionDef) -> bool:
    """Check if a function is decorated with @functools.wraps or @wraps.

    This is a standard pattern for creating decorators and should not be flagged
    as an inline function.
    """
    for decorator in func_node.decorator_list:
        # Check for @functools.wraps(...) or @wraps(...)
        if isinstance(decorator, ast.Call):
            func = decorator.func
            # Handle @wraps(...)
            if isinstance(func, ast.Name) and func.id == "wraps":
                return True
            # Handle @functools.wraps(...)
            if isinstance(func, ast.Attribute):
                if func.attr == "wraps" and isinstance(func.value, ast.Name) and func.value.id == "functools":
                    return True

    return False


def find_inline_functions(
    source_dir: Path,
    excluded_path_patterns: tuple[str, ...] = (),
) -> tuple[RatchetMatchChunk, ...]:
    """Find functions defined inside other functions using AST analysis, excluding test files.

    Excludes decorator wrapper functions that use @functools.wraps, as these are
    a standard pattern for implementing decorators.
    """
    file_paths = _get_non_ignored_files_with_extension(
        source_dir, FileExtension(".py"), TEST_FILE_PATTERNS + excluded_path_patterns
    )
    chunks: list[RatchetMatchChunk] = []

    for file_path in file_paths:
        nodes_by_type = _get_ast_nodes_by_type(file_path)
        func_def_nodes = nodes_by_type.get(ast.FunctionDef, [])

        for node in func_def_nodes:
            assert isinstance(node, ast.FunctionDef)
            # Walk within each FunctionDef to find nested functions
            for inner_node in ast.walk(node):
                if inner_node is not node and isinstance(inner_node, ast.FunctionDef):
                    # Skip decorator wrapper functions that use @functools.wraps
                    if _has_functools_wraps_decorator(inner_node):
                        continue

                    start_line = LineNumber(inner_node.lineno)
                    end_line = LineNumber(inner_node.end_lineno if inner_node.end_lineno else inner_node.lineno)

                    chunk = RatchetMatchChunk(
                        file_path=file_path,
                        matched_content=f"inline function '{inner_node.name}' at line {start_line}",
                        start_line=start_line,
                        end_line=end_line,
                    )
                    chunks.append(chunk)

    sorted_chunks = sorted(chunks, key=lambda c: (str(c.file_path), c.start_line))
    return tuple(sorted_chunks)


def find_underscore_imports(
    source_dir: Path,
    excluded_path_patterns: tuple[str, ...] = (),
) -> tuple[RatchetMatchChunk, ...]:
    """Find imports of underscore-prefixed names using AST analysis, excluding test files."""
    file_paths = _get_non_ignored_files_with_extension(
        source_dir, FileExtension(".py"), TEST_FILE_PATTERNS + excluded_path_patterns
    )
    chunks: list[RatchetMatchChunk] = []

    for file_path in file_paths:
        nodes_by_type = _get_ast_nodes_by_type(file_path)
        import_from_nodes = nodes_by_type.get(ast.ImportFrom, [])
        import_nodes = nodes_by_type.get(ast.Import, [])

        for node in import_from_nodes:
            assert isinstance(node, ast.ImportFrom)
            underscore_names: list[str] = []
            if node.names:
                for alias in node.names:
                    if alias.name.startswith("_"):
                        underscore_names.append(alias.name)

            if underscore_names:
                start_line = LineNumber(node.lineno)
                end_line = LineNumber(node.end_lineno if node.end_lineno else node.lineno)

                chunk = RatchetMatchChunk(
                    file_path=file_path,
                    matched_content=f"import of underscore-prefixed name(s): {', '.join(underscore_names)}",
                    start_line=start_line,
                    end_line=end_line,
                )
                chunks.append(chunk)

        for node in import_nodes:
            assert isinstance(node, ast.Import)
            underscore_names_import: list[str] = []
            for alias in node.names:
                if alias.name.startswith("_"):
                    underscore_names_import.append(alias.name)

            if underscore_names_import:
                start_line = LineNumber(node.lineno)
                end_line = LineNumber(node.end_lineno if node.end_lineno else node.lineno)

                chunk = RatchetMatchChunk(
                    file_path=file_path,
                    matched_content=f"import of underscore-prefixed name(s): {', '.join(underscore_names_import)}",
                    start_line=start_line,
                    end_line=end_line,
                )
                chunks.append(chunk)

    sorted_chunks = sorted(chunks, key=lambda c: (str(c.file_path), c.start_line))
    return tuple(sorted_chunks)


def find_cast_usages(
    source_dir: Path,
    excluded_path_patterns: tuple[str, ...] = (),
) -> tuple[RatchetMatchChunk, ...]:
    """Find usages of cast() from typing in non-test files using AST analysis.

    This function finds all calls to cast() in Python files, excluding test files.
    cast() usage should be avoided in favor of type: ignore comments when there's
    no other way to satisfy the type checker.
    """
    file_paths = _get_non_ignored_files_with_extension(
        source_dir, FileExtension(".py"), TEST_FILE_PATTERNS + excluded_path_patterns
    )
    chunks: list[RatchetMatchChunk] = []

    for file_path in file_paths:
        nodes_by_type = _get_ast_nodes_by_type(file_path)
        import_from_nodes = nodes_by_type.get(ast.ImportFrom, [])

        # Check if 'cast' is imported from typing
        has_cast_import = False
        cast_alias = "cast"
        for node in import_from_nodes:
            assert isinstance(node, ast.ImportFrom)
            if node.module == "typing":
                for alias in node.names:
                    if alias.name == "cast":
                        has_cast_import = True
                        cast_alias = alias.asname if alias.asname else "cast"
                        break

        if not has_cast_import:
            continue

        # Find all calls to cast()
        call_nodes = nodes_by_type.get(ast.Call, [])
        for node in call_nodes:
            assert isinstance(node, ast.Call)
            if isinstance(node.func, ast.Name) and node.func.id == cast_alias:
                start_line = LineNumber(node.lineno)
                end_line = LineNumber(node.end_lineno if node.end_lineno else node.lineno)

                chunk = RatchetMatchChunk(
                    file_path=file_path,
                    matched_content=f"cast() usage at line {start_line}",
                    start_line=start_line,
                    end_line=end_line,
                )
                chunks.append(chunk)

    sorted_chunks = sorted(chunks, key=lambda c: (str(c.file_path), c.start_line))
    return tuple(sorted_chunks)


def find_assert_isinstance_usages(
    source_dir: Path,
    excluded_path_patterns: tuple[str, ...] = (),
) -> tuple[RatchetMatchChunk, ...]:
    """Find usages of 'assert isinstance(...)' in non-test files using AST analysis.

    This function finds all assert statements containing isinstance() calls in Python
    files, excluding test files. 'assert isinstance()' usage should be replaced with
    match constructs that exhaustively handle all cases using
    'case _ as unreachable: assert_never(unreachable)'.
    """
    file_paths = _get_non_ignored_files_with_extension(
        source_dir, FileExtension(".py"), TEST_FILE_PATTERNS + excluded_path_patterns
    )
    chunks: list[RatchetMatchChunk] = []

    for file_path in file_paths:
        nodes_by_type = _get_ast_nodes_by_type(file_path)
        assert_nodes = nodes_by_type.get(ast.Assert, [])

        # Find all 'assert isinstance(...)' statements
        for node in assert_nodes:
            assert isinstance(node, ast.Assert)
            # Check if the test is an isinstance() call
            if isinstance(node.test, ast.Call):
                if isinstance(node.test.func, ast.Name) and node.test.func.id == "isinstance":
                    start_line = LineNumber(node.lineno)
                    end_line = LineNumber(node.end_lineno if node.end_lineno else node.lineno)

                    chunk = RatchetMatchChunk(
                        file_path=file_path,
                        matched_content=f"assert isinstance() at line {start_line}",
                        start_line=start_line,
                        end_line=end_line,
                    )
                    chunks.append(chunk)

    sorted_chunks = sorted(chunks, key=lambda c: (str(c.file_path), c.start_line))
    return tuple(sorted_chunks)


def check_no_type_errors(project_root: Path) -> None:
    """Run the type checker (ty) and raise AssertionError if any type errors are found."""
    result = subprocess.run(
        ["uv", "run", "ty", "check"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error_lines = [
            line for line in result.stdout.splitlines() if line.startswith("error[") or "error:" in line.lower()
        ]
        error_count = len(error_lines)

        failure_message = [
            f"Type checker found {error_count} error(s):",
            "",
            "Full type checker output:",
            "=" * 80,
            result.stdout,
            "=" * 80,
        ]

        raise AssertionError("\n".join(failure_message))


def check_no_ruff_errors(project_root: Path) -> None:
    """Run the ruff linter and raise AssertionError if any linting errors are found."""
    result = subprocess.run(
        ["uv", "run", "ruff", "check"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        failure_message = [
            "Ruff linter found errors:",
            "",
            "Full ruff output:",
            "=" * 80,
            result.stdout,
            "=" * 80,
        ]

        raise AssertionError("\n".join(failure_message))


_TEST_MODULE_GLOBS: Final[tuple[str, ...]] = (
    "*_test",
    "test_*",
    "conftest",
    "testing",
    "plugin_testing",
)


def _is_test_module(module_path: str) -> bool:
    """Check if an import-linter module path refers to a test module."""
    last_segment = module_path.rsplit(".", 1)[-1]
    return any(fnmatch(last_segment, pattern) for pattern in _TEST_MODULE_GLOBS)


def check_no_import_lint_errors(project_root: Path, contract_name: str = "mng layers contract") -> None:
    """Run import-linter and raise AssertionError if any production code violations are found.

    Uses import-linter's Python API to get structured results, then filters
    out violations where every importer in the chain is a test module.
    Only production code violations cause failure.

    Only checks the contract matching contract_name; other contracts are skipped.
    """
    configure()
    contract_registry.register(LayersContract, name="layers")
    config_filename = str(project_root / "pyproject.toml")
    user_options = read_user_options(config_filename=config_filename)
    # Filter to only the requested contract to avoid failures from unrelated
    # contracts whose modules may not be present in this worktree.
    user_options.contracts_options = [opt for opt in user_options.contracts_options if opt["name"] == contract_name]
    report = create_report(user_options)

    production_violations: list[str] = []
    for contract, check in report.get_contracts_and_checks():
        if check.kept:
            continue
        for dep in check.metadata.get("invalid_dependencies", []):
            for route in dep["routes"]:
                first_link = route["chain"][0]
                importer = first_link["importer"]
                if not _is_test_module(importer):
                    imported = first_link["imported"]
                    production_violations.append(f"  {importer} -> {imported}")

    if production_violations:
        failure_message = [
            f"import-linter found {len(production_violations)} production code layer violation(s):",
            "",
            *production_violations,
        ]
        raise AssertionError("\n".join(failure_message))


def find_bash_scripts_without_strict_mode(cwd: Path) -> list[str]:
    """Find bash scripts missing 'set -euo pipefail' in the git repo containing cwd."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    repo_root = Path(result.stdout.strip())

    ls_result = subprocess.run(
        ["git", "ls-files", "*.sh"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )

    sh_files = [repo_root / line.strip() for line in ls_result.stdout.splitlines() if line.strip()]

    strict_mode_pattern = re.compile(r"set\s+-(?=[^ ]*e)(?=[^ ]*u)(?=[^ ]*o)[euo]+\s+pipefail")

    violations: list[str] = []
    for sh_file in sh_files:
        content = sh_file.read_text()
        if not strict_mode_pattern.search(content):
            violations.append(str(sh_file))

    return violations


def find_code_in_init_files(
    source_dir: Path,
    allowed_root_init_lines: set[str] | None = None,
) -> list[str]:
    """Find __init__.py files that contain code.

    The root __init__.py (directly under source_dir) may optionally contain
    specific allowed lines (e.g., pluggy hookimpl marker). All other __init__.py
    files must be empty.
    """
    root_init = source_dir / "__init__.py"
    init_files = list(source_dir.rglob("__init__.py"))

    violations: list[str] = []
    for init_file in init_files:
        content = init_file.read_text().strip()

        if init_file == root_init and allowed_root_init_lines is not None:
            actual_lines = {line.strip() for line in content.splitlines() if line.strip()}
            disallowed = actual_lines - allowed_root_init_lines
            if disallowed:
                violations.append(f"{init_file}: contains disallowed code: {disallowed}")
        else:
            if content:
                violations.append(f"{init_file}: should be empty but contains: {content[:100]}...")

    return violations
