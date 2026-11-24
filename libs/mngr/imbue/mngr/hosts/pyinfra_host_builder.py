"""Build the pyinfra hosts mngr executes commands through."""

from collections.abc import Mapping
from typing import Any
from typing import Final

import pyinfra.api.connectors
from loguru import logger
from pyinfra.api import Host as PyinfraHost
from pyinfra.api import State as PyinfraState
from pyinfra.api.inventory import Inventory

from imbue.mngr.errors import PyinfraConnectorUnavailableError
from imbue.mngr.utils.pyinfra_install import take_pyinfra_install_snapshot

# pyinfra rediscovers its connectors on every Inventory construction, reading
# entry_points.txt out of every installed distribution, and mngr builds an Inventory per
# host. Keeping the first read that finds them stops the scans multiplying with the hosts
# a command touches, and lets a long-running process whose venv is rewritten under it keep
# the connectors it already resolved, whose modules stay imported. A read that finds
# nothing is not kept: it describes a moment (metadata mid-rewrite or shadowed), not the
# install.
#
# entry_points is the seam because pyinfra.api.inventory binds get_all_connectors by name
# at import time, while get_all_connectors looks entry_points up as a module global on
# every call.
_ENTRY_POINTS_BY_QUERY: Final[dict[tuple[tuple[str, Any], ...], Any]] = {}
_read_entry_points_from_disk = pyinfra.api.connectors.entry_points


def _cached_entry_points(**params: Any) -> Any:
    query = tuple(sorted(params.items()))
    cached_entry_points = _ENTRY_POINTS_BY_QUERY.get(query)
    if cached_entry_points is not None:
        return cached_entry_points
    entry_points = _read_entry_points_from_disk(**params)
    if entry_points:
        _ENTRY_POINTS_BY_QUERY[query] = entry_points
    return entry_points


pyinfra.api.connectors.entry_points = _cached_entry_points  # ty: ignore[invalid-assignment]


def build_pyinfra_host(name: str, host_data: Mapping[str, Any] | None = None) -> PyinfraHost:
    """Build an initialized pyinfra host, alone in an inventory of its own.

    A name starting with ``@`` selects the matching pyinfra connector (``@local`` ->
    LocalConnector); any other name executes over SSH. Raises
    PyinfraConnectorUnavailableError when pyinfra cannot find that connector.
    """
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
