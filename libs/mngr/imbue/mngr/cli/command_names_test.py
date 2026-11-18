"""Keep ``KNOWN_CONFIG_COMMAND_NAMES`` in exact sync with the source literals.

``command_name`` values are call-site literals with a deliberate convention
(``<group>_<subcommand>`` for group subcommands, group-level buckets for
``config`` / ``plugin``) that cannot be derived from the click tree, so the
registry is maintained by hand. This test statically collects every
``command_name="..."`` keyword argument in the (non-test) ``imbue.mngr`` source
and asserts the registry matches it, so a new/renamed/removed command cannot
silently drift the registry the completion writer relies on.
"""

import ast
from pathlib import Path

import imbue.mngr
from imbue.mngr.cli.command_names import KNOWN_CONFIG_COMMAND_NAMES


def _collect_command_name_literals(package_root: Path) -> set[str]:
    """Return every ``command_name="<literal>"`` call keyword arg under ``package_root``.

    Skips ``*_test.py`` / ``test_*.py`` files (their literals are fixtures, e.g.
    ``command_name="test"``) and ignores non-constant ``command_name=`` arguments
    (the ``on_before_command(command_name=command_name)`` variable pass-throughs),
    so only real command-definition literals are collected.
    """
    literals: set[str] = set()
    for py_file in package_root.rglob("*.py"):
        if py_file.name.endswith("_test.py") or py_file.name.startswith("test_"):
            continue
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "command_name" and isinstance(keyword.value, ast.Constant):
                    if isinstance(keyword.value.value, str):
                        literals.add(keyword.value.value)
    return literals


def test_registry_matches_source_command_name_literals() -> None:
    package_root = Path(imbue.mngr.__file__).parent
    actual = _collect_command_name_literals(package_root)
    registry = set(KNOWN_CONFIG_COMMAND_NAMES)

    missing_from_registry = actual - registry
    stale_in_registry = registry - actual
    assert not missing_from_registry and not stale_in_registry, (
        "KNOWN_CONFIG_COMMAND_NAMES is out of sync with the source. "
        f"Add to the registry: {sorted(missing_from_registry)}. "
        f"Remove from the registry (no longer used): {sorted(stale_in_registry)}."
    )
