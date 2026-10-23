"""Which Imbue Studio build a smoke run launches, and the launch itself.

Three ways to name the build, in the order :func:`resolve_studio_binary`
applies them: an explicit executable (``MINDS_SMOKE_BINARY_PATH``), this
checkout's Electron (``MINDS_SMOKE_DEV_CHECKOUT=1``), or a release channel's
published build (``MINDS_SMOKE_CHANNEL``: the Linux .deb, or the Apple Silicon .zip), fetched from the backend's
feed and unpacked. The artifact a channel resolves to is this module's one
packaging decision: the ``.deb``, extracted rather than installed so the run
needs no root.

The launch reuses the e2e runner's Electron machinery (the CDP attach with its
relaunch on a wedged handshake), pointed at the build and at a throwaway
``MINDS_DATA_HOME``, so a smoke run never touches the operator's own install.
The environment it hands the app is built here rather than inherited: a
published build is the product, not a harness, so it must not see that a test
started it.
"""

import base64
import hashlib
import os
import platform
import shutil
import sys
import tempfile
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.minds.bootstrap import MINDS_APP_NAME
from imbue.minds.bootstrap import MINDS_DATA_HOME_ENV_VAR
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.desktop_client.e2e_workspace_runner import DEV_CHECKOUT_LAUNCH_TARGET
from imbue.minds.desktop_client.e2e_workspace_runner import ElectronLaunchTarget
from imbue.minds.desktop_client.e2e_workspace_runner import backend_origin_from_page
from imbue.minds.desktop_client.e2e_workspace_runner import electron_app_session
from imbue.minds.smoke_tests.accounts import smoke_headers
from imbue.minds.smoke_tests.backend import resolve_smoke_client_config_path
from imbue.minds.smoke_tests.data_types import ManifestArtifact
from imbue.minds.smoke_tests.data_types import SmokeBackend
from imbue.minds.smoke_tests.data_types import StudioAppSession
from imbue.minds.smoke_tests.data_types import StudioBinary
from imbue.minds.smoke_tests.data_types import StudioBinaryKind
from imbue.minds.smoke_tests.data_types import UpdateManifest
from imbue.minds.smoke_tests.feed import LINUX_PLATFORM
from imbue.minds.smoke_tests.feed import MAC_PLATFORM
from imbue.minds.smoke_tests.feed import fetch_update_manifest
from imbue.minds.smoke_tests.feed import linux_deb_artifact_of
from imbue.minds.smoke_tests.feed import mac_arm64_zip_artifact_of
from imbue.minds.smoke_tests.primitives import ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR
from imbue.minds.smoke_tests.primitives import ReleaseChannelName
from imbue.minds.smoke_tests.primitives import ReleasePlatformName
from imbue.minds.smoke_tests.primitives import SMOKE_BINARY_PATH_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_CHANNEL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_DEV_CHECKOUT_ENABLED_VALUE
from imbue.minds.smoke_tests.primitives import SMOKE_DEV_CHECKOUT_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_TIER_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError

# The env vars that name a build; one of them has to be set for a run to launch anything.
BINARY_SELECTION_ENV_VARS: Final[tuple[str, ...]] = (
    SMOKE_BINARY_PATH_ENV_VAR,
    SMOKE_DEV_CHECKOUT_ENV_VAR,
    SMOKE_CHANNEL_ENV_VAR,
)

# Where a packaged build keeps the root name its tier was baked with, relative
# to the executable: beside it on Linux (``/opt/<app>/resources``), and in the
# bundle's ``Contents/Resources`` on macOS.
_BUNDLED_ROOT_NAME_RELATIVE_PATH: Final[Path] = (
    Path("pyproject") / "imbue" / "minds" / "config" / "envs" / "_bundled" / "root_name"
)
_LINUX_RESOURCES_DIR_NAME: Final[str] = "resources"
_MACOS_RESOURCES_DIR_NAME: Final[str] = "Resources"

