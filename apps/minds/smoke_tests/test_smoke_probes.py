"""Read-only probes of the deployed surfaces a user reaches before signing in.

The connector's and LLM proxy's liveness and version, the hosted accounts
pages, the web chrome and its published-template trampoline, the redirects off
the former chrome hosts, the public download redirect, and the release feed's
channel manifests and installers. Nothing here needs a credential; the feed
probes skip on a tier that publishes no feed (staging, dev envs).
"""

import tomllib
from pathlib import Path
from typing import Final

import httpx2
import pytest
from pydantic import AnyUrl

from imbue.minds.smoke_tests.accounts import origin_of
from imbue.minds.smoke_tests.data_types import SmokeBackend
from imbue.minds.smoke_tests.feed import RELEASE_CHANNELS
from imbue.minds.smoke_tests.feed import RELEASE_PLATFORMS
from imbue.minds.smoke_tests.feed import RELEASE_VERSION_PATTERN
from imbue.minds.smoke_tests.feed import TEMPLATE_RELEASE_TAG_PATTERN
from imbue.minds.smoke_tests.feed import fetch_update_manifest
from imbue.minds.smoke_tests.feed import fetch_web_channel_pin
from imbue.minds.smoke_tests.feed import probe_artifact_status
from imbue.minds.smoke_tests.primitives import ReleaseChannelName
from imbue.minds.smoke_tests.primitives import ReleasePlatformName

pytestmark = [pytest.mark.smoke]

_TEST_TIMEOUT_SECONDS: Final[int] = 180
_FEED_TEST_TIMEOUT_SECONDS: Final[int] = 300
_DOWNLOADABLE_STATUSES: Final[frozenset[int]] = frozenset({200, 206})
# The hosted pages are one built bundle, so these paths answering 200 with HTML
# proves it is built and served on the accounts origin.
_ACCOUNTS_PAGE_PATHS: Final[tuple[str, ...]] = ("/login", "/signup", "/downloads")
_PUBLISHED_TEMPLATE_GIT_URL: Final[str] = "https://github.com/imbue-ai/default-workspace-template"
_DEEP_LINK_SCHEME: Final[str] = "imbue-studio://"
# The marketing site's primary download button: the arm64 dmg stable serves.
_MAC_DOWNLOAD_PLATFORM: Final[str] = "mac"
# The promotion file CI publishes the feed from.
_RELEASE_CHANNELS_FILE: Final[Path] = Path(__file__).resolve().parents[1] / "release-channels.toml"


