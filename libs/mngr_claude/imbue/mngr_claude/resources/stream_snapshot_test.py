from pathlib import Path

import imbue.mngr_claude.resources as resources_package
from imbue.mngr_claude.resources import stream_snapshot

_FIXTURE_DIR = Path(resources_package.__file__).parent / "test_fixtures"


def _read_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


def _render_full(fixture_name: str, ingest_count: int) -> str:
    block = stream_snapshot.extract_latest_assistant_block(_read_fixture(fixture_name))
    assert block is not None
    state = stream_snapshot.StreamBufferState()
    conversion = stream_snapshot.convert_block_to_markdown(block)
    for _ in range(ingest_count):
        state.ingest_block(conversion, has_marker=True)
    return stream_snapshot.format_buffer("uuid-test", state.body_lines)


def test_strip_ansi_removes_csi_and_osc() -> None:
    text = "\x1b[1mbold\x1b[0m \x1b]8;id=1;https://x.com\x1b\\link\x1b]8;;\x1b\\"
    assert stream_snapshot.strip_ansi(text) == "bold link"


def test_assistant_marker_is_achromatic_white() -> None:
    # The real assistant marker uses 256-color 231 (default text color, dark mode).
    line = "\x1b[38;5;231m\x1b[49m●\x1b[39m hello"
    assert stream_snapshot._marker_prefix_is_assistant(line)


def test_tool_marker_is_chromatic_and_rejected() -> None:
    # The real tool-call marker uses 256-color 114 (green).
    line = "\x1b[38;5;114m●\x1b[39m Skill(blueprint-generate)"
    assert stream_snapshot._line_is_any_marker(line)
    assert not stream_snapshot._marker_prefix_is_assistant(line)


def test_status_marker_mid_gray_is_rejected() -> None:
    # The footer status dot uses 256-color 246 (mid gray).
    line = "          \x1b[38;5;246m● high · /effort"
    assert stream_snapshot._line_is_any_marker(line)
    assert not stream_snapshot._marker_prefix_is_assistant(line)


def test_default_foreground_marker_accepted() -> None:
    # A light-mode terminal would render the marker in default/black.
    assert stream_snapshot._marker_prefix_is_assistant("\x1b[39m● text")
    assert stream_snapshot._marker_prefix_is_assistant("\x1b[38;5;16m● text")


def test_render_inline_bold_italic_link_code() -> None:
    state = stream_snapshot._InlineState()
    line = (
        "This is \x1b[1mbold\x1b[0m and \x1b[3mitalic\x1b[0m, a link to "
        "\x1b[94m\x1b]8;id=1;https://anthropic.com\x1b\\Anthropic\x1b[39m\x1b]8;;\x1b\\"
        " and \x1b[38;5;153mfoo()\x1b[39m code."
    )
    rendered = stream_snapshot.render_inline_line(line, state)
    assert rendered == "This is **bold** and *italic*, a link to [Anthropic](https://anthropic.com) and `foo()` code."


def test_render_inline_no_empty_markers_across_lines() -> None:
    # Bold opened on a heading line and closed at the start of the next line must
    # not leave a spurious "****" behind.
    state = stream_snapshot._InlineState()
    first = stream_snapshot.render_inline_line("\x1b[1mMarkdown Demo", state)
    assert first == "**Markdown Demo**"
    second = stream_snapshot.render_inline_line("\x1b[0mThis is plain.", state)
    assert second == "This is plain."


def test_convert_table_to_pipes() -> None:
    table = [
        "┌─┬─┐",
        "│ Name │ Value │",
        "├─┼─┤",
        "│ alpha │ 1 │",
        "└─┴─┘",
    ]
    assert stream_snapshot.convert_table(table) == [
        "| Name | Value |",
        "| --- | --- |",
        "| alpha | 1 |",
    ]


def test_extract_message_region_marker_anchored() -> None:
    pane = "\x1b[38;5;231m\x1b[49m●\x1b[39m First line.\n  Second line.\n\n────────\n❯ "
    result = stream_snapshot.extract_message_region(pane)
    assert result is not None
    lines, has_marker = result
    assert has_marker is True
    assert [stream_snapshot.strip_ansi(line) for line in lines] == ["First line.", "Second line."]


def test_extract_message_region_markerless_tail() -> None:
    # A scrolled message: no marker visible, just the indented tail above the footer.
    pane = (
        "  40. the tunnel led down.\n"
        "  41. cold air poured out.\n"
        "  42. they walked in silence.\n"
        "\n"
        "✻ Thinking…\n"
        "────────\n"
        "❯ \n"
        "  [12:00 user@host /x] branch\n"
    )
    result = stream_snapshot.extract_message_region(pane)
    assert result is not None
    lines, has_marker = result
    assert has_marker is False
    assert [stream_snapshot.strip_ansi(line) for line in lines] == [
        "40. the tunnel led down.",
        "41. cold air poured out.",
        "42. they walked in silence.",
    ]


