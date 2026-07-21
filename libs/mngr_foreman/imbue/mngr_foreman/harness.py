"""Per-agent-type transcript strategy: the one seam that makes foreman multi-harness.

Foreman's transcript pipeline -- which mirrored file to follow, how to parse it,
and whether the composer's blocked state comes from a ``tmux`` pane capture -- was
hardcoded to claude. Each harness differs in exactly those three ways, collected
here into one ``TranscriptStrategy`` keyed on ``AgentDetails.type``:

* ``subpath`` -- the mirrored transcript file, relative to the agent state dir.
  Foreman reads ``events_path.parent / subpath``.
* ``parse`` -- newly-completed JSONL lines -> normalized UI event dicts (the shape
  ``static/app.js`` renders).
* ``uses_pane_dialog_detection`` -- claude blocks the composer behind numbered-choice
  TUI dialogs (trust / permission / model picker) detected via ``tmux capture-pane``
  (the ``❯`` rule). Harnesses without such dialogs -- pi runs every tool unattended
  and pre-dismisses its trust prompt -- set this False, so the input-state endpoint
  skips the capture and reports a plain working/waiting.

Adding a harness is one entry in ``_STRATEGIES``; everything else in foreman
(tailer, SSE loop, connection pool, image externalization, composer, terminals) is
already agent-type-agnostic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_foreman.codex_transcript import parse_codex_common_lines
from imbue.mngr_foreman.pi_transcript import parse_pi_common_lines
from imbue.mngr_foreman.transcript_parser import parse_claude_session_lines
from imbue.mngr_foreman.transcript_tail import TRANSCRIPT_SUBPATH

# A parser maps the newly-completed JSONL lines (plus dedup / tool-name-threading
# state) to UI event dicts. The two harnesses' signatures differ slightly -- claude
# threads a tool-name map, pi ignores it -- so this is intentionally loose; both are
# called uniformly (see ``server._transcript_stream``).
ParseFn = Callable[..., list[dict[str, Any]]]

# pi's mngr lifecycle extension emits the agent-agnostic common transcript here
# (``events/<type>/common_transcript/events.jsonl`` with ``<type>`` = ``pi-coding``).
_PI_SUBPATH = "events/pi-coding/common_transcript/events.jsonl"
# codex's mngr plugin emits the same common transcript under its own type dir
# (``mngr_codex``'s ``COMMON_TRANSCRIPT_OUTPUT_RELATIVE``).
_CODEX_SUBPATH = "events/codex/common_transcript/events.jsonl"


class TranscriptStrategy(FrozenModel):
    """How foreman follows and renders one harness's transcript."""

    subpath: str
    parse: ParseFn
    uses_pane_dialog_detection: bool


_STRATEGIES: dict[str, TranscriptStrategy] = {
    # claude: follow the raw session JSONL and parse it; its TUI dialogs gate the composer.
    "claude": TranscriptStrategy(
        subpath=TRANSCRIPT_SUBPATH,
        parse=parse_claude_session_lines,
        uses_pane_dialog_detection=True,
    ),
    # pi-coding: follow the pre-normalized common transcript; pi never blocks the composer.
    "pi-coding": TranscriptStrategy(
        subpath=_PI_SUBPATH,
        parse=parse_pi_common_lines,
        uses_pane_dialog_detection=False,
    ),
    # codex: follow the pre-normalized common transcript. codex has no numbered-choice
    # TUI dialogs; a tool-approval block promotes its lifecycle state RUNNING->WAITING,
    # so the working/waiting dot rides the generic ``active`` marker with no pane scrape.
    "codex": TranscriptStrategy(
        subpath=_CODEX_SUBPATH,
        parse=parse_codex_common_lines,
        uses_pane_dialog_detection=False,
    ),
}


def transcript_strategy_for(agent_type: str) -> TranscriptStrategy | None:
    """Return the transcript strategy for ``agent_type``, or None if unsupported.

    ``AgentDetails.type`` is always the canonical type name (pi's ``pi`` alias is
    resolved to ``pi-coding`` upstream), so a plain dict lookup is enough.
    """
    return _STRATEGIES.get(agent_type)