# What the ``.deb`` lays down: the app directory under ``/opt`` and the launcher in it.
_DEB_INSTALL_DIR: Final[Path] = Path("opt") / MINDS_APP_NAME
_DEB_EXECUTABLE_NAME: Final[str] = "imbue-studio"
_DEB_EXTRACTOR: Final[str] = "dpkg-deb"
# What the macOS ``.zip`` holds: the signed app bundle, whose executable Playwright launches directly.
_MACOS_BUNDLE_EXECUTABLE: Final[Path] = Path(f"{MINDS_APP_NAME}.app") / "Contents" / "MacOS" / MINDS_APP_NAME
# ``ditto`` keeps the bundle's symlinks, permissions and signature, which ``unzip`` need not.
_ZIP_EXTRACTOR: Final[str] = "ditto"
_NODE_BINARY: Final[str] = "node"
_LINUX_X86_64_MACHINES: Final[frozenset[str]] = frozenset({"x86_64", "amd64"})
_MACOS_PLATFORM_NAME: Final[str] = "darwin"
_MACOS_ARM64_MACHINES: Final[frozenset[str]] = frozenset({"arm64"})
_BUILD_CACHE_DIR_NAME: Final[str] = "imbue-studio-smoke"
# Enough of the digest to separate two builds, short enough to keep the path readable.
_CACHE_KEY_DIGEST_LENGTH: Final[int] = 16
_XDG_CACHE_HOME_ENV_VAR: Final[str] = "XDG_CACHE_HOME"
# The archive is downloaded inside the tree it unpacks into, so the scratch
# directory's own cleanup is the only thing that has to remove it.
_ARCHIVE_STEM: Final[str] = "archive"
# A ``.deb`` is a few hundred megabytes.
_DOWNLOAD_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
_EXTRACT_TIMEOUT_SECONDS: Final[float] = 600.0
_FEED_TIMEOUT_SECONDS: Final[float] = 60.0
# GitHub-hosted runners give Chromium a /dev/shm too small for a renderer.
_LINUX_CHROMIUM_ARGUMENTS: Final[tuple[str, ...]] = ("--disable-dev-shm-usage",)
_LINUX_PLATFORM_NAME: Final[str] = "linux"
# The Electron process runs from here: an empty directory, with no git root
# above it for its mngr to read a project config from, as a packaged install.
_LAUNCH_CWD_DIR_NAME: Final[str] = "studio-cwd"
# Python's ``webbrowser`` runs the command ``BROWSER`` names instead of the
# system browser; the app's web-login hands its sign-in URL to it.
_BROWSER_ENV_VAR: Final[str] = "BROWSER"
_NO_OP_COMMAND: Final[str] = "true"
_DISPLAY_ENV_VAR: Final[str] = "DISPLAY"
_LOGS_DIR_NAME: Final[str] = "logs"

# Variable families this process carries that an installed app never would:
# pytest's own marker and the resource guards' switches (``_PYTEST_GUARD_*``),
# mngr's host dir, prefix and project config, the minds tier this checkout is
# activated for, and the uv switches the mngr test harness sets (``UV_OFFLINE``,
# ``UV_FROZEN``), which would forbid the app's own first-launch ``uv sync`` the
# managed CPython it downloads.
_TEST_PROCESS_ENV_PREFIXES: Final[tuple[str, ...]] = ("PYTEST_", "MNGR_", "MINDS_", "UV_")
# The test venv ``uv run`` activates; the app points uv at a venv of its own.
_VIRTUAL_ENV_VAR: Final[str] = "VIRTUAL_ENV"
# The directory of guard shims the resource guards put first on this process's PATH.
_GUARD_WRAPPER_DIR_ENV_VAR: Final[str] = "_PYTEST_GUARD_WRAPPER_DIR"
_PATH_ENV_VAR: Final[str] = "PATH"