def test_extract_stops_at_circle_off_shell_spinner() -> None:
    # A tool call begins as the message ends; its grey marker circle is off, so the
    # spinner line looks like an indented continuation. It (and the tool output
    # below it) must not be captured as assistant text -- the "⎿" connector ends
    # the block and drops the spinner line above it.
    pane = (
        "\x1b[38;5;231m●\x1b[39m The answer is below.\n"
        "  Here are the details.\n"
        "  Running 1 shell command…\n"
        "  ⎿  $ cd /home/user/project\n"
        "     echo hello\n"
        "\n"
        "────────\n"
    )
    result = stream_snapshot.extract_message_region(pane)
    assert result is not None
    lines, _ = result
    assert [stream_snapshot.strip_ansi(line) for line in lines] == [
        "The answer is below.",
        "Here are the details.",
    ]


def test_extract_drops_circle_off_tool_marker_before_connector() -> None:
    # For a non-shell tool the spinner text differs, but the "⎿" connector still
    # ends the block and drops the circle-off marker line above it.
    pane = "\x1b[38;5;231m●\x1b[39m Reading the file now.\n  Reading config.toml…\n  ⎿  Read 12 lines\n\n────────\n"
    result = stream_snapshot.extract_message_region(pane)
    assert result is not None
    lines, _ = result
    assert [stream_snapshot.strip_ansi(line) for line in lines] == ["Reading the file now."]


def test_extract_keeps_assistant_text_containing_running() -> None:
    # Ordinary prose that merely starts with "Running" must not be truncated; only
    # the "⎿" connector ends the block, and there is none here.
    pane = "\x1b[38;5;231m●\x1b[39m A short tale.\n  Running water flowed past the old mill all night.\n\n────────\n"
    result = stream_snapshot.extract_message_region(pane)
    assert result is not None
    lines, _ = result
    assert [stream_snapshot.strip_ansi(line) for line in lines] == [
        "A short tale.",
        "Running water flowed past the old mill all night.",
    ]


def test_compute_overlap() -> None:
    assert stream_snapshot.compute_overlap(["a", "b", "c"], ["b", "c", "d"]) == 2
    assert stream_snapshot.compute_overlap(["a", "b"], ["a", "b", "c", "d"]) == 2
    assert stream_snapshot.compute_overlap(["a", "b"], ["x", "y"]) == 0


def test_stitch_appends_revealed_tail() -> None:
    state = stream_snapshot.StreamBufferState()
    state._prose_lines = ["a", "b", "c"]
    state._merge_prose(["b", "c", "d", "e"], has_marker=False)
    assert state.body_lines == ["a", "b", "c", "d", "e"]


def test_stitch_marker_no_overlap_resets_to_new_message() -> None:
    state = stream_snapshot.StreamBufferState()
    state._prose_lines = ["old", "message"]
    state._merge_prose(["brand", "new"], has_marker=True)
    assert state.body_lines == ["brand", "new"]


def test_stitch_markerless_no_overlap_keeps_body() -> None:
    # A marker-less region with no overlap is lost continuity (or unrelated
    # content); it must NOT reset the accumulated body.
    state = stream_snapshot.StreamBufferState()
    state._prose_lines = ["old", "message"]
    state._merge_prose(["unrelated", "tail"], has_marker=False)
    assert state.body_lines == ["old", "message"]


def test_stitch_markerless_does_not_start_empty_body() -> None:
    # Without a marker and with nothing to overlap, there is no anchor to start.
    state = stream_snapshot.StreamBufferState()
    state._merge_prose(["some", "tail"], has_marker=False)
    assert state.body_lines == []


def test_stitch_markerless_ignores_stale_shorter_snapshot() -> None:
    # A transient shorter marker-less snapshot must not shrink the body (an
    # alignment that would shorten the accumulated body is rejected).
    state = stream_snapshot.StreamBufferState()
    state._prose_lines = ["a", "b", "c", "d"]
    state._merge_prose(["a", "b"], has_marker=False)
    assert state.body_lines == ["a", "b", "c", "d"]


def test_stitch_marker_anchored_takes_full_region() -> None:
    # A marker-anchored region is the full current message; it replaces the body.
    state = stream_snapshot.StreamBufferState()
    state._prose_lines = ["a", "b"]
    state._merge_prose(["a", "b", "c", "d"], has_marker=True)
    assert state.body_lines == ["a", "b", "c", "d"]


def test_stitch_markerless_tolerates_volatile_last_row() -> None:
    # The body's last row is volatile (Claude re-wraps the streaming row). A
    # marker-less region re-includes a changed version of it plus new rows;
    # alignment must drop the volatile row and continue rather than freeze.
    state = stream_snapshot.StreamBufferState()
    state._prose_lines = ["L1", "L2", "L3", "L4", "L5-old"]
    state._merge_prose(["L3", "L4", "L5-new", "L6"], has_marker=False)
    assert state.body_lines == ["L1", "L2", "L3", "L4", "L5-new", "L6"]


