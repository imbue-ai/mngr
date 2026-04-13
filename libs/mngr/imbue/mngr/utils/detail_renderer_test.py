"""Unit tests for detail_renderer: ANSI-to-HTML conversion and HTML rendering utilities."""

import base64
from pathlib import Path

from inline_snapshot import snapshot

from imbue.mngr.utils.detail_renderer import ansi_to_html
from imbue.mngr.utils.detail_renderer import render_test_detail
from imbue.mngr.utils.detail_renderer import render_transcript
from imbue.mngr.utils.detail_renderer import render_tutorial_block

# ---------------------------------------------------------------------------
# ansi_to_html
# ---------------------------------------------------------------------------


def test_ansi_to_html_plain_text_is_returned_unchanged() -> None:
    """Plain text without escape sequences passes through unmodified."""
    assert ansi_to_html("hello world") == "hello world"


def test_ansi_to_html_html_special_chars_are_escaped() -> None:
    """Characters that are special in HTML are escaped."""
    assert ansi_to_html('<script>&"') == snapshot("&lt;script&gt;&amp;&quot;")


def test_ansi_to_html_empty_string_returns_empty_string() -> None:
    """Empty input produces empty output."""
    assert ansi_to_html("") == ""


def test_ansi_to_html_reset_code_closes_no_spans_when_nothing_open() -> None:
    """A bare reset (ESC[0m) with no prior open spans produces no span tags."""
    result = ansi_to_html("\x1b[0mhello")
    assert "<span" not in result
    assert "hello" in result


def test_ansi_to_html_empty_sgr_code_acts_as_reset() -> None:
    """ESC[m (empty code) acts as a reset, closing any open spans."""
    result = ansi_to_html("\x1b[31mred\x1b[mplain")
    assert result.count("<span") == 1
    assert result.count("</span>") == 1
    assert "color:#c00" in result
    assert "red" in result
    assert "plain" in result


def test_ansi_to_html_standard_foreground_color_30() -> None:
    """ANSI code 30 (black) maps to the first 16-color palette entry."""
    result = ansi_to_html("\x1b[30mblack\x1b[0m")
    assert "color:#000" in result
    assert "black" in result


def test_ansi_to_html_standard_foreground_color_31() -> None:
    """ANSI code 31 (red) maps to the second 16-color palette entry."""
    result = ansi_to_html("\x1b[31mred\x1b[0m")
    assert "color:#c00" in result
    assert "red" in result


def test_ansi_to_html_standard_foreground_color_37() -> None:
    """ANSI code 37 (white/light grey) maps to index 7 in the palette."""
    result = ansi_to_html("\x1b[37mlight\x1b[0m")
    assert "color:#aaa" in result


def test_ansi_to_html_bright_foreground_color_90() -> None:
    """ANSI code 90 (bright black / dark grey) maps to palette index 8."""
    result = ansi_to_html("\x1b[90mbright-black\x1b[0m")
    assert "color:#555" in result


def test_ansi_to_html_bright_foreground_color_97() -> None:
    """ANSI code 97 (bright white) maps to palette index 15."""
    result = ansi_to_html("\x1b[97mbright-white\x1b[0m")
    assert "color:#fff" in result


def test_ansi_to_html_bold_produces_font_weight_bold() -> None:
    """ANSI code 1 (bold) produces a font-weight:bold style."""
    result = ansi_to_html("\x1b[1mbold\x1b[0m")
    assert "font-weight:bold" in result
    assert "bold" in result


def test_ansi_to_html_256color_low_palette_index_uses_16_color_table() -> None:
    """38;5;N where N < 16 uses the standard 16-color palette."""
    result = ansi_to_html("\x1b[38;5;1mred256\x1b[0m")
    assert "color:#c00" in result
    assert "red256" in result


def test_ansi_to_html_256color_rgb_cube_index_16() -> None:
    """38;5;16 is the first entry of the 6x6x6 color cube (all zeros -> rgb(0,0,0))."""
    result = ansi_to_html("\x1b[38;5;16mcolor\x1b[0m")
    assert "color:rgb(0,0,0)" in result


