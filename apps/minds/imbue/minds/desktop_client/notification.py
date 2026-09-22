"""OS-banner dispatch for the minds desktop client.

The one delivery channel is the Electron main process: a ``notification``
JSONL event on stdout, which main renders as a native notification and routes
the click of. Outside Electron (a bare ``minds run``) nothing reaches the OS;
the in-app feed still records every entry.
"""

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.ui_models import UiNotificationEntry
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import emit_event


class NotificationRequest(FrozenModel):
    """One OS banner, laid out the way Slack lays its banners out."""

    title: str = Field(description="Top line: the workspace (or account) the banner is about")
    subtitle: str = Field(description="Second line: the headline (chat name, request title, or event name)")
    body: str = Field(description="Detail text")
    url: str | None = Field(
        default=None,
        description="SPA path the click lands on; None just raises the app",
    )
    entry: UiNotificationEntry | None = Field(
        default=None,
        description="Feed entry to open and acknowledge through the renderer's shared notification action",
    )


class NotificationDispatcher(FrozenModel):
    """Hands banners to the Electron main process over stdout."""

    is_electron: bool = Field(description="Whether the server is running inside the desktop app")

    def dispatch(self, request: NotificationRequest) -> None:
        """Emit the banner for Electron, or log why nothing reached the OS."""
        if not self.is_electron:
            logger.debug("notification {!r}: not running inside Electron, nothing reaches the OS", request.subtitle)
            return
        data: dict[str, object] = {
            "title": request.title,
            "subtitle": request.subtitle,
            "body": request.body,
        }
        if request.url is not None:
            data["url"] = request.url
        if request.entry is not None:
            data["entry"] = request.entry.model_dump(mode="json")
        logger.debug(
            "Emitting notification event for Electron: title={!r} subtitle={!r}", request.title, request.subtitle
        )
        emit_event("notification", data, OutputFormat.JSONL)
