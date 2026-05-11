"""Resource guard registration for the sbx CLI.

Discovered via the resource_guards entry point group declared in
mngr_sbx's pyproject.toml.
"""

from imbue.resource_guards.resource_guards import register_resource_guard


def register_sbx_guard() -> None:
    """Register the sbx CLI binary guard."""
    register_resource_guard("sbx")