def test_ansi_to_html_256color_rgb_cube_mid_range() -> None:
    """38;5;196 is pure red in the 6x6x6 cube (r=5, g=0, b=0 -> rgb(255,0,0))."""
    result = ansi_to_html("\x1b[38;5;196mcolor\x1b[0m")
    assert "color:rgb(255,0,0)" in result


def test_ansi_to_html_256color_rgb_cube_index_231() -> None:
    """38;5;231 is the last entry of the 6x6x6 cube (r=5,g=5,b=5 -> rgb(255,255,255))."""
    result = ansi_to_html("\x1b[38;5;231mcolor\x1b[0m")
    assert "color:rgb(255,255,255)" in result


def test_ansi_to_html_256color_grayscale_ramp_index_232() -> None:
    """38;5;232 is the first grayscale ramp entry (v = 8 + 0*10 = 8)."""
    result = ansi_to_html("\x1b[38;5;232mcolor\x1b[0m")
    assert "color:rgb(8,8,8)" in result


def test_ansi_to_html_256color_grayscale_ramp_index_255() -> None:
    """38;5;255 is the last grayscale ramp entry (v = 8 + 23*10 = 238)."""
    result = ansi_to_html("\x1b[38;5;255mcolor\x1b[0m")
    assert "color:rgb(238,238,238)" in result


def test_ansi_to_html_reset_closes_open_spans() -> None:
    """ESC[0m after a color code closes the open span."""
    result = ansi_to_html("\x1b[32mgreen\x1b[0mplain")
    assert result.count("<span") == 1
    assert result.count("</span>") == 1
    assert result.index("</span>") > result.index("<span")


def test_ansi_to_html_multiple_sequential_colors_open_multiple_spans() -> None:
    """Each color code opens a new span; reset closes all open spans at once."""
    result = ansi_to_html("\x1b[31mred\x1b[32mgreen\x1b[0mplain")
    # Two open spans (one per color), then two closes from the single reset.
    assert result.count("<span") == 2
    assert result.count("</span>") == 2


def test_ansi_to_html_bold_and_color_combined_in_single_span() -> None:
    """Bold + color in one ESC sequence produces a single span with both styles."""
    result = ansi_to_html("\x1b[1;31mbold-red\x1b[0m")
    assert result.count("<span") == 1
    assert "font-weight:bold" in result
    assert "color:#c00" in result


def test_ansi_to_html_unknown_code_is_ignored_silently() -> None:
    """An unrecognised SGR code (e.g. 99) produces no span and no error."""
    result = ansi_to_html("\x1b[99mtext\x1b[0m")
    # No styles recognised, so no span should be emitted.
    assert "<span" not in result
    assert "text" in result


def test_ansi_to_html_trailing_open_spans_are_closed() -> None:
    """A color sequence without a trailing reset still yields balanced HTML."""
    result = ansi_to_html("\x1b[33myellow text")
    # Must close the span that was opened even without an explicit reset.
    assert result.count("<span") == result.count("</span>")
    assert "yellow text" in result


def test_ansi_to_html_text_before_and_after_escape_is_preserved() -> None:
    """Text surrounding ANSI sequences is included verbatim."""
    result = ansi_to_html("before\x1b[31mred\x1b[0mafter")
    assert result.startswith("before")
    assert result.endswith("after")


# ---------------------------------------------------------------------------
# render_tutorial_block
# ---------------------------------------------------------------------------


def test_render_tutorial_block_comment_lines_get_comment_class() -> None:
    """Lines starting with a hash character are wrapped in a comment span."""
    result = render_tutorial_block("# This is a comment")
    assert '<span class="comment"># This is a comment</span>' in result


def test_render_tutorial_block_command_lines_get_prompt_class() -> None:
    """Non-empty, non-comment lines are wrapped in a prompt span."""
    result = render_tutorial_block("mngr list")
    assert '<span class="prompt">mngr list</span>' in result


