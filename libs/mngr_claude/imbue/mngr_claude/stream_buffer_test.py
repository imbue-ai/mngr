from imbue.mngr_claude.stream_buffer import SnapshotDeltaReader
from imbue.mngr_claude.stream_buffer import buffer_body
from imbue.mngr_claude.stream_buffer import compute_stream_delta


def test_buffer_body_strips_id_line() -> None:
    assert buffer_body("uuid-1\nline one\nline two") == "line one\nline two"


def test_buffer_body_id_line_only_is_empty() -> None:
    assert buffer_body("uuid-1") == ""
    assert buffer_body("uuid-1\n") == ""


def test_compute_stream_delta_single_partial_line_held_back() -> None:
    # A single, still-streaming line (no newline yet) is withheld during streaming.
    delta, emitted = compute_stream_delta("uuid-1\nHello world", "", is_flush=False)
    assert delta == ""
    assert emitted == ""


def test_compute_stream_delta_emits_complete_lines_only() -> None:
    # Only the complete line (up to the last newline) is emitted; "line two" held.
    delta, emitted = compute_stream_delta("uuid-1\nline one\nline two", "", is_flush=False)
    assert delta == "line one\n"
    assert emitted == "line one\n"


def test_compute_stream_delta_prefix_extension_complete_lines() -> None:
    delta, emitted = compute_stream_delta("uuid-1\nline one\nline two\nline three", "line one\n", is_flush=False)
    assert delta == "line two\n"
    assert emitted == "line one\nline two\n"


def test_compute_stream_delta_no_change() -> None:
    delta, emitted = compute_stream_delta("uuid-1\nline one\nstreaming", "line one\n", is_flush=False)
    assert delta == ""
    assert emitted == "line one\n"


def test_compute_stream_delta_flush_emits_final_line() -> None:
    # At flush (turn end) the withheld last line is delivered.
    delta, emitted = compute_stream_delta("uuid-1\nline one\nline two", "line one\n", is_flush=True)
    assert delta == "line two"
    assert emitted == "line one\nline two"


def test_compute_stream_delta_new_message_emits_after_common_prefix() -> None:
    # A new message sharing no prefix is emitted whole.
    delta, emitted = compute_stream_delta("uuid-2\nBrand new reply\n", "Old reply\n", is_flush=False)
    assert delta == "Brand new reply\n"
    assert emitted == "Brand new reply\n"


def test_compute_stream_delta_divergence_does_not_reemit_common_prefix() -> None:
    # The blank line after a horizontal rule collapses as the first paragraph
    # streams in, so the body diverges from what was emitted. Only the text past
    # the common prefix is emitted -- the title/rule are not re-printed.
    emitted = "Title\n\n---\n\n"
    delta, new_emitted = compute_stream_delta(
        "id\nTitle\n\n---\nFor years he kept the light.\nmore", emitted, is_flush=False
    )
    assert delta == "For years he kept the light.\n"
    assert new_emitted == "Title\n\n---\nFor years he kept the light.\n"


def test_compute_stream_delta_blank_line_collapse_does_not_reemit_already_emitted_paragraph() -> None:
    # Regression: the paragraph after a horizontal rule has ALREADY been emitted (it
    # is part of emitted_body). When Claude collapses the blank line between the rule
    # and the paragraph as later text streams in, the body diverges from emitted_body,
    # but the paragraph must NOT be printed a second time (plain text cannot be
    # unprinted). The previous char-level common-prefix logic re-emitted everything
    # past the collapsed blank line, duplicating the paragraph.
    emitted = "Some earlier text.\n\n---\n\nHer name was Sefa.\n"
    delta, new_emitted = compute_stream_delta(
        "id\nSome earlier text.\n\n---\nHer name was Sefa.\nstreaming tail", emitted, is_flush=False
    )
    assert delta == ""
    assert new_emitted == emitted


def test_compute_stream_delta_blank_line_collapse_emits_only_new_tail() -> None:
    # After the blank-line reflow, any genuinely new content past the already-emitted
    # text is still emitted (and only that new content).
    emitted = "Title\n\n---\n\nFirst paragraph.\n"
    delta, new_emitted = compute_stream_delta(
        "id\nTitle\n\n---\nFirst paragraph.\nSecond paragraph.\ntail", emitted, is_flush=False
    )
    assert delta == "Second paragraph.\n"
    assert new_emitted == "Title\n\n---\nFirst paragraph.\nSecond paragraph.\n"


def test_compute_stream_delta_sequence_with_rule_reflow_emits_paragraph_once() -> None:
    # Drive the consumer's poll loop over a snapshot sequence where the blank line
    # around a horizontal rule collapses while the following paragraph streams in.
    # The paragraph must appear exactly once across the concatenated deltas.
    paragraph = "Her name was Sefa, and she came from the open west."
    snapshots = [
        "id\nEarlier.\n\n---\n",
        f"id\nEarlier.\n\n---\n\n{paragraph}\nstreaming...",
        f"id\nEarlier.\n\n---\n{paragraph}\nmore streaming...",
    ]
    emitted = ""
    output_parts: list[str] = []
    for snapshot in snapshots:
        delta, emitted = compute_stream_delta(snapshot, emitted, is_flush=False)
        output_parts.append(delta)
    assert "".join(output_parts).count(paragraph) == 1


def test_compute_stream_delta_empty_body_after_idle() -> None:
    # When the watcher empties the body at turn end, only the id line remains.
    delta, emitted = compute_stream_delta("uuid-1", "previous text", is_flush=False)
    assert delta == ""
    assert emitted == "previous text"


def test_compute_stream_delta_stale_shorter_snapshot_ignored() -> None:
    delta, emitted = compute_stream_delta("uuid-1\nline one\n", "line one\nline two\n", is_flush=True)
    assert delta == ""
    assert emitted == "line one\nline two\n"


# =============================================================================
# Tests for SnapshotDeltaReader (the LiveOutputReader wrapper)
# =============================================================================


def test_snapshot_reader_feed_emits_complete_lines_and_finalize_flushes_tail() -> None:
    reader = SnapshotDeltaReader()
    # The still-streaming final line is withheld during feed...
    assert reader.feed("uuid-1\nline one\nline two") == ["line one\n"]
    # ...and released by finalize (turn end), which also resets for the next turn.
    assert reader.finalize() == ["line two"]
    assert reader.emitted_body == ""
    assert reader.last_content == ""


def test_snapshot_reader_finalize_uses_last_nonempty_snapshot() -> None:
    # The watcher empties the buffer when the agent goes idle; finalize must diff
    # against the last snapshot that carried a body, not the post-idle empty read.
    reader = SnapshotDeltaReader()
    assert reader.feed("uuid-1\nonly line") == []
    assert reader.feed("uuid-1") == []
    assert reader.finalize() == ["only line"]


def test_snapshot_reader_feed_emits_only_new_tail_across_polls() -> None:
    reader = SnapshotDeltaReader()
    parts = [
        reader.feed("id\nline one\nstreaming"),
        reader.feed("id\nline one\nline two\nstreaming"),
        reader.feed("id\nline one\nline two\nline three\nstreaming"),
    ]
    assert parts == [["line one\n"], ["line two\n"], ["line three\n"]]
    assert reader.finalize() == ["streaming"]
