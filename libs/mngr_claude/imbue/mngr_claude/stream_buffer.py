"""Parsing/diffing of the claude agent's ``stream_buffer`` snapshots into text deltas.

``stream_buffer`` is written by the mngr_claude tmux watcher (``stream_snapshot.py``): line 1 is the
uuid of the last *complete* assistant message (empty if none yet) and lines 2+ are the in-progress
assistant text, reverse-mapped to approximate markdown and strict-appended within a message. The
watcher overwrites the whole file with a fresh snapshot each tick, so "new" text is the diff of the
body against what has already been emitted.

:class:`SnapshotDeltaReader` adapts that diff to the shared :class:`LiveOutputReader` contract; the
pure ``compute_stream_delta`` / ``buffer_body`` helpers it wraps are also used directly by tests and
remain the single source of the reflow-tolerant diff logic. Consumers: the robinhood CLI
orchestrator (renders deltas as plain text / ``text_delta`` partials), the mngr-backed Agent SDK
driver (wraps deltas as synthesized ``StreamEvent`` payloads), and -- via the reader -- any pull
consumer tailing the buffer through ``tail_live_output``.
"""

from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.live_output import LiveOutputReader


@pure
def buffer_body(buffer_content: str) -> str:
    """Return the body (everything after the id line) of a stream_buffer snapshot."""
    lines = buffer_content.split("\n")
    return "\n".join(lines[1:]) if len(lines) > 1 else ""


@pure
def compute_stream_delta(buffer_content: str, emitted_body: str, is_flush: bool) -> tuple[str, str]:
    """Compute the new assistant-text delta from a stream_buffer snapshot.

    The buffer's first line is the last-complete-assistant id; the remaining lines are the
    in-progress body. Returns ``(delta, new_emitted_body)``.

    When ``is_flush`` is False, only the *complete* lines are considered (the last, still-streaming
    line is withheld) so the churning tail is never emitted mid-stream. When ``is_flush`` is True
    (turn end), the whole body is considered so the final line is delivered. A prefix-extension of
    ``emitted_body`` yields just the appended suffix; a non-prefix body (a new message) yields the
    whole new body.
    """
    body = buffer_body(buffer_content)
    visible = body if is_flush else _complete_lines_prefix(body)
    if visible == "" or visible == emitted_body:
        return "", emitted_body
    if visible.startswith(emitted_body):
        return visible[len(emitted_body) :], visible
    # A stale, shorter snapshot that the emitted text already covers: keep going.
    if emitted_body.startswith(visible):
        return "", emitted_body
    # Divergence: the body is neither an extension nor a prefix of what we've emitted. This happens
    # when the rendered text reflows (e.g. Claude collapses a blank line around a horizontal rule as
    # the following paragraph streams in) and -- across turns -- when a new message begins. Emit only
    # the content past what we have already emitted, treating whitespace runs as equivalent so a
    # reflowed-but-already-printed region is recognized as already emitted rather than re-printed.
    # Plain-text output cannot be unprinted, so re-emitting that region would duplicate everything
    # from the reflow point onward (this is the source of the duplicated paragraphs seen after a
    # horizontal rule). At worst a little already-printed whitespace (a collapsed blank line) is left
    # stale; if no new content remains, keep the existing emitted body as the baseline.
    suffix_start = _unemitted_suffix_start(emitted_body, visible)
    delta = visible[suffix_start:]
    if delta == "":
        return "", emitted_body
    return delta, visible


@pure
def _unemitted_suffix_start(emitted_body: str, visible: str) -> int:
    """Return the index in ``visible`` where genuinely new (un-emitted) content begins.

    Walks ``emitted_body`` and ``visible`` together, consuming a whitespace run in one as matching a
    whitespace run in the other and skipping whitespace present in only one. This absorbs the
    blank-line reflow Claude performs as text streams in (e.g. collapsing the blank line around a
    horizontal rule), so content already emitted under a different line layout is recognized as
    already-emitted rather than re-emitted. Stops at the first non-whitespace character that diverges
    (or when ``emitted_body`` is exhausted) and returns that visible offset.
    """
    emitted_index = 0
    visible_index = 0
    matched_visible_index = 0
    while emitted_index < len(emitted_body) and visible_index < len(visible):
        is_emitted_space = emitted_body[emitted_index].isspace()
        is_visible_space = visible[visible_index].isspace()
        if is_emitted_space and is_visible_space:
            while emitted_index < len(emitted_body) and emitted_body[emitted_index].isspace():
                emitted_index += 1
            while visible_index < len(visible) and visible[visible_index].isspace():
                visible_index += 1
            matched_visible_index = visible_index
        elif is_emitted_space:
            emitted_index += 1
        elif is_visible_space:
            visible_index += 1
            matched_visible_index = visible_index
        elif emitted_body[emitted_index] == visible[visible_index]:
            emitted_index += 1
            visible_index += 1
            matched_visible_index = visible_index
        else:
            break
    return matched_visible_index


@pure
def _complete_lines_prefix(body: str) -> str:
    """Return ``body`` up to and including its last newline (the complete lines).

    The text after the last newline is the still-streaming line and is withheld. Returns "" when
    there is no newline (only a single, in-progress line so far).
    """
    last_newline = body.rfind("\n")
    if last_newline == -1:
        return ""
    return body[: last_newline + 1]


class SnapshotDeltaReader(LiveOutputReader):
    """Reader for the claude ``stream_buffer`` snapshot (uuid id line + in-progress markdown body).

    Each read is a full snapshot, so new text is the diff of the body against what was already
    emitted. :meth:`feed` withholds the still-streaming final line (so its churn is never emitted
    mid-stream); :meth:`finalize` releases it from the most recent non-empty snapshot and resets, so
    the same reader can be reused across the next message/turn.
    """

    emitted_body: str = ""
    last_content: str = ""

    def feed(self, content: str) -> list[str]:
        # Track the most recent non-empty snapshot: the watcher empties the
        # buffer when the agent goes idle, so finalize() must diff against the
        # last content that actually carried a body, not a post-turn empty read.
        if buffer_body(content).strip():
            self.last_content = content
        delta, self.emitted_body = compute_stream_delta(content, self.emitted_body, is_flush=False)
        return [delta] if delta else []

    def finalize(self) -> list[str]:
        delta, self.emitted_body = compute_stream_delta(self.last_content, self.emitted_body, is_flush=True)
        self.emitted_body = ""
        self.last_content = ""
        return [delta] if delta else []
