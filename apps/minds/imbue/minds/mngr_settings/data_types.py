from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class CloudAccountRecord(FrozenModel):
    """One registered bring-your-own-key cloud account, as read from a ``byok-*`` provider block."""

    name: str = Field(description="The provider block name (byok-<backend>-<slug>)")
    alias: str = Field(description="Display name: the slug of the user's chosen alias")
    backend: str = Field(description="The cloud backend (aws, gcp, or azure)")
    region: str = Field(description="Pinned region (zone for GCP); empty if the block lacks one")
    identifier: str = Field(description="Masked, display-safe credential hint; never a secret")
