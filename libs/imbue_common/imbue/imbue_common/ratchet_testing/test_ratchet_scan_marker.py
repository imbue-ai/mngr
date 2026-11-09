from pathlib import Path
from typing import Final

import pytest

pytest_plugins = ["pytester"]

# Wires only the setup hook under test: register_conftest_hooks would also take the
# global test lock, which the pytest process running this file already holds.
_INNER_CONFTEST: Final[str] = """
from imbue.imbue_common.conftest_hooks import _pytest_runtest_setup as pytest_runtest_setup


def pytest_configure(config):
    config.addinivalue_line("markers", "ratchet_scan(source_dir): the directory a ratchet test scans")
"""


def _ratchet_module_source(project_dir: Path, is_scan_declared: bool) -> str:
    """A ratchet test module whose one test asserts its check reads no file inside the test body."""
    declaration = "pytestmark = pytest.mark.ratchet_scan(_DIR)" if is_scan_declared else ""
    return f"""
from pathlib import Path

import pytest

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from imbue.imbue_common.ratchet_testing.core import _get_all_text_files_with_extension
from imbue.imbue_common.ratchet_testing.core import _get_ast_nodes_by_type
from imbue.imbue_common.ratchet_testing.core import _parse_file_ast
from imbue.imbue_common.ratchet_testing.core import _read_file_contents

_DIR = Path({str(project_dir)!r})

{declaration}


def _scan_cache_miss_count() -> int:
    return sum(
        cache.cache_info().misses
        for cache in (_get_all_text_files_with_extension, _read_file_contents, _parse_file_ast, _get_ast_nodes_by_type)
    )


def test_prevent_if_elif_without_else() -> None:
    miss_count_before_check = _scan_cache_miss_count()
    rc.check_if_elif_without_else(_DIR, 1_000_000)
    assert _scan_cache_miss_count() == miss_count_before_check
"""


def _run_first_ratchet_test_in_a_fresh_process(
    pytester: pytest.Pytester,
    git_repo: Path,
    is_scan_declared: bool,
) -> pytest.RunResult:
    project_dir = git_repo / "project"
    project_dir.mkdir()
    (project_dir / "app.py").write_text("def pick(value: int) -> str:\n    if value > 0:\n        return 'positive'\n")
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_ratchets=_ratchet_module_source(project_dir, is_scan_declared))
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:xdist")


def test_the_first_ratchet_test_of_a_module_that_declares_its_scan_reads_no_file_in_its_body(
    pytester: pytest.Pytester,
    git_repo: Path,
) -> None:
    result = _run_first_ratchet_test_in_a_fresh_process(pytester, git_repo, is_scan_declared=True)

    result.assert_outcomes(passed=1)


def test_the_first_ratchet_test_of_a_module_without_the_declaration_reads_its_scan_in_its_body(
    pytester: pytest.Pytester,
    git_repo: Path,
) -> None:
    result = _run_first_ratchet_test_in_a_fresh_process(pytester, git_repo, is_scan_declared=False)

    result.assert_outcomes(failed=1)
