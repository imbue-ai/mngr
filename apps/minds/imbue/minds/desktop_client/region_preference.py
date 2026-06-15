"""Per-provider region selection for the create form.

The create form always shows an explicit region for the providers that support
one (``imbue_cloud``, ``vultr``, and ``aws``). The default shown is, in order of
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
from imbue.minds.primitives import DEFAULT_AWS_REGION

# Canonical provider keys used to key region config + tables. They match the
# ``[providers.<name>]`` section names in ``~/.minds/config.toml`` and the
# provider instance names mngr uses (``imbue_cloud`` leases an OVH-US host;
# ``vultr`` is the cloud-VM provider). ``aws`` is the create-form-level key for
# the AWS provider: minds collapses the per-region ``aws-<region>`` mngr
# provider instances behind this single key (the chosen region is selected
# explicitly in the form, not encoded in the key).
IMBUE_CLOUD_PROVIDER_KEY: Final[str] = "imbue_cloud"
VULTR_PROVIDER_KEY: Final[str] = "vultr"
AWS_PROVIDER_KEY: Final[str] = "aws"

# Approximate (latitude, longitude) of each provider's regions, used for the
# coarse nearest-region geolocation default. The imbue_cloud pool lands hosts in
# the two OVH-US datacenters; these region codes MUST stay a subset of
# ``KNOWN_OVH_US_REGIONS`` in ``imbue.mngr_imbue_cloud.primitives``. minds keeps
# its own copy rather than importing the plugin, which it does not depend on.
_IMBUE_CLOUD_REGION_COORDINATES: Final[dict[str, tuple[float, float]]] = {
    "US-EAST-VA": (38.76, -77.61),
    "US-WEST-OR": (45.54, -122.96),
}

# Vultr region codes mapped to the approximate (latitude, longitude) of each
# region's datacenter city, used only to pick a sensible default nearest the user
# (the user can override). The city for each code, in declaration order, is:
# New Jersey, Chicago, Dallas, Seattle, Los Angeles, Atlanta, Miami, Silicon
# Valley, Toronto, Mexico City, Sao Paulo, Santiago, London, Manchester,
# Amsterdam, Frankfurt, Paris, Madrid, Stockholm, Warsaw, Tel Aviv, Johannesburg,
# Delhi, Mumbai, Bangalore, Singapore, Tokyo, Osaka, Seoul, Sydney, Melbourne.
_VULTR_REGION_COORDINATES: Final[dict[str, tuple[float, float]]] = {
    "ewr": (40.74, -74.17),
    "ord": (41.88, -87.63),
    "dfw": (32.78, -96.80),
    "sea": (47.61, -122.33),
    "lax": (34.05, -118.24),
    "atl": (33.75, -84.39),
    "mia": (25.76, -80.19),
    "sjc": (37.34, -121.89),
    "yto": (43.65, -79.38),
    "mex": (19.43, -99.13),
    "sao": (-23.55, -46.63),
    "scl": (-33.45, -70.67),
    "lhr": (51.51, -0.13),
    "man": (53.48, -2.24),
    "ams": (52.37, 4.90),
    "fra": (50.11, 8.68),
    "par": (48.86, 2.35),
    "mad": (40.42, -3.70),
    "sto": (59.33, 18.07),
    "waw": (52.23, 21.01),
    "tlv": (32.07, 34.79),
    "jnb": (-26.20, 28.05),
    "del": (28.61, 77.21),
    "bom": (19.08, 72.88),
    "blr": (12.97, 77.59),
    "sgp": (1.35, 103.82),
    "nrt": (35.68, 139.69),
    "itm": (34.69, 135.50),
    "icn": (37.57, 126.98),
    "syd": (-33.87, 151.21),
    "mel": (-37.81, 144.96),
}

# AWS region codes mapped to the approximate (latitude, longitude) of each
# region's datacenter, used only to pick a sensible default nearest the user
# (the user can override). The keys MUST stay in sync with
# ``CONFIGURED_AWS_REGIONS`` in ``imbue.minds.primitives`` (the single source of
# truth for which AWS regions minds offers); a region missing here just won't be
# considered for the geo-nearest default. Cities, in declaration order: N.
# Virginia, Ohio, N. California, Oregon, Ireland, Frankfurt, Singapore, Tokyo.
_AWS_REGION_COORDINATES: Final[dict[str, tuple[float, float]]] = {
    "us-east-1": (38.95, -77.45),
    "us-east-2": (40.42, -82.91),
    "us-west-1": (37.35, -121.96),
    "us-west-2": (45.87, -119.69),
    "eu-west-1": (53.41, -8.24),
    "eu-central-1": (50.11, 8.68),
    "ap-southeast-1": (1.35, 103.82),
    "ap-northeast-1": (35.68, 139.69),
}

_REGION_COORDINATES_BY_PROVIDER: Final[dict[str, dict[str, tuple[float, float]]]] = {
    IMBUE_CLOUD_PROVIDER_KEY: _IMBUE_CLOUD_REGION_COORDINATES,
    VULTR_PROVIDER_KEY: _VULTR_REGION_COORDINATES,
    AWS_PROVIDER_KEY: _AWS_REGION_COORDINATES,
}

# Fallback region per provider when there is no stored value and geolocation has
# not (yet) resolved.
_DEFAULT_REGION_BY_PROVIDER: Final[dict[str, str]] = {
    IMBUE_CLOUD_PROVIDER_KEY: "US-EAST-VA",
    VULTR_PROVIDER_KEY: "ewr",
    AWS_PROVIDER_KEY: DEFAULT_AWS_REGION,
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


def _run_geo_detection(geo_cache: GeoLocationCache) -> None:
    """Background body: fetch IP geolocation and store it, swallowing failures.

    A failure is swallowed (logged at debug) so the create form simply falls back
    to the hardcoded per-provider default.
    """
    coordinates = fetch_geo_coordinates(_IFCONFIG_TIMEOUT_SECONDS)
    if coordinates is None:
        return
    geo_cache.set_coordinates(coordinates)
    logger.debug("Resolved IP geolocation to lat/long {} for region defaults", coordinates)


def start_geo_detection(concurrency_group: ConcurrencyGroup, geo_cache: GeoLocationCache) -> None:
    """Kick off the one-shot, non-blocking IP-geolocation lookup at startup.

    Returns immediately; on success the resolved coordinates are stored in
    ``geo_cache`` and logged.
    """
    concurrency_group.start_new_thread(
        target=_run_geo_detection,
        kwargs={"geo_cache": geo_cache},
        name="geo-location-detection",
        # A failed/slow lookup must not poison the root group; the target swallows its own failures.
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
