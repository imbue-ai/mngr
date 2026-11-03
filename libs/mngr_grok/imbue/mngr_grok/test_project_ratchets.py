import ast
import sys
from pathlib import Path
from typing import Final

import pytest

_RESOURCES_DIR: Final[Path] = Path(__file__).parent / "resources"
_NON_HOST_RESOURCE_MODULE_NAMES: Final[frozenset[str]] = frozenset({"__init__.py", "testing.py"})
# The Python scripts the agent host runs with its own python3.
_HOST_PYTHON_SCRIPT_NAMES: Final[tuple[str, ...]] = tuple(
    sorted(
        path.name
        for path in _RESOURCES_DIR.glob("*.py")
        if not path.name.endswith("_test.py") and path.name not in _NON_HOST_RESOURCE_MODULE_NAMES
    )
)
_OLDEST_HOST_PYTHON_VERSION: Final[tuple[int, int]] = (3, 9)
# Standard-library modules that the oldest host Python does not have.
_STDLIB_MODULES_NEWER_THAN_THE_OLDEST_HOST_PYTHON: Final[frozenset[str]] = frozenset({"tomllib"})


def _imported_root_module_names(tree: ast.Module) -> set[str]:
    root_names: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=aliases):
                root_names.update(alias.name.split(".")[0] for alias in aliases)
            case ast.ImportFrom(module=str() as module_name, level=0):
                root_names.add(module_name.split(".")[0])
            case _:
                pass
    return root_names


@pytest.mark.parametrize("script_name", _HOST_PYTHON_SCRIPT_NAMES)
def test_host_python_script_parses_on_the_oldest_host_python_and_imports_only_its_standard_library(
    script_name: str,
) -> None:
    tree = ast.parse((_RESOURCES_DIR / script_name).read_text(), feature_version=_OLDEST_HOST_PYTHON_VERSION)

    unavailable_module_names = sorted(
        name
        for name in _imported_root_module_names(tree)
        if name not in sys.stdlib_module_names or name in _STDLIB_MODULES_NEWER_THAN_THE_OLDEST_HOST_PYTHON
    )
    assert unavailable_module_names == []
