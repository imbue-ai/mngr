import os
import stat
import threading
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mngr_latchkey.service_plugins import INSTALLED_COMMIT_STAMP_FILENAME
from imbue.mngr_latchkey.service_plugins import KNOWN_SERVICE_PLUGINS
from imbue.mngr_latchkey.service_plugins import KNOWN_SERVICE_PLUGIN_SERVICE_NAMES
from imbue.mngr_latchkey.service_plugins import PluginService
from imbue.mngr_latchkey.service_plugins import ServicePlugin
from imbue.mngr_latchkey.service_plugins import ServicePluginError
from imbue.mngr_latchkey.service_plugins import build_plugin_service_scope_schema
from imbue.mngr_latchkey.service_plugins import fetch_plugin_archive
from imbue.mngr_latchkey.service_plugins import install_known_service_plugins
from imbue.mngr_latchkey.service_plugins import install_service_plugin
from imbue.mngr_latchkey.service_plugins import installed_plugin_commit
from imbue.mngr_latchkey.service_plugins import is_known_plugin_service
from imbue.mngr_latchkey.service_plugins import plugins_dir
from imbue.mngr_latchkey.service_plugins import service_plugin_catalog_payload
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.testing import make_github_archive

_COMMIT = "0" * 39 + "1"
_OTHER_COMMIT = "0" * 39 + "2"

_TIDEPOOL = ServicePlugin(
    name="tidepool",
    repository="acme/latchkey-tidepool",
    commit=_COMMIT,
    services=(PluginService(service_name="tidepool", display_name="Tidepool", api_domains=("api.tidepool.invalid",)),),
)

_PLUGIN_FILES = {"package.json": '{"main": "dist/index.js"}', "dist/index.js": "export default () => ({});\n"}

# How long a download waits for the others to be in flight with it before the
# test gives up on them ever being so.
_CONCURRENT_DOWNLOADS_TIMEOUT_SECONDS = 10.0


def _archive(files: Mapping[str, str], top_level: str | None = f"latchkey-tidepool-{_COMMIT}") -> bytes:
    """The tarball GitHub would serve for ``_TIDEPOOL``'s commit, unless ``top_level`` wraps the files otherwise."""
    return make_github_archive(files, top_level)


def _fetching(archive: bytes) -> Callable[[ServicePlugin], bytes]:
    return lambda plugin: archive


def _never_fetching(plugin: ServicePlugin) -> bytes:
    raise AssertionError(f"nothing should be downloaded for {plugin.name}")


def _failing_fetch(plugin: ServicePlugin) -> bytes:
    raise ServicePluginError(f"Failed to download latchkey plugin {plugin.name}: connection refused")


def _renamed(plugin: ServicePlugin, name: str) -> ServicePlugin:
    """``plugin`` as a second plugin of its own: under ``name``, adding a service of that name."""
    return plugin.model_copy_update(
        to_update(plugin.field_ref().name, name),
        to_update(
            plugin.field_ref().services,
            (PluginService(service_name=name, display_name=name.title(), api_domains=(f"api.{name}.invalid",)),),
        ),
    )


def _install_known(
    latchkey_directory: Path,
    plugins: tuple[ServicePlugin, ...],
    fetch_archive: Callable[[ServicePlugin], bytes],
) -> tuple[str, ...]:
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as concurrency_group:
        return install_known_service_plugins(latchkey_directory, concurrency_group, plugins, fetch_archive)


def test_the_known_plugins_are_the_three_imbue_repositories_pinned_to_github_commits() -> None:
    assert [plugin.name for plugin in KNOWN_SERVICE_PLUGINS] == ["garmin", "spotify", "strava"]
    for plugin in KNOWN_SERVICE_PLUGINS:
        assert plugin.repository == f"imbue-ai/latchkey-{plugin.name}"
        assert plugin.archive_url == f"https://github.com/{plugin.repository}/archive/{plugin.commit}.tar.gz"
    assert KNOWN_SERVICE_PLUGIN_SERVICE_NAMES == {"garmin", "spotify", "strava"}
    assert is_known_plugin_service("spotify")
    assert not is_known_plugin_service("slack")


@pytest.mark.parametrize("commit", ["", "abc", "0928AC064AB8AD33BB365ED492556CC72B736AEC", "x" * 40])
def test_a_plugin_is_pinned_to_a_full_lowercase_commit(commit: str) -> None:
    with pytest.raises(ValueError, match="commit"):
        ServicePlugin(name=_TIDEPOOL.name, repository=_TIDEPOOL.repository, commit=commit, services=_TIDEPOOL.services)


