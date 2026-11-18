"""Latchkey plugins: services that do not ship with latchkey itself.

Latchkey only builds in services that talk to a documented, public API. A
service that rides an undocumented one -- a website's own session, a mobile
app's private endpoints -- is distributed as a *plugin* instead: an ES module
package placed in ``<LATCHKEY_DIRECTORY>/plugins/<name>``, which latchkey
loads at startup and from then on treats like any built-in service. A plugin
runs inside latchkey with access to every stored credential, so only the
plugins this build pins (:data:`KNOWN_SERVICE_PLUGINS`) are ever installed,
and each one at one exact commit of its repository.

Those plugins are installed everywhere a gateway runs, so every machine --
this computer and each remote host's VPS -- serves the same set of services:
here by :func:`install_known_service_plugins`, which ``mngr latchkey forward``
runs before it starts the gateway (the gateway loads plugins only as it
starts), and on a remote host by the ``mngr-latchkey`` package's ``postinst``,
from the same pins (see :mod:`imbue.mngr_latchkey.remote.package`). A machine
store (see :mod:`imbue.mngr_latchkey.remote._mirror`) shares this computer's
``plugins`` directory by symlink, so a credential whose type a plugin defines
can be read from it.

A plugin is optional where everything else a gateway needs is not: latchkey
serves every other service without it. Both installers therefore fetch the
plugins concurrently, bound each download, and skip a plugin that cannot be
fetched or unpacked -- GitHub unreachable, the repository gone -- with a
warning, leaving whatever copy is installed in place; the next install tries
again.

A plugin's service has no Detent schemas of its own, so
:func:`service_plugin_catalog_payload` gives each one a single scope pinned to
the hosts its API lives on, the way a custom service gets one (see
:mod:`imbue.mngr_latchkey.custom_services`).
"""

import io
import os
import shutil
import tarfile
import zlib
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Final
from uuid import uuid4

import httpx2
from loguru import logger
from pydantic import Field
from pydantic import JsonValue
from pydantic import computed_field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.utils.thread_cleanup import mngr_executor
from imbue.mngr_latchkey.custom_services import Scheme
from imbue.mngr_latchkey.custom_services import build_custom_service_scope_schema
from imbue.mngr_latchkey.custom_services import single_scope_catalog_entry
from imbue.mngr_latchkey.store import LatchkeyStoreError

# Name of the directory latchkey loads plugins from, under a latchkey directory.
PLUGINS_DIR_NAME: Final[str] = "plugins"

# Written inside an installed plugin's directory, holding the commit it was
# installed from, so a plugin at the pinned commit is recognized without a
# download. The ``mngr-latchkey`` package writes the same file on a remote host.
INSTALLED_COMMIT_STAMP_FILENAME: Final[str] = ".mngr-latchkey-commit"

# Latchkey skips entries of the plugins directory whose name starts with a dot,
# so a plugin is staged and discarded under such names and never loaded half-written.
_HIDDEN_ENTRY_PREFIX: Final[str] = "."

_GITHUB_ARCHIVE_URL_TEMPLATE: Final[str] = "https://github.com/{repository}/archive/{commit}.tar.gz"
# How long connecting to GitHub, and then each read from it, may take. Kept
# well under the usual ceiling for a network call, because the gateway does
# not start until the installs are over.
_ARCHIVE_SOCKET_TIMEOUT_SECONDS: Final[float] = 20.0


class ServicePluginError(LatchkeyStoreError):
    """Raised when a latchkey plugin cannot be installed."""


class PluginService(FrozenModel):
    """One service a latchkey plugin adds, and where its API lives."""

    service_name: str = Field(description="The latchkey service name (e.g. ``spotify``).")
    display_name: str = Field(description="Human-readable label of the service.")
    api_domains: tuple[str, ...] = Field(
        min_length=1, description="The https hosts the service's API lives on; its Detent scope pins them."
    )


