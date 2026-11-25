"""Build the pyinfra hosts mngr executes commands through."""

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import pyinfra.api.connectors
from loguru import logger
from pyinfra.api import Host as PyinfraHost
from pyinfra.api import State as PyinfraState
from pyinfra.api.inventory import Inventory

from imbue.mngr.errors import PyinfraConnectorUnavailableError
from imbue.mngr.utils.pyinfra_install import take_pyinfra_install_snapshot

# pyinfra rediscovers its connectors on every Inventory by reading entry_points.txt out of
# every installed distribution, and mngr builds an Inventory per host. Memoizing the lookup
# stops those scans multiplying with the hosts a command touches, and lets a long-running
# process whose venv is rewritten under it keep the connectors it already resolved.
_memoized_pyinfra_entry_points = lru_cache(maxsize=None)(pyinfra.api.connectors.entry_points)


def _install_memoized_pyinfra_entry_points() -> None:
    # entry_points is the seam because pyinfra.api.inventory binds get_all_connectors by name
    # at import time, while get_all_connectors looks entry_points up on every call.
    pyinfra.api.connectors.entry_points = _memoized_pyinfra_entry_points  # ty: ignore[invalid-assignment]


def build_pyinfra_host(name: str, host_data: Mapping[str, Any] | None = None) -> PyinfraHost:
    """Build an initialized pyinfra host, alone in an inventory of its own.

    A name starting with ``@`` selects the matching pyinfra connector (``@local`` ->
    LocalConnector); any other name executes over SSH. Raises
    PyinfraConnectorUnavailableError when pyinfra cannot find that connector.
    """
    _install_memoized_pyinfra_entry_points()
    named_host: str | tuple[str, dict[str, Any]] = name if host_data is None else (name, dict(host_data))
    try:
        inventory = Inventory(([named_host], {}))
    except KeyError as e:
        snapshot = take_pyinfra_install_snapshot()
        logger.error("pyinfra has no {} connector; pyinfra install as seen now: {}", e, snapshot)
        raise PyinfraConnectorUnavailableError(f"pyinfra has no {e} connector to reach {name} with") from e
    pyinfra_host = inventory.get_host(name)
    pyinfra_host.init(PyinfraState(inventory=inventory))
    return pyinfra_host
