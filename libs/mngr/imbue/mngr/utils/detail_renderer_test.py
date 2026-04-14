from pathlib import Path

from imbue.mngr.utils.detail_renderer import ansi_to_html
from imbue.mngr.utils.detail_renderer import render_test_detail
from imbue.mngr.utils.detail_renderer import render_transcript
from imbue.mngr.utils.detail_renderer import render_tutorial_block


# -- ansi_to_html --


def test_ansi_to_html_plain_text() -> None:
    assert ansi_to_html("hello world") == "hello world"


def test_ansi_to_html_escapes_html() -> None:
    assert "&lt;" in ansi_to_html("<script>")
    assert "&amp;" in ansi_to_html("a&b")


def test_ansi_to_html_reset_code() -> None:
    result = ansi_to_html("\x1b[1mhello\x1b[0m world")
    assert "<span" in result
    assert "</span>" in result
    assert "bold" in result


def test_ansi_to_html_foreground_30_37() -> None:
    result = ansi_to_html("\x1b[31mred\x1b[0m")
    assert "color:#c00" in result
    assert "red" in result


def test_ansi_to_html_bright_foreground_90_97() -> None:
    result = ansi_to_html("\x1b[91mbright red\x1b[0m")
    assert "color:#f55" in result


def test_ansi_to_html_256_color_standard() -> None:
    result = ansi_to_html("\x1b[38;5;1mred256\x1b[0m")
    assert "color:#c00" in result


def test_ansi_to_html_256_color_cube() -> None:
    result = ansi_to_html("\x1b[38;5;196mrgb\x1b[0m")
    assert "color:rgb(" in result


def test_ansi_to_html_256_color_grayscale() -> None:
    result = ansi_to_html("\x1b[38;5;240mgray\x1b[0m")
    assert "color:rgb(" in result
    assert "gray" in result


def test_ansi_to_html_bold() -> None:
    result = ansi_to_html("\x1b[1mbold text\x1b[0m")
    assert "font-weight:bold" in result


def test_ansi_to_html_multiple_codes() -> None:
    result = ansi_to_html("\x1b[1;31mbold red\x1b[0m")
    assert "font-weight:bold" in result
    assert "color:#c00" in result


def test_ansi_to_html_unclosed_span() -> None:
    result = ansi_to_html("\x1b[31mno close")
    assert "color:#c00" in result
    assert result.endswith("</span>")


def test_ansi_to_html_unknown_code_ignored() -> None:
    result = ansi_to_html("\x1b[999mtext\x1b[0m")
    assert "text" in result


# -- render_tutorial_block --


def test_render_tutorial_block_comments() -> None:
    result = render_tutorial_block("# this is a comment\necho hello")
    assert "comment" in result
    assert "prompt" in result
    assert "transcript" in result


def test_render_tutorial_block_blank_lines() -> None:
    result = render_tutorial_block("# comment\n\necho hello")
    assert result.count("\n") >= 2


# -- render_transcript --


def test_render_transcript_comment_line() -> None:
    result = render_transcript("# setup step\n$ echo hi\n? 0")
    assert "comment" in result
    assert "# setup step" in result


def test_render_transcript_command_line() -> None:
    result = render_transcript("$ echo hi\n? 0")
    assert "prompt" in result
    assert "$ echo hi" in result


def test_render_transcript_stderr_line() -> None:
    result = render_transcript("$ cmd\n! error output\n? 1")
    assert "stderr-prefix" in result


def test_render_transcript_exit_code() -> None:
    result = render_transcript("$ cmd\n? 42")
    assert "exit code: 42" in result


def test_render_transcript_output_line_with_ansi() -> None:
    result = render_transcript("$ cmd\n\x1b[31mred output\x1b[0m\n? 0")
    assert "color:#c00" in result


def test_render_transcript_multiple_blocks() -> None:
    text = "$ cmd1\n? 0\n# next\n$ cmd2\n? 0"
    result = render_transcript(text)
    assert result.count("cmd-block") >= 2


def test_render_transcript_with_cast_stems() -> None:
    result = render_transcript("$ cmd\nmy-recording\n? 0", cast_stems=["my-recording"])
    assert "#cast-my-recording" in result
    assert "my-recording</a>" in result


# -- render_test_detail --


def test_render_test_detail_empty_dir(tmp_path: Path) -> None:
    result = render_test_detail(tmp_path)
    assert result == ""


def test_render_test_detail_tutorial_only(tmp_path: Path) -> None:
    (tmp_path / "tutorial_block.txt").write_text("# step 1\necho hello")
    result = render_test_detail(tmp_path)
    assert "Tutorial block" in result
    assert "comment" in result


def test_render_test_detail_transcript_only(tmp_path: Path) -> None:
    (tmp_path / "transcript.txt").write_text("$ echo hi\n? 0")
    result = render_test_detail(tmp_path)
    assert "CLI transcript" in result
    assert "prompt" in result


def test_render_test_detail_with_cast_file(tmp_path: Path) -> None:
    (tmp_path / "transcript.txt").write_text("$ demo\ndemo-cast\n? 0")
    (tmp_path / "demo-cast.cast").write_text('{"version": 2}\n[0.0, "o", "hello"]')
    result = render_test_detail(tmp_path)
    assert "cast-player" in result
    assert "AsciinemaPlayer.create" in result
    assert "data:text/plain;base64," in result
    assert "demo-cast" in result


def test_render_test_detail_with_prefix(tmp_path: Path) -> None:
    (tmp_path / "demo.cast").write_text('{"version": 2}')
    result = render_test_detail(tmp_path, detail_id_prefix="test1-")
    assert "test1-cast-demo" in result
    assert "test1-player-0" in result


def test_render_test_detail_full(tmp_path: Path) -> None:
    (tmp_path / "tutorial_block.txt").write_text("# intro\nrun command")
    (tmp_path / "transcript.txt").write_text("$ run command\noutput\n? 0")
    (tmp_path / "recording.cast").write_text('{"version": 2}\n[0.5, "o", "typed"]')
    result = render_test_detail(tmp_path)
    assert "Tutorial block" in result
    assert "CLI transcript" in result
    assert "TUI recording: recording" in result
    assert "AsciinemaPlayer" in result