class ServicePlugin(FrozenModel):
    """One latchkey plugin this build installs, pinned to a commit of its repository, and the services it adds."""

    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="The plugin's directory name under ``plugins/``; also how latchkey names it in errors.",
    )
    repository: str = Field(
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        description="The GitHub repository the plugin is fetched from, as ``owner/name``.",
    )
    commit: str = Field(
        pattern=r"^[0-9a-f]{40}$", description="The commit of the repository the plugin is installed at, in full."
    )
    services: tuple[PluginService, ...] = Field(min_length=1, description="The services the plugin adds.")

    @computed_field
    @property
    def archive_url(self) -> str:
        """The tarball of the repository at the pinned commit, as GitHub serves one for any commit.

        A computed field, so a dump of the plugin carries it to the templates
        of the ``mngr-latchkey`` package.
        """
        return _GITHUB_ARCHIVE_URL_TEMPLATE.format(repository=self.repository, commit=self.commit)


# The pins: see "Latchkey plugins" in the README for how to move one. A
# plugin's ``latchkeyVersion`` range has to admit the latchkey this build pins,
# because latchkey refuses to start with a plugin it cannot load.
KNOWN_SERVICE_PLUGINS: Final[tuple[ServicePlugin, ...]] = (
    # Garmin Connect, through the private API its mobile app talks to.
    ServicePlugin(
        name="garmin",
        repository="imbue-ai/latchkey-garmin",
        commit="0928ac064ab8ad33bb365ed492556cc72b736aec",
        services=(
            PluginService(
                service_name="garmin", display_name="Garmin Connect", api_domains=("connectapi.garmin.com",)
            ),
        ),
    ),
    # Spotify, through the private API its web player talks to.
    ServicePlugin(
        name="spotify",
        repository="imbue-ai/latchkey-spotify",
        commit="63bfa68f120a54ffa428dc9ad24016b47626eb16",
        services=(
            PluginService(
                service_name="spotify",
                display_name="Spotify",
                api_domains=("spclient.wg.spotify.com", "api-partner.spotify.com"),
            ),
        ),
    ),
    # Strava, through the website's own session rather than its public API. Read-only.
    ServicePlugin(
        name="strava",
        repository="imbue-ai/latchkey-strava",
        commit="58a606f352e0bf233133f567df7e2f37eb28cb45",
        services=(PluginService(service_name="strava", display_name="Strava", api_domains=("www.strava.com",)),),
    ),
)

# The services the pinned plugins add, by name: what makes a catalog service an
# experimental connector.
KNOWN_SERVICE_PLUGIN_SERVICE_NAMES: Final[frozenset[str]] = frozenset(
    service.service_name for plugin in KNOWN_SERVICE_PLUGINS for service in plugin.services
)


@pure
def is_known_plugin_service(service_name: str) -> bool:
    """Whether ``service_name`` is a service one of the pinned plugins adds, rather than a built-in or custom one."""
    return service_name in KNOWN_SERVICE_PLUGIN_SERVICE_NAMES


@pure
def build_plugin_service_scope_schema(api_domains: Sequence[str]) -> dict[str, JsonValue]:
    """Return the Detent scope schema pinning a request to https and one of ``api_domains``."""
    if len(api_domains) == 1:
        return build_custom_service_scope_schema(api_domains[0], Scheme.HTTPS)
    return {
        "properties": {"domain": {"enum": list(api_domains)}, "protocol": {"const": str(Scheme.HTTPS)}},
        "required": ["domain", "protocol"],
    }


def service_plugin_catalog_payload(
    shipped_service_names: frozenset[str], plugins: Sequence[ServicePlugin]
) -> dict[str, list[dict[str, object]]]:
    """Project the services ``plugins`` add into ``services.json``-shaped catalog entries.

    The same shape :func:`imbue.mngr_latchkey.custom_services.custom_service_catalog_payload`
    produces, for the same reason: the service has no Detent schemas, so it
    exposes one scope that pins its API hosts and lists no permissions
    (:func:`~imbue.mngr_latchkey.custom_services.single_scope_catalog_entry`).

    ``shipped_service_names`` are the names the catalog already serves. Latchkey
    refuses to load a plugin whose service name a built-in holds, so a plugin
    service that has since been built in is left to the shipped entry.
    """
    payload: dict[str, list[dict[str, object]]] = {}
    for plugin in plugins:
        for service in plugin.services:
            if service.service_name in shipped_service_names:
                logger.warning(
                    "Latchkey plugin {} adds service {}, which this build already ships; the shipped entry is kept "
                    "and the plugin's is ignored",
                    plugin.name,
                    service.service_name,
                )
                continue
            payload[service.service_name] = single_scope_catalog_entry(
                scope=service.service_name,
                display_name=service.display_name,
                description="Requests to " + ", ".join(f"https://{domain}/" for domain in service.api_domains) + ".",
                scope_schema=build_plugin_service_scope_schema(service.api_domains),
            )
    return payload