def test_render_tutorial_block_blank_lines_are_not_wrapped() -> None:
    """Empty lines appear between command spans without any wrapping."""
    result = render_tutorial_block("cmd\n\ncmd2")
    # Strip the outer <pre> wrapper to inspect the inner lines.
    inner = result.removeprefix('<pre class="transcript">').removesuffix("</pre>")
    lines = inner.split("\n")
    assert lines[0] == '<span class="prompt">cmd</span>'
    assert lines[1] == ""
    assert lines[2] == '<span class="prompt">cmd2</span>'


def test_render_tutorial_block_output_is_wrapped_in_pre_tag() -> None:
    """The entire output is wrapped in a <pre class="transcript"> element."""
    result = render_tutorial_block("# hello")
    assert result.startswith('<pre class="transcript">')
    assert result.endswith("</pre>")


def test_render_tutorial_block_html_special_chars_are_escaped() -> None:
    """HTML-special characters inside comment and command lines are escaped."""
    result = render_tutorial_block("# a < b & c\necho <hello>")
    assert "&lt;" in result
    assert "&amp;" in result
    assert "&gt;" in result


def test_render_tutorial_block_leading_whitespace_comment_still_matches() -> None:
    """A comment line with leading whitespace is still classified as a comment."""
    # Build a line with spaces then a hash; render_tutorial_block uses lstrip.
    input_line = "   " + "# indented comment"
    result = render_tutorial_block(input_line)
    assert '<span class="comment">' in result
    assert "indented comment" in result
    # The span must contain the original line verbatim (with leading spaces).
    assert input_line in result


def test_render_tutorial_block_mixed_content_preserves_order() -> None:
    """Comment, blank, and command lines appear in the correct order."""
    text = "# step 1\n\nmngr create"
    result = render_tutorial_block(text)
    comment_pos = result.index("comment")
    prompt_pos = result.index("prompt")
    assert comment_pos < prompt_pos


# ---------------------------------------------------------------------------
# render_transcript
# ---------------------------------------------------------------------------


def test_render_transcript_command_line_gets_prompt_class() -> None:
    """Lines starting with '$ ' are wrapped in a prompt span."""
    result = render_transcript("$ mngr list\n? 0")
    assert '<span class="prompt">$ mngr list</span>' in result


def test_render_transcript_comment_line_gets_comment_class() -> None:
    """Lines starting with '# ' are wrapped in a comment span."""
    result = render_transcript("# a comment\n$ cmd\n? 0")
    assert '<span class="comment"># a comment</span>' in result


def test_render_transcript_exit_code_line_gets_exit_code_class() -> None:
    """Lines matching '? N' are rendered as exit-code spans."""
    result = render_transcript("$ cmd\n? 42")
    assert '<span class="exit-code">exit code: 42</span>' in result


def test_render_transcript_stderr_line_gets_stderr_prefix_class() -> None:
    """Lines starting with '! ' render the prefix in stderr-prefix span."""
    result = render_transcript("$ cmd\n! error message\n? 1")
    assert '<span class="stderr-prefix">! </span>' in result
    assert "error message" in result


def test_render_transcript_output_is_wrapped_in_pre_transcript() -> None:
    """The rendered output is wrapped in a <pre class="transcript"> element."""
    result = render_transcript("$ ls\n? 0")
    assert '<pre class="transcript">' in result
    assert "</pre>" in result


def test_render_transcript_blocks_are_wrapped_in_cmd_block_divs() -> None:
    """Each logical block is wrapped in a <div class="cmd-block">."""
    result = render_transcript("$ ls\n? 0")
    assert '<div class="cmd-block">' in result
    assert "</div>" in result


def test_render_transcript_new_block_starts_after_exit_code_then_comment() -> None:
    """A new comment after an exit code starts a new cmd-block."""
    result = render_transcript("$ cmd1\n? 0\n# new section\n$ cmd2\n? 0")
    assert result.count('<div class="cmd-block">') == 2


def test_render_transcript_new_block_starts_after_exit_code_then_command() -> None:
    """A new $ line after an exit code starts a new cmd-block."""
    result = render_transcript("$ cmd1\n? 0\n$ cmd2\n? 0")
    assert result.count('<div class="cmd-block">') == 2


