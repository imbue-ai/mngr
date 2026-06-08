"""Per-provider region selection for the create form.

The create form always shows an explicit region for the providers that support
one (``imbue_cloud`` and ``vultr``). The default shown is, in order of
preference:

1. the last-used region for that provider (persisted in ``~/.minds/config.toml``
   under ``[providers.<name>].region`` and written back on each successful
   create),
2. otherwise the region nearest the user's IP geolocation (fetched once at
   startup in the background via ifconfig.co and cached in memory only),
3. otherwise a hardcoded default per provider.

The geolocation lookup never blocks anything: if it has not returned by the time
the user reaches the create screen we just fall back to the hardcoded default.
"""

import threading
from math import asin
from math import cos
from math import radians
from math import sin
from math import sqrt
from typing import Final

import httpx
from loguru import logger
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# Canonical provider keys used to key region config + tables. They match the
# ``[providers.<name>]`` section names in ``~/.minds/config.toml`` and the
# provider instance names mngr uses (``imbue_cloud`` leases an OVH-US host;
# ``vultr`` is the cloud-VM provider).
IMBUE_CLOUD_PROVIDER_KEY: Final[str] = "imbue_cloud"
VULTR_PROVIDER_KEY: Final[str] = "vultr"

# Approximate (latitude, longitude) of each provider's regions, used for the
# coarse nearest-region geolocation default. The imbue_cloud pool lands hosts in
# the two OVH-US datacenters; these region codes MUST stay a subset of
# ``KNOWN_OVH_US_REGIONS`` in ``imbue.mngr_imbue_cloud.primitives``. minds keeps
# its own copy rather than importing the plugin, which it does not depend on.
_IMBUE_CLOUD_REGION_COORDINATES: Final[dict[str, tuple[float, float]]] = {
    "US-EAST-VA": (38.76, -77.61),
    "US-WEST-OR": (45.54, -122.96),
}

# Vultr region codes and the approximate coordinates of each datacenter city.
# Used only to pick a sensible default nearest the user; the user can override.
_VULTR_REGION_COORDINATES: Final[dict[str, tuple[float, float]]] = {
    "ewr": (40.74, -74.17),  # New Jersey
    "ord": (41.88, -87.63),  # Chicago
    "dfw": (32.78, -96.80),  # Dallas
    "sea": (47.61, -122.33),  # Seattle
    "lax": (34.05, -118.24),  # Los Angeles
    "atl": (33.75, -84.39),  # Atlanta
    "mia": (25.76, -80.19),  # Miami
    "sjc": (37.34, -121.89),  # Silicon Valley
    "yto": (43.65, -79.38),  # Toronto
    "mex": (19.43, -99.13),  # Mexico City
    "sao": (-23.55, -46.63),  # Sao Paulo
    "scl": (-33.45, -70.67),  # Santiago
    "lhr": (51.51, -0.13),  # London
    "man": (53.48, -2.24),  # Manchester
    "ams": (52.37, 4.90),  # Amsterdam
    "fra": (50.11, 8.68),  # Frankfurt
    "par": (48.86, 2.35),  # Paris
    "mad": (40.42, -3.70),  # Madrid
    "sto": (59.33, 18.07),  # Stockholm
    "waw": (52.23, 21.01),  # Warsaw
    "tlv": (32.07, 34.79),  # Tel Aviv
    "jnb": (-26.20, 28.05),  # Johannesburg
    "del": (28.61, 77.21),  # Delhi
    "bom": (19.08, 72.88),  # Mumbai
    "blr": (12.97, 77.59),  # Bangalore
    "sgp": (1.35, 103.82),  # Singapore
    "nrt": (35.68, 139.69),  # Tokyo
    "itm": (34.69, 135.50),  # Osaka
    "icn": (37.57, 126.98),  # Seoul
    "syd": (-33.87, 151.21),  # Sydney
    "mel": (-37.81, 144.96),  # Melbourne
}

_REGION_COORDINATES_BY_PROVIDER: Final[dict[str, dict[str, tuple[float, float]]]] = {
    IMBUE_CLOUD_PROVIDER_KEY: _IMBUE_CLOUD_REGION_COORDINATES,
    VULTR_PROVIDER_KEY: _VULTR_REGION_COORDINATES,
}

# Fallback region per provider when there is no stored value and geolocation has
# not (yet) resolved.
_DEFAULT_REGION_BY_PROVIDER: Final[dict[str, str]] = {
    IMBUE_CLOUD_PROVIDER_KEY: "US-EAST-VA",
    VULTR_PROVIDER_KEY: "ewr",
}

_EARTH_RADIUS_KM: Final[float] = 6371.0

