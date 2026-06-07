from imbue.mngr_robinhood.stream_buffer import buffer_body
from imbue.mngr_robinhood.stream_buffer import compute_stream_delta


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


def test_compute_stream_delta_empty_body_after_idle() -> None:
    # When the watcher empties the body at turn end, only the id line remains.
    delta, emitted = compute_stream_delta("uuid-1", "previous text", is_flush=False)
    assert delta == ""
    assert emitted == "previous text"


def test_compute_stream_delta_stale_shorter_snapshot_ignored() -> None:
    delta, emitted = compute_stream_delta("uuid-1\nline one\n", "line one\nline two\n", is_flush=True)
    assert delta == ""
    assert emitted == "line one\nline two\n"
