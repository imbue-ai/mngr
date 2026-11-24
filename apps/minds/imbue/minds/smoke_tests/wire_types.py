"""The connector responses the smoke suite parses into models."""

from enum import auto

from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr_imbue_cloud.wire import WireEnum
from imbue.mngr_imbue_cloud.wire import WireModel


class SmokeWorkspaceRecordState(WireEnum):
    """Lifecycle state of a workspace record, as ``GET /sync/records`` reports it."""

    ACTIVE = auto()
    DESTROYED = auto()
    UNKNOWN = auto()


class SmokeWorkspaceRecord(WireModel):
    """One workspace record of a smoke account, as ``GET /sync/records`` lists it.

    Declares only what the suite reads of a record, so its secrets blob never
    reaches the model, and a record that fails to parse is reported without
    its contents.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    agent_id: NonEmptyStr = Field(description="The workspace's id, which the record's retire route takes.")
    state: SmokeWorkspaceRecordState = Field(
        description="Its lifecycle state; the connector counts only an active record as a workspace of the account."
    )
    host_id: str = Field(default="", description="The host the workspace is on.")
    display_name: str = Field(default="", description="The workspace's name.")
    provider_kind: str = Field(
        default="",
        description="The provider the workspace runs under on its device (``docker``, ``imbue_cloud_<slug>``).",
    )