# Hard timeout for the ifconfig.co fetch so a slow endpoint never ties up the
# background thread for long.
_IFCONFIG_TIMEOUT_SECONDS: Final[float] = 10.0
_IFCONFIG_URL: Final[str] = "https://ifconfig.co/json"


@pure
def provider_supports_region(provider_key: str) -> bool:
    """Return whether the given provider exposes an explicit region in the create form."""
    return provider_key in _REGION_COORDINATES_BY_PROVIDER


@pure
def known_regions_for_provider(provider_key: str) -> tuple[str, ...]:
    """Return the curated, ordered region codes offered for a provider (empty if unsupported)."""
    coordinates = _REGION_COORDINATES_BY_PROVIDER.get(provider_key)
    if coordinates is None:
        return ()
    return tuple(coordinates.keys())


@pure
def default_region_for_provider(provider_key: str) -> str:
    """Return the hardcoded fallback region for a provider (raises for an unsupported provider)."""
    return _DEFAULT_REGION_BY_PROVIDER[provider_key]


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
def nearest_region_for_provider(provider_key: str, latitude: float, longitude: float) -> str | None:
    """Return the provider's region nearest the given coordinates, or None if unsupported."""
    coordinates = _REGION_COORDINATES_BY_PROVIDER.get(provider_key)
    if not coordinates:
        return None
    return min(
        coordinates,
        key=lambda region: _haversine_distance_km(latitude, longitude, *coordinates[region]),
    )


@pure
def _coordinates_from_geo_payload(payload: object) -> tuple[float, float] | None:
    """Extract (latitude, longitude) from an ifconfig.co JSON payload, or None if unusable."""
    if not isinstance(payload, dict):
        return None
    geo_by_key: dict[str, object] = {str(key): value for key, value in payload.items()}
    latitude = geo_by_key.get("latitude")
    longitude = geo_by_key.get("longitude")
    # bool is an int subclass; geolocation never returns bools, but guard anyway.
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    return (float(latitude), float(longitude))


def fetch_geo_coordinates(http_timeout_seconds: float) -> tuple[float, float] | None:
    """Fetch the user's (latitude, longitude) from ifconfig.co.

    Best-effort: returns None on any network error, non-200 status, non-JSON
    body, or missing/unusable coordinates.
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
    coordinates = _coordinates_from_geo_payload(payload)
    if coordinates is None:
        logger.debug("IP geolocation response missing usable latitude/longitude: {}", payload)
    return coordinates


class GeoLocationCache(MutableModel):
    """In-memory holder for the user's geolocation, resolved once per process.

    Never persisted: a restart re-runs the lookup. The create form reads it as a
    best-effort default and falls back to a hardcoded region when it is empty.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _coordinates: tuple[float, float] | None = PrivateAttr(default=None)

    def set_coordinates(self, coordinates: tuple[float, float]) -> None:
        with self._lock:
            self._coordinates = coordinates

    def get_coordinates(self) -> tuple[float, float] | None:
        with self._lock:
            return self._coordinates


def start_geo_detection(concurrency_group: ConcurrencyGroup, geo_cache: GeoLocationCache) -> None:
    """Kick off the one-shot, non-blocking IP-geolocation lookup at startup.

    Returns immediately; on success the resolved coordinates are stored in
    ``geo_cache`` and logged. A failure is swallowed (logged at debug) so the
    create form simply falls back to the hardcoded per-provider default.
    """

    def _run() -> None:
        coordinates = fetch_geo_coordinates(_IFCONFIG_TIMEOUT_SECONDS)
        if coordinates is None:
            return
        geo_cache.set_coordinates(coordinates)
        logger.debug("Resolved IP geolocation to lat/long {} for region defaults", coordinates)

    concurrency_group.start_new_thread(
        target=_run,
        name="geo-location-detection",
        # A failed/slow lookup must not poison the root group; _run swallows its own failures.
        is_checked=False,
    )


def resolve_default_region(provider_key: str, configured_region: str | None, geo_cache: GeoLocationCache) -> str:
    """Resolve the region the create form should pre-select for a provider.

    Precedence: the stored last-used value (if it is a known region for this
    provider) -> the region nearest the user's geolocation (if known) -> the
    hardcoded per-provider default.
    """
    known_regions = known_regions_for_provider(provider_key)
    if configured_region and configured_region in known_regions:
        return configured_region
    coordinates = geo_cache.get_coordinates()
    if coordinates is not None:
        geo_region = nearest_region_for_provider(provider_key, coordinates[0], coordinates[1])
        if geo_region is not None:
            return geo_region
    return default_region_for_provider(provider_key)
