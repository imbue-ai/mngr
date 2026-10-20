"""Last-known per-agent service map, persisted across ``mngr forward`` runs.

The resolver's per-agent ``{service -> url}`` and ``{origin label -> service}``
maps are only populated after the slow per-agent ``mngr event ... services
--follow`` stream connects (measured ~10s cold, longer under spawn contention).
Until then ``resolve()`` returns ``None`` and the proxy serves the 503 loading
page.

This cache persists both derived maps to disk while the plugin runs, so a fresh
run can seed the resolver from them at startup: a restored window then resolves
as soon as discovery supplies membership + SSH info instead of waiting on the
event stream. Labels matter as much as URLs here: every app origin routes by
its ``<name>-<rand>`` label, so a seed without labels serves the shell but
leaves every app on the 503 loader until the stream delivers -- and if that
stream is wedged, forever. The live stream still runs and overwrites the seed
as soon as it delivers, so a stale seed self-corrects within one stream
connect.

The cache file is a single JSON object::

    {
        "format_version": 2,
        "services_by_instance": {"<agent_id>@<host_id>": {"<service>": "<url>"}},
        "label_to_name_by_instance": {"<agent_id>@<host_id>": {"<label>": "<service>"}}
    }

Agent ids are unique per host, not globally, so entries are instance-scoped.
The pre-version format (the bare ``services_by_instance`` mapping as the whole
document) is still read, with empty labels. A version-2 file read by an older
``mngr forward`` degrades to an empty seed (its per-entry coercion drops the
nested objects), never to a bad route. Instance keys persisted by the even
older bare-agent-id format are dropped at seed time (see
``ForwardResolver.seed_services``).

The file lives under the caller-chosen path (the plugin points it at
``$MNGR_HOST_DIR/plugin/forward/``), so staging / production / local minds keep
independent caches automatically.
"""

import json
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import read_json_dict

_CACHE_FORMAT_VERSION: Final[int] = 2
_FORMAT_VERSION_KEY: Final[str] = "format_version"
_SERVICES_KEY: Final[str] = "services_by_instance"
_LABELS_KEY: Final[str] = "label_to_name_by_instance"


class PersistedServiceMap(FrozenModel):
    """Point-in-time snapshot of the resolver's per-instance service and label maps."""

    services_by_instance: dict[str, dict[str, str]] = Field(
        description="Per agent-instance-key service name -> backend URL"
    )
    label_to_name_by_instance: dict[str, dict[str, str]] = Field(
        description="Per agent-instance-key origin label -> service name"
    )


def _empty_persisted_service_map() -> PersistedServiceMap:
    # A fresh instance per call: the model's dict fields are mutable, so a
    # shared module-level "empty" constant could be corrupted by a caller.
    return PersistedServiceMap(services_by_instance={}, label_to_name_by_instance={})


class ServiceMapCache(FrozenModel):
    """Reads and writes the last-known per-agent service map at ``cache_path``."""

    cache_path: Path = Field(frozen=True, description="JSON file holding the persisted service map")

    def load(self) -> PersistedServiceMap:
        """Return the persisted service + label maps.

        A missing, empty, unreadable, or malformed cache file yields empty maps
        -- seeding from them is then a no-op and startup behaves exactly as if
        no cache existed. Only well-formed ``str -> {str -> str}`` entries are
        kept; anything else is dropped so a corrupt file can never inject a bad
        route. A file written by a newer format version than this reader knows
        is treated the same as absent.
        """
        try:
            raw = read_json_dict(self.cache_path)
        except (OSError, UnicodeDecodeError) as e:
            # OSError: unreadable file (permissions, IO). UnicodeDecodeError:
            # non-UTF-8 bytes, which read_json_dict does not catch. Either way
            # the file is unusable, so degrade to empty instead of breaking startup.
            logger.warning("Could not read forward service-map cache {} ({}); ignoring.", self.cache_path, e)
            return _empty_persisted_service_map()

        format_version = raw.get(_FORMAT_VERSION_KEY)
        if format_version is None:
            # Pre-version format: the whole document is the services map.
            # CLEANUP: drop this branch (and its legacy-format tests) once no
            # supported desktop install predates the version-2 cache -- the
            # cache rewrites on every service event, so any forward that has
            # run this code once holds a versioned file.
            return PersistedServiceMap(
                services_by_instance=_coerce_instance_map(raw),
                label_to_name_by_instance={},
            )
        if format_version != _CACHE_FORMAT_VERSION:
            logger.warning(
                "Ignoring forward service-map cache {} with unknown format version {!r}",
                self.cache_path,
                format_version,
            )
            return _empty_persisted_service_map()

        services_raw = raw.get(_SERVICES_KEY)
        labels_raw = raw.get(_LABELS_KEY)
        return PersistedServiceMap(
            services_by_instance=_coerce_instance_map(services_raw if isinstance(services_raw, dict) else {}),
            label_to_name_by_instance=_coerce_instance_map(labels_raw if isinstance(labels_raw, dict) else {}),
        )

    def persist(self, service_map: PersistedServiceMap) -> None:
        """Atomically write the full service + label maps to disk (best effort).

        Called on every mutation of the resolver's per-instance maps. A write
        failure is logged and swallowed: the cache is an optimization, and a
        failed persist must never break forwarding.
        """
        document = {
            _FORMAT_VERSION_KEY: _CACHE_FORMAT_VERSION,
            _SERVICES_KEY: service_map.services_by_instance,
            _LABELS_KEY: service_map.label_to_name_by_instance,
        }
        try:
            atomic_write(self.cache_path, json.dumps(document, sort_keys=True))
        except OSError as e:
            logger.warning("Could not persist forward service-map cache {} ({}); continuing.", self.cache_path, e)


def _coerce_instance_map(raw: dict[str, object]) -> dict[str, dict[str, str]]:
    """Keep only well-formed ``str -> {str -> str}`` entries from parsed JSON."""
    result: dict[str, dict[str, str]] = {}
    for instance_str, entries in raw.items():
        if not isinstance(entries, dict):
            continue
        clean = {key: value for key, value in entries.items() if isinstance(key, str) and isinstance(value, str)}
        if clean:
            result[instance_str] = clean
    return result
