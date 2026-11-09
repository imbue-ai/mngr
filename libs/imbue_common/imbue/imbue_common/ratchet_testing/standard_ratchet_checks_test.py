import inspect
from pathlib import Path
from types import FunctionType
from typing import Final

import pytest

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from imbue.imbue_common.ratchet_testing.core import _get_ast_nodes_by_type
from imbue.imbue_common.ratchet_testing.core import _parse_file_ast

_STANDARD_CHECKS: Final[tuple[FunctionType, ...]] = tuple(
    check
    for name, check in inspect.getmembers(rc, inspect.isfunction)
    if name.startswith("check_") and check.__module__ == rc.__name__
)

# High enough that no check fails on the sample project's violations.
_UNREACHABLE_MAX_COUNT: Final[int] = 1_000_000


def _ast_cache_miss_count() -> int:
    return _parse_file_ast.cache_info().misses + _get_ast_nodes_by_type.cache_info().misses


@pytest.mark.parametrize("check", _STANDARD_CHECKS, ids=lambda check: check.__name__)
def test_every_check_that_parses_python_files_has_its_asts_preloaded(check: FunctionType, git_repo: Path) -> None:
    (git_repo / "app.py").write_text(
        "import os\n\n\nclass Widget:\n    def __init__(self) -> None:\n        self.name = os.sep\n\n\n"
        "def pick(value: int) -> str:\n    if value > 1:\n        return 'big'\n    elif value > 0:\n"
        "        return 'small'\n    return 'none'\n"
    )
    (git_repo / "app_test.py").write_text("def test_pick() -> None:\n    assert True\n")
    test_name = f"test_prevent_{check.__name__.removeprefix('check_')}"
    rc.preload_ratchet_scan_for_test(source_dir=git_repo, test_name=test_name)

    miss_count_before_check = _ast_cache_miss_count()
    check(git_repo, _UNREACHABLE_MAX_COUNT)

    assert _ast_cache_miss_count() == miss_count_before_check
