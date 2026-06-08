from imbue.minds.desktop_client.region_preference import GeoLocationCache
from imbue.minds.desktop_client.region_preference import IMBUE_CLOUD_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import VULTR_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import default_region_for_provider
from imbue.minds.desktop_client.region_preference import known_regions_for_provider
from imbue.minds.desktop_client.region_preference import nearest_region_for_provider
from imbue.minds.desktop_client.region_preference import provider_supports_region
from imbue.minds.desktop_client.region_preference import resolve_default_region


def test_nearest_region_picks_east_for_east_coast() -> None:
    # New York City is closer to US-EAST-VA than US-WEST-OR.
    assert nearest_region_for_provider(IMBUE_CLOUD_PROVIDER_KEY, 40.71, -74.01) == "US-EAST-VA"


def test_nearest_region_picks_west_for_west_coast() -> None:
    # San Francisco is closer to US-WEST-OR.
    assert nearest_region_for_provider(IMBUE_CLOUD_PROVIDER_KEY, 37.77, -122.42) == "US-WEST-OR"


def test_nearest_region_for_vultr_picks_nearby_datacenter() -> None:
    # Tokyo maps to Vultr's nrt (Tokyo) region.
    assert nearest_region_for_provider(VULTR_PROVIDER_KEY, 35.68, 139.69) == "nrt"
    # London maps to Vultr's lhr (London) region.
    assert nearest_region_for_provider(VULTR_PROVIDER_KEY, 51.51, -0.13) == "lhr"


def test_nearest_region_for_unknown_provider_is_none() -> None:
    assert nearest_region_for_provider("docker", 40.71, -74.01) is None


def test_provider_supports_region_only_for_imbue_cloud_and_vultr() -> None:
    assert provider_supports_region(IMBUE_CLOUD_PROVIDER_KEY)
    assert provider_supports_region(VULTR_PROVIDER_KEY)
    assert not provider_supports_region("docker")
    assert not provider_supports_region("lima")


def test_known_regions_and_defaults() -> None:
    assert known_regions_for_provider(IMBUE_CLOUD_PROVIDER_KEY) == ("US-EAST-VA", "US-WEST-OR")
    assert default_region_for_provider(IMBUE_CLOUD_PROVIDER_KEY) == "US-EAST-VA"
    assert default_region_for_provider(VULTR_PROVIDER_KEY) == "ewr"
    assert "ewr" in known_regions_for_provider(VULTR_PROVIDER_KEY)


def test_resolve_default_region_prefers_configured_known_value() -> None:
    cache = GeoLocationCache()
    # San Francisco would resolve to US-WEST-OR by geo.
    cache.set_coordinates((37.77, -122.42))
    # A valid stored value wins over geolocation.
    assert resolve_default_region(IMBUE_CLOUD_PROVIDER_KEY, "US-EAST-VA", cache) == "US-EAST-VA"


def test_resolve_default_region_falls_back_to_geo_when_unconfigured() -> None:
    cache = GeoLocationCache()
    # San Francisco.
    cache.set_coordinates((37.77, -122.42))
    assert resolve_default_region(IMBUE_CLOUD_PROVIDER_KEY, None, cache) == "US-WEST-OR"


def test_resolve_default_region_ignores_unknown_configured_value() -> None:
    cache = GeoLocationCache()
    # NYC resolves to US-EAST-VA by geo.
    cache.set_coordinates((40.71, -74.01))
    # An unknown stored region is ignored; geo wins.
    assert resolve_default_region(IMBUE_CLOUD_PROVIDER_KEY, "MARS-WEST-1", cache) == "US-EAST-VA"


def test_resolve_default_region_falls_back_to_hardcoded_when_no_geo() -> None:
    cache = GeoLocationCache()
    # No coordinates and no configured value -> hardcoded default.
    assert resolve_default_region(VULTR_PROVIDER_KEY, None, cache) == "ewr"
