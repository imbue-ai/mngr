"""Observation model for ATIF trajectories."""

from pydantic import BaseModel
from pydantic import Field

from imbue.mngr.agents.data_types.atif.observation_result import ObservationResult


class Observation(BaseModel):
    """Environment feedback/result after actions or system events."""

    results: list[ObservationResult] = Field(
        default=...,
        description="Array of result objects from tool calls or actions",
    )

    model_config = {"extra": "forbid"}