def test_table_render_is_monotonic_as_rows_arrive() -> None:
    # Header-only table then full table: the body must only grow (prefix-extend),
    # never re-emit the prose or duplicate the table.
    header_only = stream_snapshot.BlockConversion(
        lines=["Some prose."],
        pending_table=["┌─┬─┐", "│ A │ B │", "├─┼─┤", "└─┴─┘"],
    )
    full = stream_snapshot.BlockConversion(
        lines=["Some prose."],
        pending_table=["┌─┬─┐", "│ A │ B │", "├─┼─┤", "│ 1 │ 2 │", "└─┴─┘"],
    )
    state = stream_snapshot.StreamBufferState()
    # First sighting of the table is deferred.
    state.ingest_block(header_only, has_marker=True)
    assert "| A | B |" not in "\n".join(state.body_lines)
    # Stable across two polls: the header-only table resolves.
    state.ingest_block(header_only, has_marker=True)
    after_header = state.body_lines
    assert "| A | B |" in "\n".join(after_header)
    # The table grew (new signature), so it defers again, then resolves when stable.
    state.ingest_block(full, has_marker=True)
    state.ingest_block(full, has_marker=True)
    after_full = state.body_lines
    # Body grew monotonically: the header-only render is a prefix of the full render.
    assert after_full[: len(after_header)] == after_header
    assert "| 1 | 2 |" in "\n".join(after_full)
    # Prose appears exactly once (no duplication).
    assert "\n".join(after_full).count("Some prose.") == 1


def test_format_buffer_id_line_and_trailing_blank_trim() -> None:
    assert stream_snapshot.format_buffer("id-1", ["line", "", ""]) == "id-1\nline"
    assert stream_snapshot.format_buffer("", []) == ""


def test_full_fixture_reconstructs_markdown() -> None:
    # Two ingests so the (stable) trailing table resolves.
    rendered = _render_full("stream_markdown_demo_full.txt", ingest_count=2)
    assert "**Markdown Demo**" in rendered
    assert "This is **bold** and *italic* text, with a link to [Anthropic](https://anthropic.com)." in rendered
    assert "- First item" in rendered
    assert "1. Step one" in rendered
    assert "> This is a blockquote." in rendered
    assert "Call `foo()` inline." in rendered
    assert "```" in rendered
    assert "def greet(name):" in rendered
    assert "| Name | Type | Value |" in rendered
    assert "| --- | --- | --- |" in rendered
    assert '| beta | str | "hi" |' in rendered


def test_pretable_fixture_streams_only_what_is_visible() -> None:
    rendered = _render_full("stream_markdown_demo_pretable.txt", ingest_count=1)
    assert "**Markdown Demo**" in rendered
    assert "| Name |" not in rendered


def test_table_deferred_until_stable_across_polls() -> None:
    block = stream_snapshot.extract_latest_assistant_block(_read_fixture("stream_markdown_demo_table_building.txt"))
    assert block is not None
    conversion = stream_snapshot.convert_block_to_markdown(block)
    assert conversion.pending_table is not None

    state = stream_snapshot.StreamBufferState()
    state.ingest_block(conversion, has_marker=True)
    first = stream_snapshot.format_buffer("id", state.body_lines)
    # First poll: the (possibly still-growing) table is withheld.
    assert "| Name |" not in first
    assert "Call `foo()` inline." in first

    state.ingest_block(conversion, has_marker=True)
    second = stream_snapshot.format_buffer("id", state.body_lines)
    # Second identical poll: the table is stable and is appended.
    assert "| Name | Type | Value |" in second


def test_streaming_sequence_appends_without_reset() -> None:
    state = stream_snapshot.StreamBufferState()
    for fixture in (
        "stream_markdown_demo_pretable.txt",
        "stream_markdown_demo_table_building.txt",
        "stream_markdown_demo_full.txt",
        "stream_markdown_demo_full.txt",
    ):
        block = stream_snapshot.extract_latest_assistant_block(_read_fixture(fixture))
        assert block is not None
        state.ingest_block(stream_snapshot.convert_block_to_markdown(block), has_marker=True)
    rendered = stream_snapshot.format_buffer("final-id", state.body_lines)
    # The heading from the first snapshot survives all the way through.
    assert rendered.count("**Markdown Demo**") == 1
    assert "| gamma | float | 3.14 |" in rendered


def test_read_last_complete_assistant_id(tmp_path: Path) -> None:
    transcript = tmp_path / "events.jsonl"
    transcript.write_text(
        '{"type": "user", "uuid": "u1"}\n'
        '{"type": "assistant", "uuid": "a1"}\n'
        '{"type": "assistant", "uuid": "a2"}\n'
        "not json\n",
        encoding="utf-8",
    )
    assert stream_snapshot._read_last_complete_assistant_id(transcript) == "a2"


def test_read_last_complete_assistant_id_missing_file(tmp_path: Path) -> None:
    assert stream_snapshot._read_last_complete_assistant_id(tmp_path / "nope.jsonl") == ""


def test_agent_pane_target_addresses_window_by_name() -> None:
    """The pane is targeted by the primary window name (not the literal :0 index), with
    the `=` exact-match prefix, so capture is correct regardless of the user's base-index."""
    assert stream_snapshot._agent_pane_target("mngr-my-agent", "agent") == "=mngr-my-agent:agent"
    target = stream_snapshot._agent_pane_target("mngr-my-agent", "primary")
    assert target == "=mngr-my-agent:primary"
    assert ":0" not in target