def test_render_transcript_output_lines_are_processed_through_ansi_to_html() -> None:
    """Output lines have their ANSI codes converted to HTML spans."""
    result = render_transcript("$ cmd\n\x1b[31mred error\x1b[0m\n? 0")
    assert "color:#c00" in result
    assert "red error" in result


def test_render_transcript_stderr_rest_goes_through_ansi_to_html() -> None:
    """The text after '! ' on stderr lines is processed through ansi_to_html."""
    result = render_transcript("$ cmd\n! \x1b[31merror\x1b[0m\n? 1")
    assert "color:#c00" in result
    assert "error" in result


def test_render_transcript_html_chars_in_comment_are_escaped() -> None:
    """HTML-special characters in comment lines are properly escaped."""
    result = render_transcript("# <b>bold</b>\n$ x\n? 0")
    assert "&lt;b&gt;" in result
    assert "<b>" not in result


def test_render_transcript_cast_stem_linkified_when_in_cast_stems() -> None:
    """Cast file stems appearing in the transcript are replaced with anchor links."""
    result = render_transcript("$ cmd\nrecording-1 output\n? 0", cast_stems=["recording-1"])
    assert 'href="#cast-recording-1"' in result
    assert "recording-1" in result


def test_render_transcript_cast_stem_not_linkified_when_not_in_cast_stems() -> None:
    """Cast stems not in the cast_stems list are not linkified."""
    result = render_transcript("$ cmd\nrecording-1 output\n? 0", cast_stems=["other-cast"])
    assert 'href="#cast-recording-1"' not in result


def test_render_transcript_cast_stems_none_does_not_linkify() -> None:
    """When cast_stems is None, no linkification occurs."""
    result = render_transcript("$ cmd\nrecording-1 output\n? 0", cast_stems=None)
    assert 'href="' not in result


def test_render_transcript_cast_stem_linkification_uses_html_escaped_stem() -> None:
    """A stem containing HTML-special chars is properly escaped in the link."""
    result = render_transcript("$ cmd\na&b output\n? 0", cast_stems=["a&b"])
    # The escaped form should appear as the link href target.
    assert 'href="#cast-a&amp;b"' in result


def test_render_transcript_empty_text_produces_only_transcript_wrapper() -> None:
    """An empty transcript produces a pre.transcript with no cmd-blocks."""
    result = render_transcript("")
    assert '<pre class="transcript">' in result
    assert '<div class="cmd-block">' not in result


# ---------------------------------------------------------------------------
# render_test_detail
# ---------------------------------------------------------------------------


def test_render_test_detail_includes_tutorial_block_section(tmp_path: Path) -> None:
    """render_test_detail renders the tutorial_block.txt file when present."""
    (tmp_path / "tutorial_block.txt").write_text("# step\nmngr create")
    result = render_test_detail(tmp_path)
    assert "<h3>Tutorial block</h3>" in result
    assert '<pre class="transcript">' in result
    assert "# step" in result
    assert "mngr create" in result


def test_render_test_detail_skips_tutorial_section_when_file_absent(tmp_path: Path) -> None:
    """render_test_detail omits the tutorial block section if no file exists."""
    result = render_test_detail(tmp_path)
    assert "<h3>Tutorial block</h3>" not in result


def test_render_test_detail_includes_transcript_section(tmp_path: Path) -> None:
    """render_test_detail renders the transcript.txt file when present."""
    (tmp_path / "transcript.txt").write_text("$ mngr list\n? 0")
    result = render_test_detail(tmp_path)
    assert "<h3>CLI transcript</h3>" in result
    assert "mngr list" in result


def test_render_test_detail_skips_transcript_section_when_file_absent(tmp_path: Path) -> None:
    """render_test_detail omits the CLI transcript section if no file exists."""
    result = render_test_detail(tmp_path)
    assert "<h3>CLI transcript</h3>" not in result


def test_render_test_detail_includes_cast_player_section(tmp_path: Path) -> None:
    """render_test_detail renders an asciinema player div for each .cast file."""
    cast_content = b'{"version": 2}\n[0.1, "o", "hello"]\n'
    (tmp_path / "my-recording.cast").write_bytes(cast_content)
    result = render_test_detail(tmp_path)
    assert "<h3" in result
    assert "TUI recording: my-recording" in result
    assert 'class="cast-player"' in result


