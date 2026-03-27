import ast
from pathlib import Path

import pytest
import tomlkit
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RegexRatchetRule
from imbue.imbue_common.ratchet_testing.common_ratchets import check_ratchet_rule_all_files
from imbue.imbue_common.ratchet_testing.core import _get_all_files_with_extension
from imbue.imbue_common.ratchet_testing.ratchets import check_no_import_lint_errors
from imbue.imbue_common.ratchet_testing.ratchets import find_bash_scripts_without_strict_mode

_REPO_ROOT = Path(__file__).parent

# Projects that are excluded from ratchet requirements (scheduled for deletion)
_EXCLUDED_PROJECTS: frozenset[str] = frozenset({"flexmux", "claude_web_view", "sculptor_web"})

_SELF_EXCLUSION: tuple[str, ...] = ("test_meta_ratchets.py",)
_MIGRATION_SCRIPT_EXCLUSION: tuple[str, ...] = ("migrate_code_mng_to_mngr.sh", "migrate_state_mng_to_mngr.sh")

pytestmark = pytest.mark.xdist_group(name="meta_ratchets")


def _get_all_project_dirs() -> list[Path]:
    """Return all project directories (libs/* and apps/*) that are not excluded."""
    project_dirs: list[Path] = []
    for parent in [_REPO_ROOT / "libs", _REPO_ROOT / "apps"]:
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and (child / "pyproject.toml").exists() and child.name not in _EXCLUDED_PROJECTS:
                project_dirs.append(child)
    return project_dirs


def _find_test_ratchets_file(project_dir: Path) -> Path | None:
    """Find the test_ratchets.py file within a project directory."""
    matches = list(project_dir.rglob("test_ratchets.py"))
    if len(matches) == 1:
        return matches[0]
    elif len(matches) == 0:
        return None
    else:
        raise AssertionError(
            f"Found multiple test_ratchets.py files in {project_dir.name}: "
            + ", ".join(str(m.relative_to(project_dir)) for m in matches)
        )


