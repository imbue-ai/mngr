"""The release feed every Imbue Studio install updates from, as the smoke suite reads it.

A desktop channel publishes one electron-updater manifest per platform
(``<channel>-mac.yml``, ``<channel>-linux.yml``) listing the installers the
CDN serves; a web channel publishes ``<channel>-web.json``, which pins the
template release tag browser creates lease. The parsers here read the few
fields the suite checks without a YAML parser, which this project keeps out.
"""

import json
import re
from collections.abc import Sequence
from typing import Final

import httpx2
from pydantic import AnyUrl

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.minds.smoke_tests.accounts import origin_of
from imbue.minds.smoke_tests.data_types import ManifestArtifact
from imbue.minds.smoke_tests.data_types import UpdateManifest
from imbue.minds.smoke_tests.data_types import WebChannelPin
from imbue.minds.smoke_tests.primitives import ReleaseChannelName
from imbue.minds.smoke_tests.primitives import ReleasePlatformName
from imbue.minds.smoke_tests.primitives import SmokeManifestError

RELEASE_CHANNELS: Final[tuple[ReleaseChannelName, ...]] = (
    ReleaseChannelName("stable"),
    ReleaseChannelName("beta"),
    ReleaseChannelName("alpha"),
)
LINUX_PLATFORM: Final[ReleasePlatformName] = ReleasePlatformName("linux")
MAC_PLATFORM: Final[ReleasePlatformName] = ReleasePlatformName("mac")
RELEASE_PLATFORMS: Final[tuple[ReleasePlatformName, ...]] = (MAC_PLATFORM, LINUX_PLATFORM)

RELEASE_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")
# A web channel pins a default-workspace-template release tag; the version in
# the tag is what the manifest's own ``version`` field repeats.
TEMPLATE_RELEASE_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^minds-v(\d+\.\d+\.\d+)$")

# The Linux installer the Linux smoke lane runs: electron-updater's DebUpdater
# picks it by this suffix too.
_DEB_SUFFIX: Final[str] = ".deb"
_MAC_ARM64_ZIP_SUFFIX: Final[str] = "-arm64-mac.zip"

# The manifest fields the suite reads each sit on a line of their own in the
# YAML electron-updater consumes, so a line scan reads them.
_MANIFEST_VERSION_LINE: Final[re.Pattern[str]] = re.compile(r"^version:\s*['\"]?([^'\"\s]+)['\"]?\s*$")
_MANIFEST_ROLLOUT_LINE: Final[re.Pattern[str]] = re.compile(r"^stagingPercentage:\s*(\d+)\s*$")
_MANIFEST_ARTIFACT_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*-\s*url:\s*(\S+)\s*$")
# Indented, unlike the top-level ``sha512`` that repeats the first artifact's.
_MANIFEST_ARTIFACT_SHA512_LINE: Final[re.Pattern[str]] = re.compile(r"^\s+sha512:\s*(\S+)\s*$")


@pure
def channel_manifest_url(feed_base_url: AnyUrl, channel: ReleaseChannelName, platform: ReleasePlatformName) -> str:
    """The object electron-updater asks the feed for, e.g. ``<feed>/alpha-linux.yml``."""
    return f"{origin_of(feed_base_url)}/{channel}-{platform}.yml"


@pure
def parse_artifacts(lines: Sequence[str]) -> tuple[ManifestArtifact, ...]:
    """Pair every ``- url:`` with the indented ``sha512`` of its own block.

    A block runs to the next ``- url:``, so an intervening ``size`` or
    ``blockMapSize`` does not separate an installer from its digest, and the
    unindented top-level ``sha512`` that repeats the first artifact's belongs to
    no block.
    """
    artifacts: list[ManifestArtifact] = []
    url: str | None = None
    for line in lines:
        if (url_match := _MANIFEST_ARTIFACT_LINE.match(line)) is not None:
            if url is not None:
                raise SmokeManifestError(f"The manifest lists {url!r} with no sha512 of its own")
            url = url_match.group(1)
            continue
        if url is not None and (sha_match := _MANIFEST_ARTIFACT_SHA512_LINE.match(line)) is not None:
            artifacts.append(ManifestArtifact(url=AnyUrl(url), sha512=NonEmptyStr(sha_match.group(1))))
            url = None
    if url is not None:
        raise SmokeManifestError(f"The manifest lists {url!r} with no sha512 of its own")
    return tuple(artifacts)


