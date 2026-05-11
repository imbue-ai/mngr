"""Resource guard registration for the lima CLI.

Discovered via the resource_guards entry point group declared in
mngr_lima's pyproject.toml.
"""

from imbue.resource_guards.resource_guards import register_resource_guard


def register_lima_guard() -> None:
    """Register the lima CLI binary guard."""
    register_resource_guard("lima")
