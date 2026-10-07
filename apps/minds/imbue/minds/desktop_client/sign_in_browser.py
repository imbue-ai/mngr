"""The browsers a provider sign-in can open in, and opening one there.

A sign-in opens in the user's default browser unless they picked another in Settings, typically
because the browser they are signed in to their provider with is not the default one. Only the
browsers found installed are offered; a choice whose browser has since gone falls back to the
default.
"""

import platform
import shlex
import shutil
import webbrowser
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# Browsers looked for on macOS: (label, app bundle name).
_MACOS_BROWSER_APPS: Final[tuple[tuple[str, str], ...]] = (
    ("Safari", "Safari.app"),
    ("Google Chrome", "Google Chrome.app"),
    ("Firefox", "Firefox.app"),
    ("Arc", "Arc.app"),
    ("Brave", "Brave Browser.app"),
    ("Microsoft Edge", "Microsoft Edge.app"),
    ("Chromium", "Chromium.app"),
    ("Vivaldi", "Vivaldi.app"),
    ("Opera", "Opera.app"),
    ("Orion", "Orion.app"),
    ("Zen", "Zen.app"),
)
# Browsers looked for on Linux: (label, executable name), first match per label wins.
_LINUX_BROWSER_EXECUTABLES: Final[tuple[tuple[str, str], ...]] = (
    ("Google Chrome", "google-chrome-stable"),
    ("Google Chrome", "google-chrome"),
    ("Chromium", "chromium"),
    ("Chromium", "chromium-browser"),
    ("Firefox", "firefox"),
    ("Brave", "brave-browser"),
    ("Microsoft Edge", "microsoft-edge"),
    ("Vivaldi", "vivaldi"),
    ("Opera", "opera"),
)
_MACOS_APPLICATION_DIRS: Final[tuple[Path, ...]] = (
    Path("/Applications"),
    Path("/System/Applications"),
    Path("/System/Cryptexes/App/System/Applications"),
    Path.home() / "Applications",
)


class InstalledBrowser(FrozenModel):
    """One browser a sign-in can be opened in."""

    browser_id: str = Field(description="Stable id the setting stores: the app bundle path, or the executable path")
    label: str = Field(description="What Settings shows")
    command_template: str = Field(description="A `webbrowser` command line with `%s` for the URL")


@pure
def _first_per_label(browsers: Sequence[InstalledBrowser]) -> tuple[InstalledBrowser, ...]:
    seen_labels: set[str] = set()
    unique: list[InstalledBrowser] = []
    for browser in browsers:
        if browser.label not in seen_labels:
            seen_labels.add(browser.label)
            unique.append(browser)
    return tuple(unique)


def _find_macos_browsers() -> tuple[InstalledBrowser, ...]:
    found: list[InstalledBrowser] = []
    for label, bundle_name in _MACOS_BROWSER_APPS:
        for directory in _MACOS_APPLICATION_DIRS:
            app_path = directory / bundle_name
            if app_path.is_dir():
                found.append(
                    InstalledBrowser(
                        browser_id=str(app_path),
                        label=label,
                        command_template=f"open -a {shlex.quote(str(app_path))} %s",
                    )
                )
    return _first_per_label(found)


def _find_linux_browsers() -> tuple[InstalledBrowser, ...]:
    found: list[InstalledBrowser] = []
    for label, executable in _LINUX_BROWSER_EXECUTABLES:
        path = shutil.which(executable)
        if path is not None:
            # The trailing `&` makes `webbrowser` start it detached, so it outlives nothing of ours.
            found.append(InstalledBrowser(browser_id=path, label=label, command_template=f"{shlex.quote(path)} %s &"))
    return _first_per_label(found)


def list_installed_browsers() -> tuple[InstalledBrowser, ...]:
    """The browsers found on this machine, in a stable order; empty where none are recognized."""
    match platform.system():
        case "Darwin":
            return _find_macos_browsers()
        case "Linux":
            return _find_linux_browsers()
        case _:
            return ()


def open_url_in_browser(url: str, browser_id: str | None) -> bool:
    """Open ``url`` in the chosen browser, or the default one; whether a browser took it."""
    chosen = next((browser for browser in list_installed_browsers() if browser.browser_id == browser_id), None)
    if browser_id is not None and chosen is None:
        logger.warning("The browser chosen for sign-ins ({}) is no longer installed; using the default", browser_id)
    if chosen is None:
        return webbrowser.open(url)
    return webbrowser.get(chosen.command_template).open(url)


class SignInBrowsersInterface(MutableModel, ABC):
    """The browsers a provider sign-in can open in, and opening one there."""

    @abstractmethod
    def list_browsers(self) -> tuple[InstalledBrowser, ...]:
        """The browsers found on this machine, in a stable order."""

    @abstractmethod
    def open_sign_in_url(self, url: str, browser_id: str | None) -> bool:
        """Open ``url`` in the browser ``browser_id`` names, or the default one; whether a browser took it."""


class InstalledSignInBrowsers(SignInBrowsersInterface):
    """The browsers installed on this machine."""

    def list_browsers(self) -> tuple[InstalledBrowser, ...]:
        return list_installed_browsers()

    def open_sign_in_url(self, url: str, browser_id: str | None) -> bool:
        return open_url_in_browser(url, browser_id)