def plugins_dir(latchkey_directory: Path) -> Path:
    """Return the directory latchkey loads ``latchkey_directory``'s plugins from."""
    return latchkey_directory / PLUGINS_DIR_NAME


def installed_plugin_commit(latchkey_directory: Path, plugin_name: str) -> str | None:
    """The commit the plugin named ``plugin_name`` is installed at in ``latchkey_directory``, or ``None``.

    ``None`` for a plugin that is not installed, and for one installed by
    something other than this module (which writes no stamp). A stamp that
    holds something other than a commit, bytes that are no text included, is
    returned as the text it decodes to, which no pinned commit equals.

    Raises:
        ServicePluginError: when a stamp is there but cannot be read, which
            says nothing about which commit is installed.
    """
    stamp_path = plugins_dir(latchkey_directory) / plugin_name / INSTALLED_COMMIT_STAMP_FILENAME
    try:
        return stamp_path.read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise ServicePluginError(
            f"Failed to read the commit stamp of latchkey plugin {plugin_name} at {stamp_path}: {e}"
        ) from e


def fetch_plugin_archive(plugin: ServicePlugin, transport: httpx2.BaseTransport | None = None) -> bytes:
    """Download the tarball of ``plugin``'s repository at its pinned commit.

    GitHub answers an archive URL with a redirect to where the tarball is
    served from, which is followed. ``transport`` stands in for the network in
    tests.

    Raises:
        ServicePluginError: when it cannot be downloaded: GitHub unreachable or
            answering with an error status, a response cut short included.
    """
    try:
        with httpx2.Client(
            transport=transport, timeout=_ARCHIVE_SOCKET_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            return client.get(plugin.archive_url).raise_for_status().content
    except httpx2.HTTPError as e:
        raise ServicePluginError(
            f"Failed to download latchkey plugin {plugin.name} from {plugin.archive_url}: {e}"
        ) from e


def install_service_plugin(
    latchkey_directory: Path,
    plugin: ServicePlugin,
    fetch_archive: Callable[[ServicePlugin], bytes] = fetch_plugin_archive,
) -> bool:
    """Make ``latchkey_directory`` hold ``plugin`` at its pinned commit, returning whether anything changed.

    A plugin already installed at that commit is left alone, so this costs one
    file read in the steady state. A symlink in the plugin's place is left
    alone too, whatever it points at: it is a checkout somebody put there on
    purpose. Anything else there -- a plugin at another commit, or one nothing
    here installed -- is replaced.

    The archive is unpacked into a hidden sibling first (latchkey skips names
    starting with a dot) and renamed into place, so latchkey never loads a
    half-written plugin and a failed install leaves nothing behind.

    Raises:
        ServicePluginError: when the archive cannot be fetched or unpacked, or
            the plugin cannot be moved into place.
    """
    directory = plugins_dir(latchkey_directory)
    destination = directory / plugin.name
    if destination.is_symlink():
        logger.info("Latchkey plugin {} in {} is a symlink; leaving it as it is", plugin.name, directory)
        return False
    if installed_plugin_commit(latchkey_directory, plugin.name) == plugin.commit:
        logger.debug("Latchkey plugin {} is installed at {} in {}", plugin.name, plugin.commit, directory)
        return False
    staging = directory / f"{_HIDDEN_ENTRY_PREFIX}{plugin.name}.installing-{uuid4().hex}"
    with log_span("Installing latchkey plugin {} at {} into {}", plugin.name, plugin.commit, directory):
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _unpack_archive(fetch_archive(plugin), plugin, staging)
            (staging / INSTALLED_COMMIT_STAMP_FILENAME).write_text(plugin.commit + "\n", encoding="utf-8")
            _discard_plugin_entry(destination)
            os.replace(staging, destination)
        except OSError as e:
            raise ServicePluginError(f"Failed to install latchkey plugin {plugin.name} into {directory}: {e}") from e
        finally:
            _remove_staging(staging)
    return True


def install_known_service_plugins(
    latchkey_directory: Path,
    concurrency_group: ConcurrencyGroup,
    plugins: Sequence[ServicePlugin] = KNOWN_SERVICE_PLUGINS,
    fetch_archive: Callable[[ServicePlugin], bytes] = fetch_plugin_archive,
) -> tuple[str, ...]:
    """Make ``latchkey_directory`` hold every one of ``plugins`` at its pinned commit, as far as it can.

    The plugins are installed concurrently, a thread of ``concurrency_group``
    each, so the caller waits for the slowest download rather than for all of
    them in turn; every install is over, one way or the other, by the time
    this returns.

    Returns the names of the plugins that could not be installed. A failure is
    logged and leaves the other plugins to be installed: the gateway is to
    start either way, serving whatever is there, and the next start tries
    again. In the steady state nothing is downloaded (see
    :func:`install_service_plugin`).
    """
    if not plugins:
        return ()
    with mngr_executor(
        parent_cg=concurrency_group, name="latchkey-plugin-install", max_workers=len(plugins)
    ) as executor:
        installs = [
            executor.submit(install_service_plugin, latchkey_directory, plugin, fetch_archive) for plugin in plugins
        ]
    failed_names: list[str] = []
    for plugin, install in zip(plugins, installs, strict=True):
        try:
            install.result()
        except ServicePluginError as e:
            logger.warning("Latchkey plugin {} is not installed; its services are unavailable: {}", plugin.name, e)
            failed_names.append(plugin.name)
    return tuple(failed_names)


def _remove_staging(staging: Path) -> None:
    """Remove what an install left at ``staging``, if anything: a leftover is hidden from latchkey but not from disk.

    A failure to remove it is logged rather than raised, so it never stands in
    for the install's own outcome.
    """
    if not staging.is_dir():
        return
    try:
        shutil.rmtree(staging)
    except OSError as e:
        logger.warning("Could not remove {} left behind by a latchkey plugin install: {}", staging, e)


def _unpack_archive(content: bytes, plugin: ServicePlugin, destination: Path) -> None:
    """Unpack a GitHub repository tarball into ``destination``, dropping the ``<repository>-<commit>/`` it wraps.

    Raises:
        ServicePluginError: when ``content`` is not such a tarball, one cut
            short included.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            all_members = archive.getmembers()
            top_level_names = {member.name.partition("/")[0] for member in all_members}
            if len(top_level_names) != 1:
                raise ServicePluginError(
                    f"The archive of latchkey plugin {plugin.name} does not wrap one top-level directory"
                )
            members = []
            for member in all_members:
                inner_path = member.name.partition("/")[2]
                if inner_path:
                    member.name = inner_path
                    members.append(member)
            if not members:
                raise ServicePluginError(f"The archive of latchkey plugin {plugin.name} holds no files")
            archive.extractall(destination, members=members, filter="data")
    except (tarfile.TarError, zlib.error, EOFError, ValueError, OSError) as e:
        raise ServicePluginError(f"Failed to unpack the archive of latchkey plugin {plugin.name}: {e}") from e


def _discard_plugin_entry(installed: Path) -> None:
    """Remove whatever is at ``installed``, if anything: a plugin directory is renamed out of latchkey's sight first.

    The caller has already excluded a symlink, so anything here other than a
    directory is a stray file in the plugin's place, removed outright.
    """
    if not installed.exists():
        return
    if installed.is_dir():
        doomed = installed.parent / f"{_HIDDEN_ENTRY_PREFIX}{installed.name}.removing-{uuid4().hex}"
        os.replace(installed, doomed)
        shutil.rmtree(doomed)
    else:
        installed.unlink()
