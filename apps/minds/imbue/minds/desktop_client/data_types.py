"""Frozen domain objects for the desktop client."""

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class RemoteWorkspaceTile(FrozenModel):
    """A workspace known only from another device's synced record, for the landing list."""

    agent_id: str = Field(description="The workspace agent id (drives backup status)")
    name: str = Field(description="Display name from the record")
    accent: str = Field(description="Accent color hex")
    location: str = Field(description="Where it lives (the other device's label, or a provider name)")
    host_id: str = Field(description="The record's host id (drives remove-from-list)")
    state: str = Field(
        default="",
        description="Derived access state: '' (plain), 'connecting', 'unreachable', or 'error'",
    )
    state_detail: str | None = Field(default=None, description="Failure detail for the 'error' state (chip tooltip)")