@pure
def parse_update_manifest(manifest_text: str) -> UpdateManifest:
    """Read the version, rollout percentage, and installers out of an electron-updater channel manifest."""
    lines = manifest_text.splitlines()
    versions = [match.group(1) for line in lines if (match := _MANIFEST_VERSION_LINE.match(line)) is not None]
    rollouts = [int(match.group(1)) for line in lines if (match := _MANIFEST_ROLLOUT_LINE.match(line)) is not None]
    artifacts = parse_artifacts(lines)
    if len(versions) != 1:
        raise SmokeManifestError(f"Expected exactly one top-level version line, found {len(versions)}: {versions}")
    if len(rollouts) > 1:
        raise SmokeManifestError(f"Expected at most one stagingPercentage line, found {rollouts}")
    if not artifacts:
        raise SmokeManifestError("The manifest lists no installer URLs")
    return UpdateManifest(
        version=NonEmptyStr(versions[0]),
        rollout_percentage=rollouts[0] if rollouts else None,
        artifacts=artifacts,
    )


@pure
def parse_web_channel_pin(manifest_text: str) -> WebChannelPin:
    """Read a ``<channel>-web.json`` manifest: the template release tag browser creates pin to, and its version."""
    try:
        payload = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        raise SmokeManifestError(f"The web channel manifest is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or "templateRef" not in payload or "version" not in payload:
        raise SmokeManifestError(f"The web channel manifest lacks templateRef or version: {manifest_text[:200]!r}")
    return WebChannelPin(
        version=NonEmptyStr(str(payload["version"])), template_ref=NonEmptyStr(str(payload["templateRef"]))
    )


@pure
def _sole_artifact_with_suffix(manifest: UpdateManifest, suffix: str, noun: str) -> ManifestArtifact:
    matched = [a for a in manifest.artifacts if str(a.url).lower().endswith(suffix)]
    if len(matched) != 1:
        raise SmokeManifestError(
            f"Expected exactly one {noun} in the manifest for version {manifest.version}, "
            f"found {len(matched)}: {[str(a.url) for a in manifest.artifacts]}"
        )
    return matched[0]


@pure
def linux_deb_artifact_of(manifest: UpdateManifest) -> ManifestArtifact:
    """The one ``.deb`` a channel's Linux manifest lists: the artifact the Linux smoke lane installs."""
    return _sole_artifact_with_suffix(manifest, _DEB_SUFFIX, f"{_DEB_SUFFIX} installer")


@pure
def mac_arm64_zip_artifact_of(manifest: UpdateManifest) -> ManifestArtifact:
    """The one Apple Silicon ``.zip`` a channel's macOS manifest lists: the bundle the macOS smoke lane unpacks."""
    return _sole_artifact_with_suffix(manifest, _MAC_ARM64_ZIP_SUFFIX, f"{_MAC_ARM64_ZIP_SUFFIX} bundle")


def fetch_update_manifest(
    client: httpx2.Client, feed_base_url: AnyUrl, channel: ReleaseChannelName, platform: ReleasePlatformName
) -> UpdateManifest:
    url = channel_manifest_url(feed_base_url, channel, platform)
    response = client.get(url)
    if response.status_code != 200:
        raise SmokeManifestError(f"{url} answered {response.status_code}: {response.text[:200]!r}")
    return parse_update_manifest(response.text)


def fetch_web_channel_pin(client: httpx2.Client, feed_base_url: AnyUrl, channel: ReleaseChannelName) -> WebChannelPin:
    url = f"{origin_of(feed_base_url)}/{channel}-web.json"
    response = client.get(url)
    if response.status_code != 200:
        raise SmokeManifestError(f"{url} answered {response.status_code}: {response.text[:200]!r}")
    return parse_web_channel_pin(response.text)


def probe_artifact_status(client: httpx2.Client, url: AnyUrl) -> int:
    """Ask for the first byte of an installer and return the status, so a 300 MB artifact is checked without a download."""
    with client.stream("GET", str(url), headers={"Range": "bytes=0-0"}) as response:
        return response.status_code
