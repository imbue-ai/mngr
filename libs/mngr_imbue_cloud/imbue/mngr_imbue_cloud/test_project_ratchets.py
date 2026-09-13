"""Project-specific guardrails for the imbue_cloud plugin's wire-parsing discipline.

The connector client parses responses produced by servers that deploy
independently of this (shipped) code, so every parse must go through the
tolerant ``validate_wire`` / ``parse_wire_entries`` entrypoints in
``wire.py`` -- they are typed to accept only WireModel subclasses and add the
drift observability and list semantics the forward-compatibility contract
requires. See ``wire.py`` and the connector's ``wire_compat_test.py``.
"""

import ast
import re
import sys
from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent


def test_connector_client_never_calls_model_validate_directly() -> None:
    """connector/client.py must parse response bodies via validate_wire, never Model.model_validate.

    ``validate_wire`` is typed to accept only WireModel subclasses, so routing
    every parse through it is what guarantees no strict (extra="forbid") model
    can ever validate a connector response body again.
    """
    client_source = (_PACKAGE_DIR / "connector" / "client.py").read_text()
    direct_calls = re.findall(r"\.model_validate\(", client_source)
    assert len(direct_calls) == 0, (
        "connector/client.py calls model_validate directly; parse connector responses through "
        "validate_wire / parse_wire_entries (wire.py) instead, so only WireModel subclasses can "
        "ever validate wire bodies."
    )


# The gen-2 slice script renderers ship into the remote_service_connector's
# Modal container as a source mount (see libs/modal_app_kit/README.md), where
# the only monorepo code is the mounted packages and the only third-party code
# is the connector's pinned image set. An import outside this allowance works
# locally and crashes the connector container at import time.
_GEN2_SCRIPTS_DIR = _PACKAGE_DIR / "slices" / "gen2_scripts"
_GEN2_SCRIPTS_PACKAGE = "imbue.mngr_imbue_cloud.slices.gen2_scripts"
_GEN2_SCRIPTS_ALLOWED_THIRD_PARTY_ROOTS = frozenset({"pydantic", "yaml"})
_GEN2_SCRIPTS_ALLOWED_IMBUE_PACKAGES = (_GEN2_SCRIPTS_PACKAGE, "imbue.imbue_common")
_NON_SHIPPED_FILENAMES = ("conftest.py", "testing.py")


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
        else:
            pass
    return names


def _gen2_scripts_production_modules() -> list[Path]:
    return sorted(
        path
        for path in _GEN2_SCRIPTS_DIR.glob("*.py")
        if not path.name.endswith("_test.py") and path.name not in _NON_SHIPPED_FILENAMES
    )


def test_gen2_scripts_import_only_what_the_connector_container_ships() -> None:
    """Modules under slices/gen2_scripts import only the stdlib, the two allowed third-party roots, imbue_common, and each other."""
    production_modules = _gen2_scripts_production_modules()
    assert len(production_modules) >= 5, "the gen2_scripts subpackage lost its production modules"
    violations: list[str] = []
    for path in production_modules:
        for module_name in sorted(_imported_module_names(path)):
            root = module_name.split(".")[0]
            if root in sys.stdlib_module_names or root in _GEN2_SCRIPTS_ALLOWED_THIRD_PARTY_ROOTS:
                continue
            is_allowed_imbue_module = any(
                module_name == package or module_name.startswith(package + ".")
                for package in _GEN2_SCRIPTS_ALLOWED_IMBUE_PACKAGES
            )
            if not is_allowed_imbue_module:
                violations.append(f"{path.name}: {module_name}")
    assert not violations, (
        "slices/gen2_scripts ships into the connector container, which has none of these: "
        f"{violations} (keep the subpackage on the stdlib, the allowed third-party roots, and imbue_common)"
    )