def test_render_test_detail_embeds_cast_as_base64_data_url(tmp_path: Path) -> None:
    """The cast file content is embedded as a base64 data URL."""
    cast_content = b'{"version": 2}\n[0.1, "o", "hi"]\n'
    (tmp_path / "rec.cast").write_bytes(cast_content)
    result = render_test_detail(tmp_path)
    expected_b64 = base64.b64encode(cast_content).decode("ascii")
    assert f"data:text/plain;base64,{expected_b64}" in result


def test_render_test_detail_cast_player_init_script_is_present(tmp_path: Path) -> None:
    """A DOMContentLoaded script initialising AsciinemaPlayer is emitted."""
    (tmp_path / "rec.cast").write_bytes(b'{"version": 2}\n')
    result = render_test_detail(tmp_path)
    assert "AsciinemaPlayer.create(" in result
    assert "DOMContentLoaded" in result


def test_render_test_detail_no_script_when_no_cast_files(tmp_path: Path) -> None:
    """No player init script is emitted when there are no cast files."""
    (tmp_path / "transcript.txt").write_text("$ ls\n? 0")
    result = render_test_detail(tmp_path)
    assert "AsciinemaPlayer" not in result


def test_render_test_detail_cast_stem_is_linkified_in_transcript(tmp_path: Path) -> None:
    """A cast file stem appearing in the transcript text becomes a link."""
    (tmp_path / "transcript.txt").write_text("$ cmd\nrec-1 output\n? 0")
    (tmp_path / "rec-1.cast").write_bytes(b'{"version": 2}\n')
    result = render_test_detail(tmp_path)
    assert 'href="#cast-rec-1"' in result


def test_render_test_detail_detail_id_prefix_applied_to_anchor_and_player(tmp_path: Path) -> None:
    """detail_id_prefix is prepended to both the cast anchor and player div IDs."""
    (tmp_path / "rec.cast").write_bytes(b'{"version": 2}\n')
    result = render_test_detail(tmp_path, detail_id_prefix="test42-")
    assert 'id="test42-cast-rec"' in result
    assert 'id="test42-player-0"' in result


def test_render_test_detail_html_escaping_in_cast_stem(tmp_path: Path) -> None:
    """A cast file stem is HTML-escaped in the anchor id and heading."""
    # Ampersands are valid in POSIX filenames and require HTML escaping.
    (tmp_path / "a&b.cast").write_bytes(b'{"version": 2}\n')
    result = render_test_detail(tmp_path)
    # The escaped form must appear in the anchor id, not the raw ampersand.
    assert 'id="cast-a&amp;b"' in result
    assert 'id="cast-a&b"' not in result


def test_render_test_detail_multiple_cast_files_indexed_correctly(tmp_path: Path) -> None:
    """Multiple cast files produce distinct player divs with sequential indices."""
    (tmp_path / "alpha.cast").write_bytes(b'{"version": 2}\n')
    (tmp_path / "beta.cast").write_bytes(b'{"version": 2}\n')
    result = render_test_detail(tmp_path)
    assert 'id="player-0"' in result
    assert 'id="player-1"' in result
    assert "TUI recording: alpha" in result
    assert "TUI recording: beta" in result


def test_render_test_detail_empty_directory_returns_empty_string(tmp_path: Path) -> None:
    """An empty test directory produces an empty string."""
    result = render_test_detail(tmp_path)
    assert result == ""


def test_render_test_detail_all_sections_appear_in_correct_order(tmp_path: Path) -> None:
    """Tutorial block precedes transcript, which precedes cast player sections."""
    (tmp_path / "tutorial_block.txt").write_text("# tutorial")
    (tmp_path / "transcript.txt").write_text("$ cmd\n? 0")
    (tmp_path / "rec.cast").write_bytes(b'{"version": 2}\n')
    result = render_test_detail(tmp_path)
    tutorial_pos = result.index("Tutorial block")
    transcript_pos = result.index("CLI transcript")
    cast_pos = result.index("TUI recording")
    assert tutorial_pos < transcript_pos < cast_pos
