"""Last-known per-agent service map, persisted across ``mngr forward`` runs.

The resolver's per-agent ``{service -> url}`` map is only populated after the
slow per-agent ``mngr event ... services --follow`` stream connects (measured
~10s cold, longer under spawn contention). Until then ``resolve()`` returns
``None`` and the proxy serves the 503 loading page.

This cache persists that derived map to disk while the plugin runs, so a fresh
run can seed the resolver from it at startup: a restored window then resolves
as soon as discovery supplies membership + SSH info instead of waiting on the
event stream. The live stream still runs and overwrites the seed as soon as it
delivers, so a stale seed self-corrects within one stream connect.

The cache is a single JSON object mapping agent id -> {service_name -> url}.
It lives under the caller-chosen path (the plugin points it at
``$MNGR_HOST_DIR/plugin/forward/``), so staging / production / local minds keep
independent caches automatically.
"""

import json
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import read_json_dict


class ServiceMapCache(FrozenModel):
    """Reads and writes the last-known per-agent service map at ``cache_path``."""

    cache_path: Path = Field(frozen=True, description="JSON file holding the persisted service map")

    def load(self) -> dict[str, dict[str, str]]:
        """Return the persisted ``{agent_id -> {service -> url}}`` map.

        A missing, empty, unreadable, or malformed cache file yields ``{}`` --
        seeding from it is then a no-op and startup behaves exactly as if no
        cache existed. Only well-formed ``str -> {str -> str}`` entries are
        kept; anything else is dropped so a corrupt file can never inject a bad
        route.
        """
        try:
            raw = read_json_dict(self.cache_path)
        except OSError as e:
            logger.warning("Could not read forward service-map cache {} ({}); ignoring.", self.cache_path, e)
            return {}
        return _coerce_service_map(raw)

    def persist(self, services_by_agent: dict[str, dict[str, str]]) -> None:
        """Atomically write the full service map to disk (best effort).

        Called on every mutation of the resolver's service map. A write
        failure is logged and swallowed: the cache is an optimization, and a
        failed persist must never break forwarding.
        """
        try:
            atomic_write(self.cache_path, json.dumps(services_by_agent, sort_keys=True))
        except OSError as e:
            logger.warning("Could not persist forward service-map cache {} ({}); continuing.", self.cache_path, e)


def _coerce_service_map(raw: dict[str, object]) -> dict[str, dict[str, str]]:
    """Keep only well-formed ``str -> {str -> str}`` entries from parsed JSON."""
    result: dict[str, dict[str, str]] = {}
    for agent_id, services in raw.items():
        if not isinstance(services, dict):
            continue
        clean = {name: url for name, url in services.items() if isinstance(name, str) and isinstance(url, str)}
        if clean:
            result[agent_id] = clean
    return result