@pure
def is_studio_binary_selected(environ: Mapping[str, str]) -> bool:
    """Whether the environment names a build at all (as opposed to misnaming one)."""
    return any(environ.get(name, "") for name in BINARY_SELECTION_ENV_VARS)


def resolve_studio_binary(backend: SmokeBackend, environ: Mapping[str, str]) -> StudioBinary:
    """The build the environment names, checked to be launchable and to target ``backend``.

    An explicit executable wins over the dev checkout, which wins over a
    channel: the more specific the operator was, the more the choice counts.
    """
    explicit_path = environ.get(SMOKE_BINARY_PATH_ENV_VAR, "")
    if explicit_path:
        return packaged_studio_binary(Path(explicit_path), backend, label=f"binary at {explicit_path}")
    if environ.get(SMOKE_DEV_CHECKOUT_ENV_VAR, "") == SMOKE_DEV_CHECKOUT_ENABLED_VALUE:
        return dev_checkout_studio_binary(backend)
    channel = environ.get(SMOKE_CHANNEL_ENV_VAR, "")
    if channel:
        return published_channel_studio_binary(ReleaseChannelName(channel), backend)
    raise SmokeConfigError(
        f"No Imbue Studio build is named: set {SMOKE_CHANNEL_ENV_VAR} (a release channel's published Linux build), "
        f"{SMOKE_BINARY_PATH_ENV_VAR} (an executable), or {SMOKE_DEV_CHECKOUT_ENV_VAR}={SMOKE_DEV_CHECKOUT_ENABLED_VALUE} "
        "(this checkout's Electron)."
    )


