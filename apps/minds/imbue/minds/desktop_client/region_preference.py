"""Best-effort resolution of the user's preferred imbue_cloud datacenter.

When the create page is opened we kick off a non-blocking, throttled lookup of
the user's IP geolocation (via ifconfig.co) and map it to the nearest OVH-US
datacenter. The result is stored in the minds settings file and later passed to
``mngr create`` as a soft ``-b preferred_region=`` knob. The lookup never blocks
the page render and, on any failure, leaves the previously stored preference
untouched.
"""

import threading
import time
from math import asin
from math import cos
from math import radians
from math import sin
from math import sqrt
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.errors import MindsConfigError

# Approximate coordinates (latitude, longitude) of the OVH-US datacenters the
# imbue_cloud pool can land hosts in -- Vint Hill, VA and Hillsboro, OR. Precise
# enough for a coarse nearest-datacenter choice. These region codes MUST stay a
# subset of ``KNOWN_OVH_US_REGIONS`` in
# ``imbue.mngr_imbue_cloud.primitives`` (the connector-side validator that
# rejects an unknown ``preferred_region``); minds keeps its own copy rather than
# importing the plugin, which it does not depend on.
_DATACENTER_COORDINATES: Final[dict[str, tuple[float, float]]] = {
    "US-EAST-VA": (38.76, -77.61),
    "US-WEST-OR": (45.54, -122.96),
}

_EARTH_RADIUS_KM: Final[float] = 6371.0

# Refresh at most this often per process. Tracked in-process only (see
# ``_RefreshThrottle``); a restart resets it, which is acceptable for a soft
# preference.
_REFRESH_INTERVAL_SECONDS: Final[float] = 3600.0

# Hard timeout for the ifconfig.co fetch so a slow endpoint never ties up the
# background thread for long.
_IFCONFIG_TIMEOUT_SECONDS: Final[float] = 10.0

_IFCONFIG_URL: Final[str] = "https://ifconfig.co/json"


@pure
def _haversine_distance_km(
    first_latitude: float,
    first_longitude: float,
    second_latitude: float,
    second_longitude: float,
) -> float:
    """Great-circle distance in kilometers between two lat/long points."""
    first_lat_rad = radians(first_latitude)
    second_lat_rad = radians(second_latitude)
    delta_lat_rad = radians(second_latitude - first_latitude)
    delta_lon_rad = radians(second_longitude - first_longitude)
    chord = sin(delta_lat_rad / 2) ** 2 + cos(first_lat_rad) * cos(second_lat_rad) * sin(delta_lon_rad / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(chord))


@pure
def resolve_nearest_region(latitude: float, longitude: float) -> str:
    """Return the OVH-US datacenter code nearest to the given coordinates.

    Always returns one of the known datacenters -- even for a point far from
    both -- so a user anywhere still gets the closer of the two.
    """
    return min(
        _DATACENTER_COORDINATES,
        key=lambda region: _haversine_distance_km(latitude, longitude, *_DATACENTER_COORDINATES[region]),
    )


@pure
def region_from_geo_payload(payload: object) -> str | None:
    """Map an ifconfig.co JSON payload to the nearest OVH-US region, or None if unusable.

    Returns None when the payload is not an object or its latitude/longitude are
    missing or non-numeric, so the caller can leave any stored preference intact.
    """
    if not isinstance(payload, dict):
        return None
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    # bool is an int subclass; geolocation never returns bools, but guard anyway.
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    return resolve_nearest_region(float(latitude), float(longitude))


def fetch_preferred_region(http_timeout_seconds: float) -> str | None:
    """Fetch IP geolocation from ifconfig.co and map it to the nearest OVH-US region.

    Best-effort: returns None on any network error, non-200 status, non-JSON
    body, or missing/unusable latitude/longitude so the caller can leave the
    stored preference untouched.
    """
    try:
        response = httpx.get(
            _IFCONFIG_URL,
            timeout=http_timeout_seconds,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        logger.debug("Failed to fetch IP geolocation from {}: {}", _IFCONFIG_URL, exc)
        return None
    if response.status_code != 200:
        logger.debug("IP geolocation request to {} returned status {}", _IFCONFIG_URL, response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError as exc:
        logger.debug("IP geolocation response was not valid JSON: {}", exc)
        return None
    region = region_from_geo_payload(payload)
    if region is None:
        logger.debug("IP geolocation response missing usable latitude/longitude: {}", payload)
    return region


class _RefreshThrottle(MutableModel):
    """In-process gate that lets a region refresh run at most once per interval."""

    interval_seconds: float = Field(frozen=True, description="Minimum seconds between refreshes")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _last_run_monotonic: float | None = PrivateAttr(default=None)

    def claim_if_due(self) -> bool:
        """Return True (recording 'now' as the last run) iff no refresh ran within the interval."""
        with self._lock:
            now = time.monotonic()
            if self._last_run_monotonic is not None and (now - self._last_run_monotonic) < self.interval_seconds:
                return False
            self._last_run_monotonic = now
            return True


# Process-global throttle: stores only the last-run time, never persisted.
_REFRESH_THROTTLE: Final[_RefreshThrottle] = _RefreshThrottle(interval_seconds=_REFRESH_INTERVAL_SECONDS)


def _run_refresh(minds_config: MindsConfig) -> None:
    """Background body: resolve the region and write it, leaving it untouched on failure."""
    region = fetch_preferred_region(_IFCONFIG_TIMEOUT_SECONDS)
    if region is None:
        return
    try:
        minds_config.set_preferred_region(region)
    except MindsConfigError as exc:
        # Best-effort: a config write failure must not surface anywhere user-facing.
        logger.debug("Failed to persist preferred region {}: {}", region, exc)


def trigger_preferred_region_refresh(
    concurrency_group: ConcurrencyGroup,
    minds_config: MindsConfig,
) -> None:
    """Schedule a non-blocking region refresh, throttled to ~once per process per hour.

    Returns immediately so the create page incurs no added latency. Does nothing
    if a refresh already ran within the throttle window.
    """
    if not _REFRESH_THROTTLE.claim_if_due():
        return
    concurrency_group.start_new_thread(
        target=_run_refresh,
        kwargs={"minds_config": minds_config},
        name="region-preference-refresh",
        # A failed/slow lookup must not poison the root group for unrelated work;
        # ``_run_refresh`` already swallows + logs its own failures.
        is_checked=False,
    )
