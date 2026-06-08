from enum import auto
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import TmuxHeight
from imbue.mngr.primitives import TmuxWidth
from imbue.mngr.primitives import TmuxWindowSize

# Default tmux window dimensions for robinhood-spawned agents. Large and pinned
# ("manual") so the agent's pane is wide enough that the live-streamed text (which
# is reverse-mapped from the rendered tmux pane) is not chopped into hard line
# wraps at a narrow width.
DEFAULT_ROBINHOOD_TMUX_WIDTH: Final[TmuxWidth] = TmuxWidth(2048)
DEFAULT_ROBINHOOD_TMUX_HEIGHT: Final[TmuxHeight] = TmuxHeight(256)
DEFAULT_ROBINHOOD_TMUX_WINDOW_SIZE: Final[TmuxWindowSize] = TmuxWindowSize.MANUAL


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
    tmux_width: TmuxWidth = Field(
        default=DEFAULT_ROBINHOOD_TMUX_WIDTH,
        description="Width (columns) of the spawned agent's tmux window",
    )
    tmux_height: TmuxHeight = Field(
        default=DEFAULT_ROBINHOOD_TMUX_HEIGHT,
        description="Height (rows) of the spawned agent's tmux window",
    )
    tmux_window_size: TmuxWindowSize = Field(
        default=DEFAULT_ROBINHOOD_TMUX_WINDOW_SIZE,
        description="tmux window resize policy for the spawned agent",
    )
    pass_through_agent_args: tuple[str, ...] = Field(description="Args to forward to the spawned claude")
    positional_prompt: str | None = Field(description="The positional prompt, if any")


class ResultMeta(FrozenModel):
    """Metadata used to synthesize claude's `result` envelope."""

    session_id: str = Field(description="Session/agent identifier (best-effort)")
    duration_ms: int = Field(description="Wall-clock duration of the run")
    is_error: bool = Field(description="Whether the turn ended in an error")
    error_text: str | None = Field(description="Error text if any")
