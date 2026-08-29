"""Parsing and ordering of ``minds-v*`` template tags."""

from imbue.minds.desktop_client.minds_version import MindsVersion
from imbue.minds.desktop_client.minds_version import parse_minds_version


def _version(tag: str) -> MindsVersion:
    parsed = parse_minds_version(tag)
    assert parsed is not None, f"{tag} should parse as a minds version"
    return parsed


def test_minds_versions_order_by_semver_not_lexically() -> None:
    assert _version("minds-v0.3.9") < _version("minds-v0.3.10")


def test_a_prerelease_sorts_below_the_release_it_precedes() -> None:
    assert _version("minds-v0.4.0-rc1") < _version("minds-v0.4.0")
    assert _version("minds-v0.4.0-rc1") < _version("minds-v0.4.0-rc2")


def test_numeric_prerelease_identifiers_order_numerically() -> None:
    assert _version("minds-v0.4.0-rc.2") < _version("minds-v0.4.0-rc.10")
    assert _version("minds-v0.4.0-1") < _version("minds-v0.4.0-alpha")


def test_a_non_release_ref_has_no_version() -> None:
    assert parse_minds_version("main") is None
    assert parse_minds_version("gabriel/some-branch") is None
    assert parse_minds_version("") is None
    assert parse_minds_version(None) is None