def _extract_test_function_names(file_path: Path) -> frozenset[str]:
    """Extract all test function names (starting with 'test_') from a Python file using AST."""
    tree = ast.parse(file_path.read_text())
    return frozenset(
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


# --- Meta: ensure every project has ratchets ---


def test_every_project_has_test_ratchets_file() -> None:
    """Ensure each project (except excluded ones) has a test_ratchets.py file."""
    missing: list[str] = []
    for project_dir in _get_all_project_dirs():
        if _find_test_ratchets_file(project_dir) is None:
            missing.append(project_dir.name)
    assert len(missing) == 0, "The following projects are missing a test_ratchets.py file:\n" + "\n".join(
        f"  - {m}" for m in missing
    )


def test_all_test_ratchets_files_have_same_tests() -> None:
    """Ensure all test_ratchets.py files define precisely the same set of test functions."""
    test_names_by_project: dict[str, frozenset[str]] = {}
    for project_dir in _get_all_project_dirs():
        ratchet_file = _find_test_ratchets_file(project_dir)
        if ratchet_file is None:
            continue
        test_names_by_project[project_dir.name] = _extract_test_function_names(ratchet_file)

    if not test_names_by_project:
        raise AssertionError("No test_ratchets.py files found")

    # Use the first project's test names as the reference
    project_names = sorted(test_names_by_project.keys())
    reference_project = project_names[0]
    reference_tests = test_names_by_project[reference_project]

    mismatches: list[str] = []
    for project_name in project_names[1:]:
        project_tests = test_names_by_project[project_name]
        missing_tests = reference_tests - project_tests
        extra_tests = project_tests - reference_tests
        if missing_tests or extra_tests:
            parts = [f"  {project_name} (vs {reference_project}):"]
            if missing_tests:
                parts.append(f"    missing: {sorted(missing_tests)}")
            if extra_tests:
                parts.append(f"    extra:   {sorted(extra_tests)}")
            mismatches.append("\n".join(parts))

    assert len(mismatches) == 0, "test_ratchets.py files have different test functions:\n" + "\n".join(mismatches)


# --- Repo-wide ratchets (run once, not per-project) ---


def test_no_import_layer_violations() -> None:
    """Ensure production code has zero import layer violations."""
    check_no_import_lint_errors(_REPO_ROOT)


def test_prevent_bash_without_strict_mode() -> None:
    """Ensure all bash scripts in the repo use 'set -euo pipefail' for strict error handling."""
    violations = find_bash_scripts_without_strict_mode(_REPO_ROOT)
    assert len(violations) <= snapshot(0), "Bash scripts missing 'set -euo pipefail':\n" + "\n".join(
        f"  - {v}" for v in violations
    )


_PREVENT_OLD_MNG_NAME = RegexRatchetRule(
    rule_name="'mng' (without 'r') occurrences",
    rule_description="The old 'mng' name should not be reintroduced. Use 'mngr' instead.",
    pattern_string=r"mng(?!r)",
)


def test_prevent_old_mng_name_in_file_contents() -> None:
    """Ensure the old 'mng' name (not followed by 'r') is not reintroduced in file contents."""
    exclusions = _SELF_EXCLUSION + _MIGRATION_SCRIPT_EXCLUSION
    chunks = check_ratchet_rule_all_files(_PREVENT_OLD_MNG_NAME, _REPO_ROOT, exclusions)
    assert len(chunks) <= snapshot(0), _PREVENT_OLD_MNG_NAME.format_failure(chunks)


def test_prevent_old_mng_name_in_file_paths() -> None:
    """Ensure the old 'mng' name (not followed by 'r') is not reintroduced in file paths."""
    import re

    mng_not_mngr = re.compile(r"mng(?!r)")
    all_paths = _get_all_files_with_extension(_REPO_ROOT, None)
    mng_paths = [
        p
        for p in all_paths
        if mng_not_mngr.search(str(p.relative_to(_REPO_ROOT)))
        and not any(excl in p.name for excl in _MIGRATION_SCRIPT_EXCLUSION)
    ]
    assert len(mng_paths) <= snapshot(0), (
        f"Found {len(mng_paths)} file paths containing 'mng' (not 'mngr'):\n"
        + "\n".join(f"  {p.relative_to(_REPO_ROOT)}" for p in mng_paths)
    )


def test_every_project_has_pypi_readme() -> None:
    """Ensure each project's pyproject.toml has a readme field pointing to an existing file.

    Every published package should have a README so that PyPI displays useful
    information. This checks two things:
    1. The [project] section contains a `readme` key
    2. The referenced file exists on disk
    """
    missing_field: list[str] = []
    missing_file: list[str] = []

    for project_dir in _get_all_project_dirs():
        pyproject_path = project_dir / "pyproject.toml"
        pyproject = tomlkit.parse(pyproject_path.read_text())
        project_section = pyproject.get("project", {})

        readme_value = project_section.get("readme")
        if not isinstance(readme_value, str):
            missing_field.append(project_dir.name)
            continue

        if not (project_dir / readme_value).exists():
            missing_file.append(f"{project_dir.name} (references {readme_value})")

    errors: list[str] = []
    if missing_field:
        errors.append("Missing readme field in [project]: " + ", ".join(missing_field))
    if missing_file:
        errors.append("readme file does not exist: " + ", ".join(missing_file))

    assert len(errors) == 0, "Projects with PyPI readme issues:\n" + "\n".join(f"  - {e}" for e in errors)


def _has_test_files(project_dir: Path) -> bool:
    """Return True if the project contains any test files."""
    for pattern in ["*_test.py", "test_*.py"]:
        if list(project_dir.rglob(pattern)):
            return True
    return False


def test_every_project_with_tests_has_coverage_config() -> None:
    """Ensure each project with tests has pytest coverage configuration in its pyproject.toml.

    Every project that contains test files must have:
    1. A [tool.pytest.ini_options] section with a --cov flag scoped to the project's package
    2. A [tool.coverage.run] section with omit patterns for test files
    """
    missing_pytest: list[str] = []
    missing_cov_flag: list[str] = []
    missing_coverage_run: list[str] = []

    for project_dir in _get_all_project_dirs():
        if not _has_test_files(project_dir):
            continue

        pyproject_path = project_dir / "pyproject.toml"
        pyproject = tomlkit.parse(pyproject_path.read_text())

        tool = pyproject.get("tool", {})

        # Check for [tool.pytest.ini_options]
        pytest_opts = tool.get("pytest", {}).get("ini_options", {})
        if not pytest_opts:
            missing_pytest.append(project_dir.name)
            continue

        # Check that addopts contains a --cov flag
        addopts = pytest_opts.get("addopts", [])
        has_cov_flag = any(str(opt).startswith("--cov=") for opt in addopts)
        if not has_cov_flag:
            missing_cov_flag.append(project_dir.name)

        # Check for [tool.coverage.run]
        coverage_run = tool.get("coverage", {}).get("run", {})
        if not coverage_run:
            missing_coverage_run.append(project_dir.name)

    errors: list[str] = []
    if missing_pytest:
        errors.append("Missing [tool.pytest.ini_options]: " + ", ".join(missing_pytest))
    if missing_cov_flag:
        errors.append("Missing --cov= in addopts: " + ", ".join(missing_cov_flag))
    if missing_coverage_run:
        errors.append("Missing [tool.coverage.run]: " + ", ".join(missing_coverage_run))

    assert len(errors) == 0, "Projects with tests are missing coverage configuration:\n" + "\n".join(
        f"  - {e}" for e in errors
    )
