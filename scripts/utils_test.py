import pytest

from scripts.utils import PackageInfo
from scripts.utils import _iter_all_dependency_strings
from scripts.utils import _iter_runtime_dependency_strings
from scripts.utils import _topologically_sort
from scripts.utils import parse_exact_pin


@pytest.mark.parametrize(
    ("dep_str", "expected"),
    [
        ("imbue-mngr==0.2.10", "0.2.10"),
        ("imbue-mngr == 0.2.10", "0.2.10"),
        ("imbue-mngr>=0.2.0", None),
        ("imbue-mngr", None),
        # The version stops at a comma (additional constraint) or semicolon (marker).
        ("imbue-mngr==0.2.10, <0.3", "0.2.10"),
        ("imbue-mngr==0.2.10 ; python_version < '3.12'", "0.2.10"),
        # A `!=` exclusion is not a `==` pin.
        ("imbue-mngr!=0.2.9", None),
    ],
)
def test_parse_exact_pin(dep_str: str, expected: str | None) -> None:
    assert parse_exact_pin(dep_str) == expected


def test_iter_runtime_dependency_strings_covers_deps_and_extras() -> None:
    data = {
        "project": {
            "dependencies": ["imbue-mngr==1.0", "click"],
            "optional-dependencies": {"extra": ["httpx", "imbue-common==2.0"]},
        },
        "dependency-groups": {"dev": ["pytest", "imbue-mngr-modal==3.0"]},
    }
    # Runtime view excludes dependency-groups (they don't ship in the wheel).
    assert list(_iter_runtime_dependency_strings(data)) == ["imbue-mngr==1.0", "click", "httpx", "imbue-common==2.0"]


def test_iter_all_dependency_strings_includes_groups_and_skips_include_group() -> None:
    data = {
        "project": {"dependencies": ["imbue-mngr==1.0"]},
        # PEP 735 groups may contain `{include-group = ...}` tables, which are not deps.
        "dependency-groups": {"dev": ["pytest", {"include-group": "test"}, "imbue-mngr-modal==3.0"]},
    }
    assert list(_iter_all_dependency_strings(data)) == ["imbue-mngr==1.0", "pytest", "imbue-mngr-modal==3.0"]


def _pkg(name: str, *deps: str) -> PackageInfo:
    return PackageInfo(dir_name=name.replace("-", "_"), pypi_name=name, internal_deps=tuple(deps))


def test_topologically_sort_orders_dependencies_before_dependents() -> None:
    # base <- mid <- top, declared out of order.
    packages = [_pkg("top", "mid"), _pkg("base"), _pkg("mid", "base")]
    order = [pkg.pypi_name for pkg in _topologically_sort(packages)]
    assert order.index("base") < order.index("mid") < order.index("top")


def test_topologically_sort_is_deterministic() -> None:
    packages = [_pkg("b", "a"), _pkg("a"), _pkg("c", "a")]
    # Ties (b and c both depend only on a) break alphabetically.
    assert [pkg.pypi_name for pkg in _topologically_sort(packages)] == ["a", "b", "c"]


def test_topologically_sort_detects_cycle() -> None:
    packages = [_pkg("a", "b"), _pkg("b", "a")]
    with pytest.raises(ValueError, match="Cycle detected"):
        _topologically_sort(packages)
