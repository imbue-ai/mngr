"""Markdown-to-urwid-markup rendering for the peek panel, via markdown-it-py."""

from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from imbue.imbue_common.mutable_model import MutableModel

# One rendered display line: urwid text markup segments (str or (attr, str)).
MarkupLine = list[Any]

_PARSER = MarkdownIt("commonmark")

# Sentinel for soft/hard line breaks inside an inline token stream.
_BREAK = ("__break__", "")


def render_markdown_lines(text: str) -> list[MarkupLine]:
    """Render markdown text to a list of urwid markup lines.

    Links show their text (styled ``md_link``) without the URL. Inline code and
    fenced blocks carry ``md_code``; bold and heading text ``md_bold``; emphasis
    ``md_em``; structural chrome (heading hashes, list bullets, fence delimiters,
    blockquote bars) is dimmed with ``peek_hint``. Unknown constructs fall back
    to their raw content as plain lines.
    """
    lines: list[MarkupLine] = []
    state = _BlockState()
    for token in _PARSER.parse(text):
        _render_block_token(token, state, lines)
    return lines


class _BlockState(MutableModel):
    """Mutable walker state: list nesting, blockquote depth, pending prefixes."""

    list_stack: list[dict[str, Any]] = []
    quote_depth: int = 0
    # Dim bullet awaiting the item's first rendered line (e.g. "- " or "3. ").
    item_bullet: str | None = None
    # Dim heading hashes awaiting the heading's inline content (e.g. "## ").
    heading_prefix: str | None = None

    def line_prefix(self) -> MarkupLine:
        """Chrome prepended to each rendered line: quote bars, indent, one bullet."""
        prefix: MarkupLine = []
        if self.quote_depth:
            prefix.append(("peek_hint", "> " * self.quote_depth))
        if self.item_bullet is not None:
            indent = "".join(level["indent"] for level in self.list_stack[:-1])
            if indent:
                prefix.append(indent)
            prefix.append(("peek_hint", self.item_bullet))
            self.item_bullet = None
        else:
            indent = "".join(level["indent"] for level in self.list_stack)
            if indent:
                prefix.append(indent)
        return prefix


def _render_block_token(token: Token, state: _BlockState, lines: list[MarkupLine]) -> None:
    match token.type:
        case "bullet_list_open" | "ordered_list_open":
            _separate_top_level_block(token, lines)
            state.list_stack.append({"ordered": token.type == "ordered_list_open", "n": 0, "indent": ""})
        case "bullet_list_close" | "ordered_list_close":
            state.list_stack.pop()
        case "list_item_open":
            level = state.list_stack[-1]
            level["n"] += 1
            bullet = f"{level['n']}. " if level["ordered"] else f"{token.markup} "
            level["indent"] = " " * len(bullet)
            state.item_bullet = bullet
        case "blockquote_open":
            _separate_top_level_block(token, lines)
            state.quote_depth += 1
        case "blockquote_close":
            state.quote_depth -= 1
        case "heading_open":
            _separate_top_level_block(token, lines)
            state.heading_prefix = f"{token.markup} "
        case "paragraph_open":
            _separate_top_level_block(token, lines)
        case "inline":
            base_attr = "md_bold" if state.heading_prefix is not None else None
            for index, content in enumerate(_split_on_breaks(_inline_markup(token.children or [], base_attr))):
                line = state.line_prefix()
                if index == 0 and state.heading_prefix is not None:
                    line.append(("peek_hint", state.heading_prefix))
                line.extend(content)
                lines.append(line)
            state.heading_prefix = None
        case "fence" | "code_block":
            _separate_top_level_block(token, lines)
            is_fence = token.type == "fence"
            if is_fence:
                lines.append([*state.line_prefix(), ("peek_hint", f"{token.markup}{token.info}")])
            code_lines = token.content.split("\n")
            if code_lines and code_lines[-1] == "":
                code_lines.pop()
            for code_line in code_lines:
                lines.append([*state.line_prefix(), ("md_code", code_line)])
            if is_fence:
                lines.append([*state.line_prefix(), ("peek_hint", token.markup)])
        case "hr":
            _separate_top_level_block(token, lines)
            lines.append([*state.line_prefix(), ("peek_hint", "───")])
        case _:
            # Unknown block (html_block, etc.): fall back to its raw content.
            if token.content:
                for raw_line in token.content.strip("\n").split("\n"):
                    lines.append([*state.line_prefix(), raw_line])


def _separate_top_level_block(token: Token, lines: list[MarkupLine]) -> None:
    """Insert one blank line between top-level blocks (tight list items excluded)."""
    if token.level == 0 and not token.hidden and lines and lines[-1]:
        lines.append([])


def _inline_markup(children: list[Token], base_attr: str | None) -> MarkupLine:
    """Flatten inline tokens into markup segments; breaks become sentinels."""
    segments: MarkupLine = []
    bold = 0
    em = 0
    link = 0
    for token in children:
        match token.type:
            case "text":
                if token.content:
                    segments.append(_styled(token.content, base_attr, bold, em, link))
            case "code_inline":
                segments.append(("md_code", token.content))
            case "strong_open":
                bold += 1
            case "strong_close":
                bold -= 1
            case "em_open":
                em += 1
            case "em_close":
                em -= 1
            case "link_open":
                link += 1
            case "link_close":
                link -= 1
            case "softbreak" | "hardbreak":
                segments.append(_BREAK)
            case "image":
                segments.append(("md_link", token.content or "[image]"))
            case _:
                if token.content:
                    segments.append(token.content)
    return segments


def _styled(text: str, base_attr: str | None, bold: int, em: int, link: int) -> Any:
    if link:
        return ("md_link", text)
    if base_attr is not None:
        return (base_attr, text)
    if bold:
        return ("md_bold", text)
    if em:
        return ("md_em", text)
    return text


def _split_on_breaks(segments: MarkupLine) -> list[MarkupLine]:
    """Split an inline segment stream into display lines at break sentinels."""
    result: list[MarkupLine] = [[]]
    for segment in segments:
        if segment == _BREAK:
            result.append([])
        else:
            result[-1].append(segment)
    return result


def flatten_markup_line(line: MarkupLine) -> str:
    """The plain text of one rendered line, attrs stripped."""
    return "".join(segment if isinstance(segment, str) else segment[1] for segment in line)
