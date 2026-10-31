import json

import pytest
from inline_snapshot import snapshot
from pydantic import AnyUrl

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.smoke_tests.data_types import ManifestArtifact
from imbue.minds.smoke_tests.data_types import UpdateManifest
from imbue.minds.smoke_tests.feed import channel_manifest_url
from imbue.minds.smoke_tests.feed import linux_deb_artifact_of
from imbue.minds.smoke_tests.feed import mac_arm64_zip_artifact_of
from imbue.minds.smoke_tests.feed import parse_update_manifest
from imbue.minds.smoke_tests.feed import parse_web_channel_pin
from imbue.minds.smoke_tests.primitives import ReleaseChannelName
from imbue.minds.smoke_tests.primitives import ReleasePlatformName
from imbue.minds.smoke_tests.primitives import SmokeManifestError

# The shape the promotion job publishes.
_PUBLISHED_MAC_MANIFEST = """version: 0.8.5
files:
- url: https://download.todesktop.com/26032588hqdzk/Imbue%20Studio%200.8.5%20-%20Build%202610066k8bzbhej-arm64-mac.zip
  sha512: 5FvnfzfmrTjMAGt4ZQVTzcTWIpLlCLlP3O+WJVuDj2ZEzEQoQc3ekRU8KDra3HF11o825wJfi5EehNM+/GBe1Q==
  size: 301373285
- url: https://download.todesktop.com/26032588hqdzk/Imbue%20Studio%200.8.5%20-%20Build%202610066k8bzbhej-arm64.dmg
  sha512: JnQKX+9OcQ9OsLhztDNaGSiNFyD3OHGzwYmOhw8CQxEsjl+1SSdANXu8yI06IkwTGFARERU2TMN/nbQtyF0S7w==
  size: 306446320
path: https://download.todesktop.com/26032588hqdzk/Imbue%20Studio%200.8.5%20-%20Build%202610066k8bzbhej-arm64-mac.zip
sha512: 5FvnfzfmrTjMAGt4ZQVTzcTWIpLlCLlP3O+WJVuDj2ZEzEQoQc3ekRU8KDra3HF11o825wJfi5EehNM+/GBe1Q==
releaseDate: '2026-10-06T03:48:04.138Z'
stagingPercentage: 100
"""

_PUBLISHED_LINUX_MANIFEST = """version: 0.8.5
files:
- url: https://download.todesktop.com/26032588hqdzk/minds-0.8.5-build-2610066k8bzbhej-x86_64.AppImage
  sha512: aGVsbG8=
  size: 200000000
  blockMapSize: 1000
- url: https://download.todesktop.com/26032588hqdzk/minds-0.8.5-build-2610066k8bzbhej-amd64.deb
  sha512: d29ybGQ=
  size: 190000000
path: https://download.todesktop.com/26032588hqdzk/minds-0.8.5-build-2610066k8bzbhej-x86_64.AppImage
sha512: aGVsbG8=
releaseDate: '2026-10-06T03:48:04.138Z'
"""


def test_parse_update_manifest_reads_version_rollout_and_installer_urls() -> None:
    manifest = parse_update_manifest(_PUBLISHED_MAC_MANIFEST)

    assert str(manifest.version) == snapshot("0.8.5")
    assert manifest.rollout_percentage == snapshot(100)
    # The top-level ``path`` repeats the first artifact and must not be counted twice.
    assert [str(a.url) for a in manifest.artifacts] == snapshot(
        [
            "https://download.todesktop.com/26032588hqdzk/Imbue%20Studio%200.8.5%20-%20Build%202610066k8bzbhej-arm64-mac.zip",
            "https://download.todesktop.com/26032588hqdzk/Imbue%20Studio%200.8.5%20-%20Build%202610066k8bzbhej-arm64.dmg",
        ]
    )


def test_parse_update_manifest_pairs_each_installer_with_its_own_sha512() -> None:
    mac = parse_update_manifest(_PUBLISHED_MAC_MANIFEST)

    # The top-level ``sha512`` repeats the first artifact's and is unindented, so it is not a third artifact.
    assert [(str(a.url).rsplit("-", 1)[-1], str(a.sha512)[:8]) for a in mac.artifacts] == [
        ("mac.zip", "5Fvnfzfm"),
        ("arm64.dmg", "JnQKX+9O"),
    ]

    # A ``blockMapSize`` line sits between the first artifact's sha512 and the next ``- url:``.
    linux = parse_update_manifest(_PUBLISHED_LINUX_MANIFEST)
    assert [str(a.sha512) for a in linux.artifacts] == ["aGVsbG8=", "d29ybGQ="]


