import json
from collections.abc import Callable
from collections.abc import Iterable
from typing import TypeVar
from typing import assert_never

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.primitives import OutputFormat


class TargetOutcome(FrozenModel):
    """One target's outcome in a multi-target read; a subclass adds the payload the target produced."""

    target: str = Field(description="The target as it was addressed")
    agent_id: str | None = Field(description="The agent the target resolved to, if it resolved to one")
    name: str | None = Field(description="The agent (or host) the target resolved to, if it resolved")
    error: str | None = Field(description="Why the target produced no payload, or None when it did")


TargetOutcomeT = TypeVar("TargetOutcomeT", bound=TargetOutcome)


def emit_target_outcomes(
    outcomes: Iterable[TargetOutcomeT],
    output_format: OutputFormat,
    # Called with each outcome and whether no earlier target produced a payload
    write_human_block: Callable[[TargetOutcomeT, bool], None],
) -> bool:
    """Write each target's outcome in the requested format, returning whether any target failed.

    JSONL writes each record as soon as ``outcomes`` yields it, so a caller that
    has to cut the run short still holds every target that finished before it did.
    JSON writes the whole array once every target is done.
    """
    emitted: list[TargetOutcomeT] = []
    for outcome in outcomes:
        match output_format:
            case OutputFormat.JSONL:
                write_json_line(outcome.model_dump())
            case OutputFormat.HUMAN:
                write_human_block(outcome, not any(previous.error is None for previous in emitted))
            case OutputFormat.JSON:
                pass
            case _ as unreachable:
                assert_never(unreachable)
        emitted.append(outcome)
    if output_format == OutputFormat.JSON:
        write_human_line("{}", json.dumps([outcome.model_dump() for outcome in emitted], indent=2))
    return any(outcome.error is not None for outcome in emitted)