def _feed_base_url_or_skip(backend: SmokeBackend) -> AnyUrl:
    if backend.update_feed_base_url is None:
        pytest.skip(f"{backend.env_name} publishes no release feed")
    return backend.update_feed_base_url


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_connector_and_llm_proxy_answer_liveness_and_version(
    backend: SmokeBackend, smoke_http_client: httpx2.Client
) -> None:
    """The two Modal apps the desktop talks to are up, and the connector names the deploy it is serving."""
    connector = origin_of(backend.connector_url)

    liveness = smoke_http_client.get(f"{connector}/health/liveness")
    assert liveness.status_code == 200, liveness.text
    assert liveness.json() == {"status": "ok"}, liveness.text

    version = smoke_http_client.get(f"{connector}/version")
    assert version.status_code == 200, version.text
    version_body = version.json()
    assert version_body.get("deploy_id"), version_body
    assert "generation_id" in version_body, version_body

    generation = smoke_http_client.get(f"{connector}/generation")
    assert generation.status_code == 200, generation.text
    assert generation.json().get("generation_id") == version_body["generation_id"], generation.text

    llm_liveness = smoke_http_client.get(f"{origin_of(backend.litellm_proxy_url)}/health/liveness")
    assert llm_liveness.status_code == 200, llm_liveness.text


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_accounts_surface_serves_its_pages_and_public_config(
    backend: SmokeBackend, smoke_http_client: httpx2.Client
) -> None:
    """The hosted sign-in bundle is built and served on the accounts origin, with the tier's own config behind it."""
    accounts = origin_of(backend.accounts_origin)

    for page_path in _ACCOUNTS_PAGE_PATHS:
        page = smoke_http_client.get(f"{accounts}{page_path}")
        assert page.status_code == 200, f"{page_path} answered {page.status_code}: {page.text[:200]!r}"
        assert page.headers.get("content-type", "").startswith("text/html"), (page_path, page.headers)

    config = smoke_http_client.get(f"{accounts}/accounts/api/config")
    assert config.status_code == 200, config.text
    config_body = config.json()
    assert isinstance(config_body["google_enabled"], bool), config_body
    assert isinstance(config_body["turnstile_site_key"], str), config_body
    # The deploy stamps the waitlist switch from the tier's deploy.toml; the
    # live connector must report the value the checkout declares.
    assert config_body["waitlist_enabled"] is backend.is_waitlist_enabled, config_body

    signed_out = smoke_http_client.get(f"{accounts}/accounts/api/me")
    assert signed_out.status_code == 401, signed_out.text
    assert signed_out.json() == {"signed_in": False}, signed_out.text


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_web_chrome_origin_serves_the_chrome_and_the_open_page(
    backend: SmokeBackend, smoke_http_client: httpx2.Client
) -> None:
    """The web chrome's origin serves the SPA shell and the published-template trampoline, which other hosts refuse."""
    if backend.chrome_origin is None:
        pytest.skip(f"{backend.env_name} declares no [origins] block, so it has no web chrome origin")
    chrome = origin_of(backend.chrome_origin)

    web_page = smoke_http_client.get(f"{chrome}/web")
    assert web_page.status_code == 200, f"/web answered {web_page.status_code}: {web_page.text[:200]!r}"
    assert web_page.headers.get("content-type", "").startswith("text/html"), web_page.headers

    open_page = smoke_http_client.get(f"{chrome}/open", params={"git_url": _PUBLISHED_TEMPLATE_GIT_URL})
    assert open_page.status_code == 200, f"/open answered {open_page.status_code}: {open_page.text[:200]!r}"
    assert _DEEP_LINK_SCHEME in open_page.text, open_page.text[:500]

    # The trampoline is served on the chrome host only, so a connector that
    # answers it on its API host has lost its chrome-origin configuration.
    connector = origin_of(backend.connector_url)
    if connector != chrome:
        misdirected = smoke_http_client.get(f"{connector}/open", params={"git_url": _PUBLISHED_TEMPLATE_GIT_URL})
        assert misdirected.status_code == 404, misdirected.text[:200]


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_legacy_chrome_origins_redirect_to_the_chrome_origin(
    backend: SmokeBackend, smoke_http_client: httpx2.Client
) -> None:
    """Every former chrome host still resolves, redirects pages permanently, and keeps serving the probes."""
    if not backend.legacy_chrome_origins:
        pytest.skip(f"{backend.env_name} declares no legacy chrome origins")
    assert backend.chrome_origin is not None
    chrome = origin_of(backend.chrome_origin)

    for legacy_origin in backend.legacy_chrome_origins:
        legacy = origin_of(legacy_origin)
        redirected = smoke_http_client.get(f"{legacy}/web")
        assert redirected.status_code == 301, f"{legacy}/web answered {redirected.status_code}"
        assert redirected.headers.get("location") == f"{chrome}/web", redirected.headers

        liveness = smoke_http_client.get(f"{legacy}/health/liveness")
        assert liveness.status_code == 200, f"{legacy}/health/liveness answered {liveness.status_code}"


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_mac_download_redirect_serves_a_downloadable_installer(
    backend: SmokeBackend, smoke_http_client: httpx2.Client
) -> None:
    """The marketing site's download button lands on an installer the CDN actually serves.

    One request per run, so one ``download_events`` row tagged with the suite's
    User-Agent; the other platforms are covered through the release feed.
    """
    download_host = origin_of(backend.chrome_origin or backend.connector_url)

    redirect = smoke_http_client.get(f"{download_host}/download", params={"platform": _MAC_DOWNLOAD_PLATFORM})
    assert redirect.status_code == 302, f"/download answered {redirect.status_code}: {redirect.text[:200]!r}"
    installer_url = redirect.headers.get("location", "")
    assert installer_url.endswith(".dmg"), installer_url

    status = probe_artifact_status(smoke_http_client, AnyUrl(installer_url))
    assert status in _DOWNLOADABLE_STATUSES, f"{installer_url} answered {status}"