def bundled_root_name_or_none(executable: Path) -> str | None:
    """The ``MINDS_ROOT_NAME`` a packaged build was stamped with, or None for a build that carries none."""
    candidates = (
        executable.parent / _LINUX_RESOURCES_DIR_NAME / _BUNDLED_ROOT_NAME_RELATIVE_PATH,
        executable.parent.parent / _MACOS_RESOURCES_DIR_NAME / _BUNDLED_ROOT_NAME_RELATIVE_PATH,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text().strip() or None
    return None


def packaged_studio_binary(executable: Path, backend: SmokeBackend, *, label: str) -> StudioBinary:
    """Describe a packaged build at ``executable``, refusing one baked for a tier other than ``backend``'s.

    The check is what keeps a run from ever creating resources on a tier it was
    not pointed at: a production build talks to production whatever the
    environment says.
    """
    if not executable.is_file():
        raise SmokeConfigError(f"The Imbue Studio executable {executable} does not exist.")
    expected_root_name = root_name_for_env_name(str(backend.env_name))
    bundled_root_name = bundled_root_name_or_none(executable)
    if bundled_root_name is not None and bundled_root_name != expected_root_name:
        raise SmokeConfigError(
            f"The build at {executable} is baked for the {env_name_from_root_name(bundled_root_name)!r} tier "
            f"(root name {bundled_root_name!r}), but {SMOKE_TIER_ENV_VAR} names {str(backend.env_name)!r}."
        )
    return StudioBinary(
        kind=StudioBinaryKind.PACKAGED,
        executable=executable,
        main_js=None,
        label=NonEmptyStr(label),
        root_name=NonEmptyStr(bundled_root_name if bundled_root_name is not None else expected_root_name),
    )


def dev_checkout_studio_binary(backend: SmokeBackend) -> StudioBinary:
    """Describe this checkout's Electron running this checkout's app against ``backend``."""
    target = DEV_CHECKOUT_LAUNCH_TARGET
    if not target.executable.is_file():
        raise SmokeConfigError(
            f"This checkout's Electron is missing at {target.executable}; run "
            "`cd apps/minds && pnpm install --frozen-lockfile && pnpm run build:ui` first."
        )
    if target.main_js is None or not target.main_js.is_file():
        raise SmokeConfigError(f"This checkout's Electron main script is missing at {target.main_js}.")
    # The pnpm-installed ``electron`` is a shell wrapper that execs ``node``.
    if shutil.which(_NODE_BINARY) is None:
        raise SmokeConfigError(
            f"`{_NODE_BINARY}` is not on PATH, and this checkout's Electron wrapper needs it; put the pinned "
            "Node (apps/minds/package.json `engines`) on PATH, e.g. ~/.nvm/versions/node/<version>/bin."
        )
    return StudioBinary(
        kind=StudioBinaryKind.DEV_CHECKOUT,
        executable=target.executable,
        main_js=target.main_js,
        label=NonEmptyStr("this checkout's Electron"),
        root_name=NonEmptyStr(root_name_for_env_name(str(backend.env_name))),
    )


class PublishedBuildLayout(FrozenModel):
    """How a channel's published build for one platform is fetched, unpacked, and launched."""

    platform: ReleasePlatformName = Field(description="The feed manifest the build is listed in (``mac``, ``linux``).")
    cache_name: NonEmptyStr = Field(description="The platform part of the build's cache directory name.")
    archive_suffix: NonEmptyStr = Field(description="The downloaded archive's suffix, ``.deb`` or ``.zip``.")
    extractor: NonEmptyStr = Field(description="The command that unpacks the archive without installing it.")
    executable_relative_path: Path = Field(description="The app executable's path inside the unpacked archive.")
    label_suffix: NonEmptyStr = Field(description="What the build's label calls the artifact.")


_LINUX_DEB_LAYOUT: Final[PublishedBuildLayout] = PublishedBuildLayout(
    platform=LINUX_PLATFORM,
    cache_name=NonEmptyStr("linux-x86_64"),
    archive_suffix=NonEmptyStr(".deb"),
    extractor=NonEmptyStr(_DEB_EXTRACTOR),
    executable_relative_path=_DEB_INSTALL_DIR / _DEB_EXECUTABLE_NAME,
    label_suffix=NonEmptyStr("linux .deb"),
)
_MACOS_ZIP_LAYOUT: Final[PublishedBuildLayout] = PublishedBuildLayout(
    platform=MAC_PLATFORM,
    cache_name=NonEmptyStr("macos-arm64"),
    archive_suffix=NonEmptyStr(".zip"),
    extractor=NonEmptyStr(_ZIP_EXTRACTOR),
    executable_relative_path=_MACOS_BUNDLE_EXECUTABLE,
    label_suffix=NonEmptyStr("macOS arm64 .zip"),
)


@pure
def published_build_layout_or_none(system_platform: str, machine: str) -> PublishedBuildLayout | None:
    """The published build this machine can run unpacked, or None where the feed publishes none.

    The feed publishes a Linux x86_64 ``.deb`` and an Apple Silicon ``.zip``;
    nothing for an Intel Mac or a non-x86 Linux.
    """
    if system_platform == _LINUX_PLATFORM_NAME and machine in _LINUX_X86_64_MACHINES:
        return _LINUX_DEB_LAYOUT
    if system_platform == _MACOS_PLATFORM_NAME and machine in _MACOS_ARM64_MACHINES:
        return _MACOS_ZIP_LAYOUT
    return None


@pure
def _xdg_cache_home_or_none(environ: Mapping[str, str]) -> Path | None:
    """``$XDG_CACHE_HOME`` when it names an absolute path, else None."""
    raw = environ.get(_XDG_CACHE_HOME_ENV_VAR, "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else None


@pure
def published_build_cache_root(system_platform: str, home_dir: Path, xdg_cache_home: Path | None) -> Path:
    """Where unpacked published builds live, across every checkout on this machine.

    Deliberately not under ``tempfile.gettempdir()``: macOS prunes that by
    access time, which leaves a bundle whose main executable survives while its
    helpers are gone -- a tree that looks cached and cannot launch. Shared
    rather than per-checkout because one build is hundreds of megabytes and this
    machine carries dozens of worktrees. The platform paths are written out
    rather than taken from ``platformdirs`` for the reason given in
    ``imbue/minds/bootstrap.py``.
    """
    if system_platform == _MACOS_PLATFORM_NAME:
        return home_dir / "Library" / "Caches" / _BUILD_CACHE_DIR_NAME / "builds"
    cache_home = xdg_cache_home if xdg_cache_home is not None else home_dir / ".cache"
    return cache_home / _BUILD_CACHE_DIR_NAME / "builds"


@pure
def published_build_cache_key(
    channel: ReleaseChannelName, version: NonEmptyStr, layout: PublishedBuildLayout, sha512: NonEmptyStr
) -> NonEmptyStr:
    """The cache directory name for one published build.

    The digest is part of the name, so a channel that republishes a version
    resolves to a different directory instead of reusing the old one. It is
    re-hashed to hex because the manifest states it in base64, whose ``+`` and
    ``/`` do not belong in a path component.
    """
    digest = hashlib.sha256(sha512.encode()).hexdigest()[:_CACHE_KEY_DIGEST_LENGTH]
    return NonEmptyStr(f"{channel}-{version}-{layout.cache_name}-{digest}")


def cached_build_dir(cache_root: Path, key: NonEmptyStr, populate: Callable[[Path], None]) -> Path:
    """The directory for ``key``, calling ``populate`` to fill a fresh one when it is absent.

    ``populate`` writes into a scratch directory that is renamed into place only
    once it returns, so the key's directory exists if and only if it holds a
    finished build. A populate that raises leaves nothing behind and the next
    run retries.
    """
    build_dir = cache_root / key
    if build_dir.is_dir():
        return build_dir
    cache_root.mkdir(parents=True, exist_ok=True)
    scratch_dir = Path(tempfile.mkdtemp(prefix=f"{key}.", dir=cache_root))
    try:
        populate(scratch_dir)
        scratch_dir.replace(build_dir)
    finally:
        # A no-op once the rename above moved it; on any other exit path, including
        # an interrupt, this is what keeps a half-written tree from being left behind.
        shutil.rmtree(scratch_dir, ignore_errors=True)
    return build_dir


def _archive_artifact_of(layout: PublishedBuildLayout, manifest: UpdateManifest) -> ManifestArtifact:
    if layout.platform == LINUX_PLATFORM:
        return linux_deb_artifact_of(manifest)
    return mac_arm64_zip_artifact_of(manifest)


def _extract_command(layout: PublishedBuildLayout, archive_path: Path, extract_dir: Path) -> list[str]:
    if layout.platform == LINUX_PLATFORM:
        return [str(layout.extractor), "-x", str(archive_path), str(extract_dir)]
    return [str(layout.extractor), "-x", "-k", str(archive_path), str(extract_dir)]


def published_channel_studio_binary(channel: ReleaseChannelName, backend: SmokeBackend) -> StudioBinary:
    """Fetch and unpack the channel's published build for this machine from ``backend``'s feed."""
    layout = published_build_layout_or_none(sys.platform, platform.machine())
    if layout is None:
        raise SmokeConfigError(
            f"{SMOKE_CHANNEL_ENV_VAR} resolves a channel's published build for Linux x86_64 (the .deb) or an "
            f"Apple Silicon Mac (the .zip), and the feed publishes neither for {sys.platform}/{platform.machine()}; "
            f"use {SMOKE_BINARY_PATH_ENV_VAR} or {SMOKE_DEV_CHECKOUT_ENV_VAR}={SMOKE_DEV_CHECKOUT_ENABLED_VALUE} here."
        )
    if backend.update_feed_base_url is None:
        raise SmokeConfigError(
            f"{str(backend.env_name)!r} publishes no release feed, so {SMOKE_CHANNEL_ENV_VAR}={str(channel)!r} "
            "names nothing there; a channel's build can only be tested against a tier with a feed."
        )
    if shutil.which(str(layout.extractor)) is None:
        raise SmokeConfigError(
            f"`{layout.extractor}` is not on PATH; it unpacks the published {layout.archive_suffix}."
        )

    with httpx.Client(timeout=_FEED_TIMEOUT_SECONDS, headers=smoke_headers(None)) as client:
        manifest = fetch_update_manifest(client, backend.update_feed_base_url, channel, layout.platform)
    artifact = _archive_artifact_of(layout, manifest)
    build_dir = cached_build_dir(
        published_build_cache_root(sys.platform, Path.home(), _xdg_cache_home_or_none(os.environ)),
        published_build_cache_key(channel, manifest.version, layout, artifact.sha512),
        lambda destination: _download_and_extract(artifact, destination, layout),
    )
    executable = build_dir / layout.executable_relative_path
    if not executable.is_file():
        raise SmokeConfigError(
            f"The build cached at {build_dir} has no executable at {executable}. Something outside this suite "
            f"damaged that directory; delete it and run again to fetch {artifact.url} afresh."
        )
    return packaged_studio_binary(executable, backend, label=f"{channel} {manifest.version} ({layout.label_suffix})")


def _download_and_extract(artifact: ManifestArtifact, destination: Path, layout: PublishedBuildLayout) -> None:
    """Fetch ``artifact``, prove it is the bytes the manifest names, and unpack it into ``destination``.

    The archive is downloaded beside the tree it unpacks into and deleted once
    extracted, so only the unpacked build is kept. The caller owns publishing
    ``destination``; this leaves a complete tree there or raises.
    """
    archive_path = destination / f"{_ARCHIVE_STEM}{layout.archive_suffix}"
    logger.info("Downloading the published build {} to {}", artifact.url, archive_path)
    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT, headers=smoke_headers(None), follow_redirects=True) as client:
        with client.stream("GET", str(artifact.url)) as response:
            if response.status_code != 200:
                raise SmokeConfigError(f"{artifact.url} answered {response.status_code}; cannot download the build.")
            digest = hashlib.sha512()
            with archive_path.open("wb") as archive_file:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    archive_file.write(chunk)
    downloaded_sha512 = base64.b64encode(digest.digest()).decode()
    if downloaded_sha512 != str(artifact.sha512):
        raise SmokeConfigError(
            f"{artifact.url} did not deliver the bytes its manifest names: the manifest declares sha512 "
            f"{artifact.sha512!s} and the download hashes to {downloaded_sha512}."
        )

    logger.info("Unpacking {} into {}", archive_path, destination)
    with ConcurrencyGroup(name="smoke-build-extract") as concurrency_group:
        finished = concurrency_group.run_process_to_completion(
            command=_extract_command(layout, archive_path, destination),
            timeout=_EXTRACT_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if finished.returncode != 0:
        raise SmokeConfigError(
            f"`{layout.extractor}` exited {finished.returncode} unpacking {archive_path}: {finished.stderr.strip()[:500]}"
        )
    archive_path.unlink(missing_ok=True)


def launch_target_for(binary: StudioBinary) -> ElectronLaunchTarget:
    """The e2e runner's launch description of ``binary``, with the switches a headless Linux run needs."""
    extra_arguments = _LINUX_CHROMIUM_ARGUMENTS if sys.platform == _LINUX_PLATFORM_NAME else ()
    return ElectronLaunchTarget(executable=binary.executable, main_js=binary.main_js, extra_arguments=extra_arguments)


@pure
def app_log_dir(data_home: Path, root_name: str) -> Path:
    """Where the app lays its logs under the data home: ``<data home>/<tier>/logs``."""
    return data_home / env_name_from_root_name(root_name) / _LOGS_DIR_NAME


@pure
def app_environment(environ: Mapping[str, str], launch_env: Mapping[str, str]) -> dict[str, str]:
    """The environment the app under test runs in: the shell's, minus this test process's own, plus ``launch_env``.

    The build under test is whatever the release channel serves, so it cannot
    carry an accommodation for being driven from a test; the run has to make
    its process look like an installed app's instead. Everything pytest put
    in this process's environment is therefore dropped rather than inherited:
    ``PYTEST_CURRENT_TEST`` (which mngr's config guard reads, and would make
    every config the app loads fail to load), the resource guards' per-test
    switches (which would make the binaries the app shells out to exit 127)
    and their shim directory on ``PATH``, the ``MNGR_``/``MINDS_`` variables
    that pin this checkout to its own host dir, prefix and tier, the ``UV_*``
    switches the mngr test harness sets (``UV_OFFLINE`` would forbid the
    app's first-launch ``uv sync`` the managed CPython it downloads), and the
    test venv ``uv run`` activated.

    What is left is the operator's own shell -- ``HOME``, ``PATH``, the
    locale, the display -- which is what an installed app is started with.
    """
    inherited = {
        name: value
        for name, value in environ.items()
        if not name.lstrip("_").startswith(_TEST_PROCESS_ENV_PREFIXES) and name != _VIRTUAL_ENV_VAR
    }
    guard_wrapper_dir = environ.get(_GUARD_WRAPPER_DIR_ENV_VAR, "")
    if guard_wrapper_dir and _PATH_ENV_VAR in inherited:
        inherited[_PATH_ENV_VAR] = os.pathsep.join(
            entry for entry in inherited[_PATH_ENV_VAR].split(os.pathsep) if entry != guard_wrapper_dir
        )
    return {**inherited, **launch_env}


@contextmanager
def launched_studio_app(
    binary: StudioBinary, backend: SmokeBackend, data_home: Path, debug_port: int, display: str | None
) -> Iterator[StudioAppSession]:
    """Launch ``binary`` against ``backend`` on a throwaway data home, with Playwright attached over CDP.

    The app is started the way an installed one is: from an empty directory
    outside any checkout, so the mngr it spawns finds no project config above
    its cwd, and on an environment built here rather than inherited from
    pytest (see :func:`app_environment`). The dev checkout is pointed at the
    backend's committed client config; a packaged build already carries its
    own.

    The app's web-login opens its sign-in URL in the system browser. The
    suite completes that page in a browser of its own, so the app is given a
    no-op ``BROWSER``: on a developer's machine the real browser would land
    the URL on whatever account is signed in there, and that account, not
    the suite's, would be handed to the app.
    """
    data_home.mkdir(parents=True, exist_ok=True)
    launch_cwd = data_home.parent / _LAUNCH_CWD_DIR_NAME
    launch_cwd.mkdir(parents=True, exist_ok=True)
    no_op_command = shutil.which(_NO_OP_COMMAND)
    if no_op_command is None:
        raise SmokeConfigError(f"`{_NO_OP_COMMAND}` is not on PATH; it stands in for the app's browser.")
    launch_env = {
        MINDS_DATA_HOME_ENV_VAR: str(data_home),
        MINDS_ROOT_NAME_ENV_VAR: str(binary.root_name),
        ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR: str(resolve_smoke_client_config_path(backend.env_name)),
        _BROWSER_ENV_VAR: no_op_command,
        **({_DISPLAY_ENV_VAR: display} if display is not None else {}),
    }
    logger.info("Launching {} as {} with {}={}", binary.label, binary.root_name, MINDS_DATA_HOME_ENV_VAR, data_home)
    with electron_app_session(
        None,
        debug_port,
        host_config_dir=launch_cwd,
        user_data_dir=None,
        launch_target=launch_target_for(binary),
        base_env=app_environment(os.environ, launch_env),
    ) as (browser, page):
        yield StudioAppSession(
            browser=browser,
            page=page,
            backend_origin=NonEmptyStr(backend_origin_from_page(page)),
            data_home=data_home,
            log_dir=app_log_dir(data_home, str(binary.root_name)),
        )
