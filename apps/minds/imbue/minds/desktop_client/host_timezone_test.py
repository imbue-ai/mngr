"""Unit tests for reading and validating the host machine's IANA timezone."""

from zoneinfo import ZoneInfo

import pytest

from imbue.minds.desktop_client.host_timezone import read_host_timezone
from imbue.minds.desktop_client.host_timezone import validate_iana_timezone


def test_validate_iana_timezone_accepts_known_names() -> None:
    assert validate_iana_timezone("America/New_York") == "America/New_York"
    assert validate_iana_timezone("UTC") == "UTC"
    # Surrounding whitespace is trimmed.
    assert validate_iana_timezone("  Europe/London  ") == "Europe/London"


@pytest.mark.parametrize(
    "raw_timezone",
    [
        # Absent value.
        "",
        # Explicit null (e.g. a JSON null that reached the validator).
        None,
        # Not a real IANA tz: must never be reported as a real timezone.
        "Not/AZone",
        "America/Nowhere",
    ],
)
def test_validate_iana_timezone_returns_empty_for_missing_or_unknown(raw_timezone: object) -> None:
    """Missing or unrecognized values collapse to "" (callers use the host clock)."""
    assert validate_iana_timezone(raw_timezone) == ""


def test_read_host_timezone_returns_empty_or_loadable_name() -> None:
    # The value is host-dependent, so assert the contract: either "unknown"
    # ("") or a name the tz database actually knows.
    name = read_host_timezone()
    if name:
        ZoneInfo(name)