def test_installing_a_plugin_unpacks_the_archive_where_latchkey_loads_it_and_stamps_the_commit(tmp_path: Path) -> None:
    assert install_service_plugin(tmp_path, _TIDEPOOL, _fetching(_archive(_PLUGIN_FILES))) is True

    installed = plugins_dir(tmp_path) / "tidepool"
    assert (installed / "dist" / "index.js").read_text() == _PLUGIN_FILES["dist/index.js"]
    assert (installed / "package.json").is_file()
    # GitHub's ``<repository>-<commit>/`` wrapper is dropped, so latchkey finds the package at the top.
    assert not (installed / f"latchkey-tidepool-{_COMMIT}").exists()
    assert installed_plugin_commit(tmp_path, "tidepool") == _COMMIT
    # Nothing is staged or left half-written beside it.
    assert [path.name for path in plugins_dir(tmp_path).iterdir()] == ["tidepool"]
    # Owner-only, like the rest of what latchkey loads code from.
    assert stat.S_IMODE(plugins_dir(tmp_path).stat().st_mode) == stat.S_IRWXU


def test_a_plugin_already_at_the_pinned_commit_is_left_alone_without_a_download(tmp_path: Path) -> None:
    install_service_plugin(tmp_path, _TIDEPOOL, _fetching(_archive(_PLUGIN_FILES)))
    (plugins_dir(tmp_path) / "tidepool" / "local-edit").write_text("kept")

    assert install_service_plugin(tmp_path, _TIDEPOOL, _never_fetching) is False

    assert (plugins_dir(tmp_path) / "tidepool" / "local-edit").read_text() == "kept"


def test_a_plugin_at_another_commit_is_replaced_whole(tmp_path: Path) -> None:
    install_service_plugin(tmp_path, _TIDEPOOL, _fetching(_archive({**_PLUGIN_FILES, "dist/old.js": "old"})))
    moved = _TIDEPOOL.model_copy_update(to_update(_TIDEPOOL.field_ref().commit, _OTHER_COMMIT))
    moved_archive = _archive(_PLUGIN_FILES, top_level=f"latchkey-tidepool-{_OTHER_COMMIT}")

    assert install_service_plugin(tmp_path, moved, _fetching(moved_archive)) is True

    installed = plugins_dir(tmp_path) / "tidepool"
    assert installed_plugin_commit(tmp_path, "tidepool") == _OTHER_COMMIT
    assert not (installed / "dist" / "old.js").exists()
    assert [path.name for path in plugins_dir(tmp_path).iterdir()] == ["tidepool"]


def test_a_plugin_installed_by_something_else_is_replaced(tmp_path: Path) -> None:
    """A directory with no stamp, such as a clone left by hand, is not known to be the pinned commit."""
    unstamped = plugins_dir(tmp_path) / "tidepool"
    unstamped.mkdir(parents=True)
    (unstamped / "index.js").write_text("whatever")

    assert install_service_plugin(tmp_path, _TIDEPOOL, _fetching(_archive(_PLUGIN_FILES))) is True

    assert not (unstamped / "index.js").exists()
    assert installed_plugin_commit(tmp_path, "tidepool") == _COMMIT


def test_a_stray_file_in_the_plugins_place_is_replaced(tmp_path: Path) -> None:
    plugins_dir(tmp_path).mkdir()
    (plugins_dir(tmp_path) / "tidepool").write_text("not a plugin")

    assert install_service_plugin(tmp_path, _TIDEPOOL, _fetching(_archive(_PLUGIN_FILES))) is True

    assert (plugins_dir(tmp_path) / "tidepool").is_dir()
    assert installed_plugin_commit(tmp_path, "tidepool") == _COMMIT


def test_a_plugin_whose_stamp_is_garbled_is_replaced_like_one_at_another_commit(tmp_path: Path) -> None:
    install_service_plugin(tmp_path, _TIDEPOOL, _fetching(_archive({**_PLUGIN_FILES, "dist/old.js": "old"})))
    stamp = plugins_dir(tmp_path) / "tidepool" / INSTALLED_COMMIT_STAMP_FILENAME
    stamp.write_bytes(b"\xff\xfe\x80 not text")

    assert _install_known(tmp_path, (_TIDEPOOL,), _fetching(_archive(_PLUGIN_FILES))) == ()

    assert installed_plugin_commit(tmp_path, "tidepool") == _COMMIT
    assert not (plugins_dir(tmp_path) / "tidepool" / "dist" / "old.js").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root is not held to file permissions")