@pytest.mark.timeout(_FEED_TEST_TIMEOUT_SECONDS)
@pytest.mark.parametrize("channel", RELEASE_CHANNELS)
def test_channel_publishes_a_manifest_per_platform_whose_installers_download(
    backend: SmokeBackend, smoke_http_client: httpx2.Client, channel: ReleaseChannelName
) -> None:
    """The channel's mac and linux manifests parse, name a release version, and list installers the CDN serves."""
    feed_base_url = _feed_base_url_or_skip(backend)
    for platform in RELEASE_PLATFORMS:
        manifest = fetch_update_manifest(smoke_http_client, feed_base_url, channel, platform)
        assert RELEASE_VERSION_PATTERN.match(str(manifest.version)), (channel, platform, manifest.version)
        assert manifest.rollout_percentage is None or 0 <= manifest.rollout_percentage <= 100, manifest
        for artifact_url in (a.url for a in manifest.artifacts):
            status = probe_artifact_status(smoke_http_client, artifact_url)
            assert status in _DOWNLOADABLE_STATUSES, f"{channel}-{platform}: {artifact_url} answered {status}"


@pytest.mark.timeout(_FEED_TEST_TIMEOUT_SECONDS)
@pytest.mark.parametrize("channel", RELEASE_CHANNELS)
def test_web_channel_pins_a_template_release_tag(
    backend: SmokeBackend, smoke_http_client: httpx2.Client, channel: ReleaseChannelName
) -> None:
    """Browser creates on the channel lease pool hosts baked at exactly one release tag, and its version matches."""
    feed_base_url = _feed_base_url_or_skip(backend)
    pin = fetch_web_channel_pin(smoke_http_client, feed_base_url, channel)
    tag_match = TEMPLATE_RELEASE_TAG_PATTERN.match(str(pin.template_ref))
    assert tag_match is not None, (channel, pin)
    assert tag_match.group(1) == str(pin.version), (channel, pin)


@pytest.mark.timeout(_FEED_TEST_TIMEOUT_SECONDS)
def test_feed_serves_the_channels_this_checkout_declares(
    backend: SmokeBackend, smoke_http_client: httpx2.Client
) -> None:
    """The publish landed: every entry of ``release-channels.toml`` is what the feed serves.

    This is the check a promotion ends with. It compares the feed against the
    checkout the suite runs from, so it is only meaningful from the commit
    whose promotion merged (and fails from a checkout that is ahead of or
    behind the published state).
    """
    feed_base_url = _feed_base_url_or_skip(backend)
    declared = tomllib.loads(_RELEASE_CHANNELS_FILE.read_text())

    for channel_name, entry in declared["channels"].items():
        channel = ReleaseChannelName(channel_name)
        for platform_name in entry["platforms"]:
            manifest = fetch_update_manifest(
                smoke_http_client, feed_base_url, channel, ReleasePlatformName(platform_name)
            )
            assert str(manifest.version) == entry["version"], (
                f"{channel}-{platform_name}: the feed serves {manifest.version}, this checkout declares {entry['version']}"
            )
            served_rollout = manifest.rollout_percentage if manifest.rollout_percentage is not None else 100
            assert served_rollout == entry["rollout_percentage"], (channel, platform_name, manifest)

    for channel_name, entry in declared["web_channels"].items():
        pin = fetch_web_channel_pin(smoke_http_client, feed_base_url, ReleaseChannelName(channel_name))
        assert str(pin.template_ref) == entry["template_ref"], (
            f"{channel_name}-web: the feed pins {pin.template_ref}, this checkout declares {entry['template_ref']}"
        )
