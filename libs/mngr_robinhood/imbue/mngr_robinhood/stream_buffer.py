"""Pure parsing/diffing of the claude agent's ``stream_buffer`` snapshots.

``stream_buffer`` is written by the mngr_claude tmux watcher (``stream_snapshot.py``): line 1 is the
uuid of the last *complete* assistant message (empty if none yet) and lines 2+ are the in-progress
assistant text, reverse-mapped to approximate markdown and strict-appended within a message.

These helpers turn successive snapshots into incremental text deltas. They are shared by the
robinhood CLI orchestrator (which renders deltas as plain text / ``text_delta`` partials) and the
mngr-backed Agent SDK driver (which wraps deltas as synthesized ``StreamEvent`` payloads).
"""

from imbue.imbue_common.pure import pure


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
    # when the rendered text shifts slightly (e.g. Claude collapses the blank line around a
    # horizontal rule as a paragraph streams in) and -- across turns -- when a new message begins.
    # Emit only the part of the body past the longest common prefix with what we already emitted,
    # never re-emitting the common prefix (plain-text output cannot be unprinted, so re-emitting
    # would duplicate everything from the divergence point back to the start). At worst a small
    # amount of already-printed text is left stale.
    common = _common_prefix_length(emitted_body, visible)
    return visible[common:], visible


@pure
def _common_prefix_length(first: str, second: str) -> int:
    """Return the length of the longest common prefix of two strings."""
    limit = min(len(first), len(second))
    index = 0
    while index < limit and first[index] == second[index]:
        index += 1
    return index


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
