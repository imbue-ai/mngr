"""Unit tests for markdown-to-urwid-markup rendering."""

from imbue.mngr_kanpan.markdown_render import flatten_markup_line
from imbue.mngr_kanpan.markdown_render import render_markdown_lines


def test_plain_text_passes_through() -> None:
    assert render_markdown_lines("nothing special") == [["nothing special"]]


def test_paragraph_softbreaks_keep_lines() -> None:
    lines = render_markdown_lines("first line\nsecond line")
    assert [flatten_markup_line(line) for line in lines] == ["first line", "second line"]


def test_blank_line_between_blocks() -> None:
    lines = render_markdown_lines("one paragraph\n\nanother paragraph")
    assert [flatten_markup_line(line) for line in lines] == ["one paragraph", "", "another paragraph"]


def test_inline_code_and_bold_styled() -> None:
    (line,) = render_markdown_lines("run `mngr list` and **push**")
    assert ("md_code", "mngr list") in line
    assert ("md_bold", "push") in line


def test_emphasis_styled() -> None:
    (line,) = render_markdown_lines("very *subtle* hint")
    assert ("md_em", "subtle") in line


def test_link_shows_text_without_url() -> None:
    (line,) = render_markdown_lines("see [the PR](https://github.com/x/y/pull/1) for details")
    assert ("md_link", "the PR") in line
    assert "https://" not in flatten_markup_line(line)


def test_heading_dim_hashes_bold_text() -> None:
    (line,) = render_markdown_lines("## Results")
    assert line == [("peek_hint", "## "), ("md_bold", "Results")]


def test_bullet_list_dim_markers() -> None:
    lines = render_markdown_lines("- one\n- two `x`")
    assert lines[0] == [("peek_hint", "- "), "one"]
    assert ("peek_hint", "- ") in lines[1]
    assert ("md_code", "x") in lines[1]


def test_ordered_list_numbers_and_nesting_indent() -> None:
    lines = render_markdown_lines("1. first\n2. second\n   - nested")
    assert lines[0] == [("peek_hint", "1. "), "first"]
    assert lines[1] == [("peek_hint", "2. "), "second"]
    assert lines[2] == ["   ", ("peek_hint", "- "), "nested"]


def test_fence_dim_delimiters_and_code_lines() -> None:
    lines = render_markdown_lines("```python\nx = 1\n```")
    assert lines == [
        [("peek_hint", "```python")],
        [("md_code", "x = 1")],
        [("peek_hint", "```")],
    ]


def test_unclosed_fence_renders_rest_as_code() -> None:
    lines = render_markdown_lines("```\ncode line")
    assert [line for line in lines if ("md_code", "code line") == line[-1]]


def test_blockquote_dim_bar() -> None:
    (line,) = render_markdown_lines("> quoted words")
    assert line == [("peek_hint", "> "), "quoted words"]


def test_unknown_block_falls_back_to_raw_content() -> None:
    lines = render_markdown_lines("<div>\nraw html\n</div>")
    assert [flatten_markup_line(line) for line in lines] == ["<div>", "raw html", "</div>"]
