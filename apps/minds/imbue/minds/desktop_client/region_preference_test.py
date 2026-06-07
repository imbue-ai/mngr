from imbue.minds.desktop_client.region_preference import _RefreshThrottle
from imbue.minds.desktop_client.region_preference import region_from_geo_payload
from imbue.minds.desktop_client.region_preference import resolve_nearest_region


def test_resolve_nearest_region_picks_east_for_east_coast() -> None:
    # New York City is closer to US-EAST-VA than US-WEST-OR.
    assert resolve_nearest_region(40.71, -74.01) == "US-EAST-VA"


def test_resolve_nearest_region_picks_west_for_west_coast() -> None:
    # San Francisco is closer to US-WEST-OR.
    assert resolve_nearest_region(37.77, -122.42) == "US-WEST-OR"


def test_resolve_nearest_region_picks_nearest_for_far_away_user() -> None:
    # London is far from both, but still resolves to the nearer (east) datacenter.
    assert resolve_nearest_region(51.51, -0.13) == "US-EAST-VA"
    # Tokyo is nearer the west-coast datacenter.
    assert resolve_nearest_region(35.68, 139.69) == "US-WEST-OR"


def test_region_from_geo_payload_maps_valid_coordinates() -> None:
    assert region_from_geo_payload({"latitude": 45.5, "longitude": -122.9}) == "US-WEST-OR"


def test_region_from_geo_payload_returns_none_for_missing_fields() -> None:
    assert region_from_geo_payload({"city": "Nowhere"}) is None


def test_region_from_geo_payload_returns_none_for_non_numeric() -> None:
    assert region_from_geo_payload({"latitude": "north", "longitude": "west"}) is None


def test_region_from_geo_payload_returns_none_for_non_dict() -> None:
    assert region_from_geo_payload("not json") is None
    assert region_from_geo_payload(None) is None


def test_refresh_throttle_allows_first_then_blocks_within_interval() -> None:
    throttle = _RefreshThrottle(interval_seconds=3600.0)
    # First claim runs; an immediate second claim is throttled.
    assert throttle.claim_if_due() is True
    assert throttle.claim_if_due() is False


def test_refresh_throttle_allows_immediately_with_zero_interval() -> None:
    # A zero interval means every claim is due (never throttled).
    throttle = _RefreshThrottle(interval_seconds=0.0)
    assert throttle.claim_if_due() is True
    assert throttle.claim_if_due() is True
