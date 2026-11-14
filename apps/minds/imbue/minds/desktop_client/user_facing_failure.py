"""A failure as the user is told about it, apart from the technical account of it.

An exception's own message is written for a log: a URL, an errno, a host id. A
failure that knows what it means for the user carries one of these as well, so
the surface that reports it can show our sentence and keep the exception's text
for See details.
"""

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.ui_models import ErrorRemedy


class UserFacingFailure(FrozenModel):
    """Our sentence for a failure, and the button that fixes it when the app can run the fix."""

    message: str = Field(description="The sentence on the visible line")
    remedy: ErrorRemedy | None = Field(description="The button offered beside the sentence; None for none")