def test_parse_update_manifest_refuses_an_installer_with_no_sha512() -> None:
    with pytest.raises(SmokeManifestError):
        parse_update_manifest("version: 0.4.0\nfiles:\n  - url: https://cdn.example/Studio-arm64.dmg\n")


def test_parse_update_manifest_treats_an_absent_rollout_line_as_fully_rolled_out() -> None:
    manifest = parse_update_manifest(
        "version: 0.4.0\nfiles:\n  - url: https://cdn.example/Studio-arm64.dmg\n    sha512: aGVsbG8=\n"
    )

    assert manifest.rollout_percentage is None
    assert str(manifest.version) == "0.4.0"


@pytest.mark.parametrize(
    "manifest_text",
    [
        "files:\n  - url: https://cdn.example/Studio-arm64.dmg\n",
        "version: 0.4.0\nfiles:\n",
        "version: 0.4.0\nversion: 0.4.1\nfiles:\n  - url: https://cdn.example/Studio-arm64.dmg\n",
    ],
)
def test_parse_update_manifest_refuses_a_manifest_missing_its_version_or_installers(manifest_text: str) -> None:
    with pytest.raises(SmokeManifestError):
        parse_update_manifest(manifest_text)


def test_parse_web_channel_pin_reads_the_template_tag_and_version() -> None:
    pin = parse_web_channel_pin(json.dumps({"version": "0.8.5", "templateRef": "minds-v0.8.5"}))

    assert str(pin.template_ref) == "minds-v0.8.5"
    assert str(pin.version) == "0.8.5"


@pytest.mark.parametrize("manifest_text", ["not json", json.dumps({"version": "0.8.5"}), json.dumps(["minds-v0.8.5"])])
def test_parse_web_channel_pin_refuses_a_manifest_without_both_fields(manifest_text: str) -> None:
    with pytest.raises(SmokeManifestError):
        parse_web_channel_pin(manifest_text)


def test_linux_deb_artifact_of_picks_the_deb_among_the_linux_installers() -> None:
    manifest = parse_update_manifest(_PUBLISHED_LINUX_MANIFEST)

    assert str(linux_deb_artifact_of(manifest).url) == snapshot(
        "https://download.todesktop.com/26032588hqdzk/minds-0.8.5-build-2610066k8bzbhej-amd64.deb"
    )


def test_linux_deb_artifact_of_refuses_a_manifest_without_exactly_one_deb() -> None:
    without_deb = parse_update_manifest(_PUBLISHED_MAC_MANIFEST)
    with pytest.raises(SmokeManifestError, match="found 0"):
        linux_deb_artifact_of(without_deb)

    two_debs = UpdateManifest(
        version=NonEmptyStr("0.8.5"),
        rollout_percentage=None,
        artifacts=(
            ManifestArtifact(url=AnyUrl("https://cdn.example/a.deb"), sha512=NonEmptyStr("aGVsbG8=")),
            ManifestArtifact(url=AnyUrl("https://cdn.example/b.DEB"), sha512=NonEmptyStr("d29ybGQ=")),
        ),
    )
    with pytest.raises(SmokeManifestError, match="found 2"):
        linux_deb_artifact_of(two_debs)


def test_channel_manifest_url_names_the_object_electron_updater_fetches() -> None:
    url = channel_manifest_url(
        AnyUrl("https://updates.example.com/"), ReleaseChannelName("alpha"), ReleasePlatformName("linux")
    )
    assert url == "https://updates.example.com/alpha-linux.yml"


def test_mac_arm64_zip_artifact_of_picks_the_apple_silicon_bundle_among_the_mac_installers() -> None:
    manifest = parse_update_manifest(_PUBLISHED_MAC_MANIFEST)

    assert str(mac_arm64_zip_artifact_of(manifest).url) == snapshot(
        "https://download.todesktop.com/26032588hqdzk/Imbue%20Studio%200.8.5%20-%20Build%202610066k8bzbhej-arm64-mac.zip"
    )


def test_mac_arm64_zip_artifact_of_refuses_a_manifest_without_the_bundle() -> None:
    with pytest.raises(SmokeManifestError, match="found 0"):
        mac_arm64_zip_artifact_of(parse_update_manifest(_PUBLISHED_LINUX_MANIFEST))
