"""Per-agent-type transcript/status strategy -- foreman's one multi-harness seam.

Foreman gives a chat UI over a mngr agent's transcript. Three things vary by the
agent's *harness* (its coding CLI); everything else in foreman (the byte-offset
tailer, the SSE loop, the connection pool, image externalization, the composer,
terminals, the agent registry) is already agent-type-agnostic. Those three
knobs live together in one :class:`TranscriptStrategy` keyed on ``agent.type``:

1. **``subpath``** -- where the agent's mirrored transcript file sits under its
   state dir. claude mirrors its *raw* session JSONL at
   ``logs/claude_transcript/events.jsonl``; the common-transcript agents (codex,
   opencode, pi-coding, ...) emit the shared normalized envelope at
   ``events/<type>/common_transcript/events.jsonl``.

2. **``parse``** -- the function turning that file's lines into foreman UI event
   dicts. claude needs its bespoke raw parser
   (:func:`~imbue.mngr_foreman.transcript_parser.parse_claude_session_lines`);
   the common-transcript agents share the thin
   :func:`~imbue.mngr_foreman.common_transcript_parser.parse_common_transcript_lines`
   normalizer. Both take the same keyword arguments so the caller dispatches
   uniformly.

3. **``uses_pane_dialog_detection``** -- whether "needs-input" is found by
   capturing the agent's tmux pane and matching claude's numbered-choice ``❯``
   dialog shape. Only claude drives blocking menus (trust / plan / model picker /
   ``/login``) at run time that lack a marker signal. codex and opencode surface
   a tool-approval block through mngr's own ``waiting_reason == PERMISSIONS``
   field (a free, pane-less signal read off ``AgentDetails``), and their other
   dialogs are pre-dismissed at provisioning -- so they set this False and rely on
   the field alone. (pi-coding has no needs-input state at all; also False.)

Adding a harness is one row here plus, for a new format, one parser module. A
literal dict keyed on ``agent.type`` is the whole registry -- no plugin-style
dynamic discovery is warranted at this size.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_foreman.common_transcript_parser import parse_common_transcript_lines
from imbue.mngr_foreman.transcript_parser import parse_claude_session_lines
from imbue.mngr_foreman.transcript_tail import TRANSCRIPT_SUBPATH

# A transcript parser: (lines, existing_event_ids, tool_name_by_call_id,
# max_tool_output_chars) -> UI event dicts. The claude and common parsers share
# this signature so the SSE loop dispatches without special-casing.
ParseFn = Callable[..., list[dict[str, Any]]]


class TranscriptStrategy(FrozenModel):
    """How to read + classify one agent type's transcript and blocking state.

    ``subpath`` is the transcript file path relative to the agent's state dir.
    ``parse`` maps that file's lines to foreman UI event dicts.
    ``uses_pane_dialog_detection`` is True to also capture the tmux pane and match
    claude's numbered-choice dialog shape for the needs-input state; False to rely
    solely on mngr's pane-less ``waiting_reason`` field (codex/opencode) or to have
    no needs-input state at all (pi-coding).
    """

    subpath: str
    parse: ParseFn
    uses_pane_dialog_detection: bool


def _common_transcript_subpath(agent_type: str) -> str:
    """The shared common-transcript file path for ``agent_type`` under the state dir.

    Every common-transcript agent writes ``events/<type>/common_transcript/events.jsonl``
    (the convention in ``imbue.mngr.api.preservation.build_transcript_preserved_items``).
    """
    return f"events/{agent_type}/common_transcript/events.jsonl"


_STRATEGIES: Final[dict[str, TranscriptStrategy]] = {
    "claude": TranscriptStrategy(
        subpath=TRANSCRIPT_SUBPATH,
        parse=parse_claude_session_lines,
        uses_pane_dialog_detection=True,
    ),
    "codex": TranscriptStrategy(
        subpath=_common_transcript_subpath("codex"),
        parse=parse_common_transcript_lines,
        uses_pane_dialog_detection=False,
    ),
    "opencode": TranscriptStrategy(
        subpath=_common_transcript_subpath("opencode"),
        parse=parse_common_transcript_lines,
        uses_pane_dialog_detection=False,
    ),
}


def transcript_strategy_for(agent_type: str) -> TranscriptStrategy | None:
    """Return the transcript strategy for ``agent_type``, or None if unsupported.

    None means foreman has no chat rendering for that type (the transcript SSE
    sends ``unsupported`` and the input-state route reports not-running); the
    agent still appears in the list and its terminal still works.
    """
    return _STRATEGIES.get(agent_type)
