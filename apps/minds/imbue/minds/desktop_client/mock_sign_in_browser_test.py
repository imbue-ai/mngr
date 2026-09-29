from pydantic import Field
from pydantic import PrivateAttr

from imbue.minds.desktop_client.sign_in_browser import InstalledBrowser
from imbue.minds.desktop_client.sign_in_browser import SignInBrowsersInterface


class RecordingSignInBrowsers(SignInBrowsersInterface):
    """A described machine's browsers: records each page it was asked to open, and whether one took it."""

    browsers: tuple[InstalledBrowser, ...] = Field(default=(), description="The browsers this machine has")
    is_opening: bool = Field(default=True, description="What every open reports")

    _opened: list[tuple[str, str | None]] = PrivateAttr(default_factory=list)

    def list_browsers(self) -> tuple[InstalledBrowser, ...]:
        return self.browsers

    def open_sign_in_url(self, url: str, browser_id: str | None) -> bool:
        self._opened.append((url, browser_id))
        return self.is_opening

    def opened(self) -> list[tuple[str, str | None]]:
        return list(self._opened)