def test_a_stamp_that_cannot_be_read_is_reported_rather_than_taken_for_a_missing_plugin(tmp_path: Path) -> None:
    """An unreadable stamp says nothing about the installed commit, so nothing is downloaded over it."""
    install_service_plugin(tmp_path, _TIDEPOOL, _fetching(_archive(_PLUGIN_FILES)))
    stamp = plugins_dir(tmp_path) / "tidepool" / INSTALLED_COMMIT_STAMP_FILENAME
    stamp.chmod(0)

    with pytest.raises(
        ServicePluginError, match=f"Failed to read the commit stamp of latchkey plugin tidepool at {stamp}"
    ):
        installed_plugin_commit(tmp_path, "tidepool")
    assert _install_known(tmp_path, (_TIDEPOOL,), _never_fetching) == ("tidepool",)

    stamp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert installed_plugin_commit(tmp_path, "tidepool") == _COMMIT


def test_a_linked_plugin_is_left_as_it_is(tmp_path: Path) -> None:
    """A symlink is a checkout somebody put there on purpose: never replaced, never followed."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "index.js").write_text("export default () => ({});\n")
    plugins_dir(tmp_path).mkdir()
    (plugins_dir(tmp_path) / "tidepool").symlink_to(checkout, target_is_directory=True)

    assert install_service_plugin(tmp_path, _TIDEPOOL, _never_fetching) is False

    assert (plugins_dir(tmp_path) / "tidepool").is_symlink()
    assert (checkout / "index.js").is_file()
    assert installed_plugin_commit(tmp_path, "tidepool") is None


# Where GitHub redirects an archive URL to, which is where the tarball is served from.
_TIDEPOOL_TARBALL_URL = f"https://codeload.github.invalid/acme/latchkey-tidepool/tar.gz/{_COMMIT}"


def test_downloading_a_plugin_follows_the_redirect_to_where_github_serves_the_tarball() -> None:
    archive = _archive(_PLUGIN_FILES)
    requested_urls: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        requested_urls.append(str(request.url))
        if str(request.url) == _TIDEPOOL.archive_url:
            return httpx2.Response(302, headers={"Location": _TIDEPOOL_TARBALL_URL})
        return httpx2.Response(200, content=archive)

    assert fetch_plugin_archive(_TIDEPOOL, httpx2.MockTransport(answer)) == archive

    assert requested_urls == [_TIDEPOOL.archive_url, _TIDEPOOL_TARBALL_URL]


def _answering_not_found(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(404, content=b"Not Found")


def _resetting_the_connection(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection reset", request=request)


def _cutting_the_response_short(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.RemoteProtocolError("peer closed connection without sending complete message body", request=request)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_answering_not_found, id="repository-gone"),
        pytest.param(_resetting_the_connection, id="unreachable"),
        pytest.param(_cutting_the_response_short, id="cut-short"),
    ],
)
def test_a_download_that_fails_or_is_cut_short_is_reported_as_a_plugin_error(
    answer: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    with pytest.raises(
        ServicePluginError, match=f"Failed to download latchkey plugin tidepool from {_TIDEPOOL.archive_url}"
    ):
        fetch_plugin_archive(_TIDEPOOL, httpx2.MockTransport(answer))


def test_a_failed_download_leaves_the_installed_plugin_and_nothing_else_behind(tmp_path: Path) -> None:
    install_service_plugin(tmp_path, _TIDEPOOL, _fetching(_archive(_PLUGIN_FILES)))
    moved = _TIDEPOOL.model_copy_update(to_update(_TIDEPOOL.field_ref().commit, _OTHER_COMMIT))

    with pytest.raises(ServicePluginError, match="connection refused"):
        install_service_plugin(tmp_path, moved, _failing_fetch)

    assert installed_plugin_commit(tmp_path, "tidepool") == _COMMIT
    assert [path.name for path in plugins_dir(tmp_path).iterdir()] == ["tidepool"]


@pytest.mark.parametrize(
    "archive",
    [
        pytest.param(b"not a tarball", id="not-a-tarball"),
        pytest.param(_archive(_PLUGIN_FILES)[: len(_archive(_PLUGIN_FILES)) // 2], id="cut-short"),
        pytest.param(_archive({}), id="empty"),
        pytest.param(_archive(_PLUGIN_FILES, top_level=None), id="no-wrapper-directory"),
        pytest.param(
            _archive({"one/index.js": "a", "two/index.js": "b"}, top_level=None), id="two-wrapper-directories"
        ),
    ],
)
def test_an_archive_that_is_not_a_repository_tarball_is_refused(tmp_path: Path, archive: bytes) -> None:
    with pytest.raises(ServicePluginError):
        install_service_plugin(tmp_path, _TIDEPOOL, _fetching(archive))

    assert [path.name for path in plugins_dir(tmp_path).iterdir()] == []


def test_installing_the_known_plugins_carries_on_past_a_failure_and_names_it(tmp_path: Path) -> None:
    other = _renamed(_TIDEPOOL, "rockpool")

    def fetch(plugin: ServicePlugin) -> bytes:
        if plugin.name == "tidepool":
            raise ServicePluginError("Failed to download latchkey plugin tidepool: connection refused")
        return _archive(_PLUGIN_FILES, top_level=f"latchkey-rockpool-{_COMMIT}")

    assert _install_known(tmp_path, (_TIDEPOOL, other), fetch) == ("tidepool",)

    assert installed_plugin_commit(tmp_path, "rockpool") == _COMMIT
    assert installed_plugin_commit(tmp_path, "tidepool") is None


def test_the_known_plugins_are_downloaded_at_the_same_time(tmp_path: Path) -> None:
    """Every download waits for all the others to be in flight, which one after another they never are."""
    plugins = (_TIDEPOOL, _renamed(_TIDEPOOL, "rockpool"), _renamed(_TIDEPOOL, "millpond"))
    all_in_flight = threading.Barrier(len(plugins), timeout=_CONCURRENT_DOWNLOADS_TIMEOUT_SECONDS)

    def fetch(plugin: ServicePlugin) -> bytes:
        try:
            all_in_flight.wait()
        except threading.BrokenBarrierError as e:
            raise ServicePluginError(f"{plugin.name} was downloaded on its own") from e
        return _archive(_PLUGIN_FILES, top_level=f"latchkey-{plugin.name}-{_COMMIT}")

    assert _install_known(tmp_path, plugins, fetch) == ()

    for plugin in plugins:
        assert installed_plugin_commit(tmp_path, plugin.name) == _COMMIT
    assert sorted(path.name for path in plugins_dir(tmp_path).iterdir()) == ["millpond", "rockpool", "tidepool"]


def test_installing_the_known_plugins_with_everything_in_place_downloads_nothing(tmp_path: Path) -> None:
    for plugin in KNOWN_SERVICE_PLUGINS:
        install_service_plugin(
            tmp_path, plugin, _fetching(_archive(_PLUGIN_FILES, top_level=f"latchkey-{plugin.name}-{plugin.commit}"))
        )

    assert _install_known(tmp_path, KNOWN_SERVICE_PLUGINS, _never_fetching) == ()


def test_installing_no_plugins_is_nothing_to_do(tmp_path: Path) -> None:
    assert _install_known(tmp_path, (), _never_fetching) == ()

    assert not plugins_dir(tmp_path).exists()


def test_every_known_plugin_service_is_cataloged_under_one_scope_pinned_to_its_api_host() -> None:
    payload = service_plugin_catalog_payload(frozenset(), KNOWN_SERVICE_PLUGINS)

    assert set(payload) == {"garmin", "spotify", "strava"}
    assert payload["strava"] == [
        {
            "scope": "strava",
            "display_name": "Strava",
            "description": "Requests to https://www.strava.com/.",
            "permissions": [],
            "scope_schema": {
                "properties": {"domain": {"const": "www.strava.com"}, "protocol": {"const": "https"}},
                "required": ["domain", "protocol"],
            },
        }
    ]


def test_a_plugin_service_the_catalog_already_ships_is_left_to_the_shipped_entry() -> None:
    payload = service_plugin_catalog_payload(frozenset({"strava"}), KNOWN_SERVICE_PLUGINS)

    assert "strava" not in payload
    assert "spotify" in payload


def test_a_service_on_several_hosts_is_scoped_to_any_of_them() -> None:
    assert build_plugin_service_scope_schema(("a.example.com", "b.example.com")) == {
        "properties": {"domain": {"enum": ["a.example.com", "b.example.com"]}, "protocol": {"const": "https"}},
        "required": ["domain", "protocol"],
    }


def test_the_catalog_of_an_install_names_the_plugin_services_and_the_shipped_one_does_not(tmp_path: Path) -> None:
    assert ServicesCatalog(latchkey_directory=tmp_path).get("spotify")[0].service_display_name == "Spotify"
    assert ServicesCatalog().get("spotify") == ()
