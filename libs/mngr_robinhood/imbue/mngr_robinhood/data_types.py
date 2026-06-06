from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class InputFormat(UpperCaseStrEnum):
    """How the wrapper reads user-side prompts."""

    TEXT = auto()
    STREAM_JSON = auto()


class OutputFormat(UpperCaseStrEnum):
    """How the wrapper formats the assistant response on stdout."""

    TEXT = auto()
    JSON = auto()
    STREAM_JSON = auto()


class ArgPartition(FrozenModel):
    """Result of splitting the raw argv that follows `mngr robinhood`."""

    input_format: InputFormat = Field(description="Resolved --input-format")
    output_format: OutputFormat = Field(description="Resolved --output-format")
    replay_user_messages: bool = Field(description="Resolved --replay-user-messages")
    include_partial_messages: bool = Field(
        default=False,
        description="Emit claude-native text_delta partial events from the agent's stream_buffer (stream-json only)",
    )
    stream_plain_text: bool = Field(
        default=False,
        description="Stream the assistant's text to stdout incrementally (text output only)",
    )
    pass_through_agent_args: tuple[str, ...] = Field(description="Args to forward to the spawned claude")
    positional_prompt: str | None = Field(description="The positional prompt, if any")


class ResultMeta(FrozenModel):
    """Metadata used to synthesize claude's `result` envelope."""

    session_id: str = Field(description="Session/agent identifier (best-effort)")
    duration_ms: int = Field(description="Wall-clock duration of the run")
    is_error: bool = Field(description="Whether the turn ended in an error")
    error_text: str | None = Field(description="Error text if any")
