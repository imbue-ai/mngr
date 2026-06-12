"""Resource guard registration for the smolvm CLI.

Discovered via the resource_guards entry point group declared in
mngr_smolvm's pyproject.toml.
"""

from imbue.resource_guards.resource_guards import register_resource_guard


def register_smolvm_guard() -> None:
    """Register the smolvm CLI binary guard."""
    register_resource_guard("smolvm")
