#!/usr/bin/env python3
"""Approximate response streaming for Claude agents by watching the tmux pane.

This script runs as a background watcher while the agent's tmux session is
alive. On a configurable interval it captures the agent's tmux pane (with ANSI
escape codes preserved), reverse-maps the terminal-rendered assistant text back
into markdown, and writes the current in-progress assistant message to
``$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer``.

Buffer format:
  line 1     -> uuid of the last *complete* assistant message (empty if none)
  lines 2..  -> the in-progress assistant text, reverse-mapped to markdown

The body is strict-append within a single message (snapshots are overlap-stitched
onto what was already accumulated). A snapshot that does not overlap the current
buffer is treated as a new message and resets the body. When the agent goes idle
(the ``active`` file disappears) the body is emptied and only the id line remains.

Standalone: no mngr imports, uses only the Python stdlib, so it runs on remote
hosts where mngr is not installed. The pure reverse-mapping functions are imported
directly by the unit tests.

Usage: stream_snapshot.py <tmux_session_name> [primary_window_name]

``primary_window_name`` is the name of the agent's primary tmux window (config
``tmux.primary_window_name``, default ``agent``). The pane is captured by
targeting that window by name rather than the literal ``:0`` index, so capture
works regardless of the user's tmux ``base-index``.

Requires environment variables:
  MNGR_AGENT_STATE_DIR                  - the agent's state directory
  MNGR_CLAUDE_STREAM_SNAPSHOT_INTERVAL  - poll interval in seconds (float, > 0)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# The marker glyph Claude prints at the start of every assistant text block and
# tool-call block (U+25CF BLACK CIRCLE).
_MARKER: str = "●"

# Box-drawing characters Claude uses to render markdown tables.
_BOX_DRAWING_CHARS: frozenset[str] = frozenset("─│┌┐└┘├┤┬┴┼")

# The dim left-bar glyph Claude renders before blockquote lines (U+258E).
_BLOCKQUOTE_BAR: str = "▎"

# The connector glyph Claude prints on the first line of a tool call's rendered
# output (e.g. "  ⎿  $ cd ..."). It is U+23BF, distinct from the box-drawing
# characters used in markdown tables, and never appears in assistant prose, so its
# presence reliably marks the start of a tool-call block (the end of the message).
_TOOL_OUTPUT_CONNECTOR: str = "⎿"

# 256-color foreground code Claude uses for inline code spans.
_INLINE_CODE_COLOR: int = 153

# When stitching a marker-less (scrolled) region onto the accumulated body, the
# body's last few rows are volatile because Claude re-wraps the currently-streaming
# row. Tolerate dropping up to this many trailing body rows when searching for the
# alignment between the body and the new region.
_MAX_VOLATILE_TAIL_LINES: int = 5

# Basic-color SGR foreground codes that indicate syntax-highlighted code-block
# content. 94 (bright blue) is excluded because Claude uses it for links, not
# code.
_CODE_COLOR_CODES: frozenset[int] = frozenset({31, 32, 33, 34, 35, 36, 91, 92, 93, 95, 96})

# Regex matching a CSI escape sequence (e.g. "\x1b[1m", "\x1b[38;5;231m").
_CSI_RE = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")

# Regex matching any ANSI escape sequence (CSI or OSC) for plain stripping.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


# =============================================================================
# ANSI tokenization helpers
# =============================================================================


def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences (CSI and OSC) from a string."""
    return _ANSI_RE.sub("", text)


def _iter_sgr_params(text: str) -> list[int]:
    """Return all SGR numeric parameters appearing in CSI 'm' sequences in order."""
    params: list[int] = []
    for match in _CSI_RE.finditer(text):
        if match.group(2) != "m":
            continue
        raw_params = match.group(1)
        for part in raw_params.split(";"):
            if part == "":
                params.append(0)
            else:
                params.append(int(part))
    return params


def _has_code_color(text: str) -> bool:
    """True if the line contains a basic-color SGR used for code syntax highlighting."""
    for match in _CSI_RE.finditer(text):
        if match.group(2) != "m":
            continue
        parts = [p for p in match.group(1).split(";") if p != ""]
        for part in parts:
            if int(part) in _CODE_COLOR_CODES:
                return True
    return False


# =============================================================================
# Assistant-marker detection
# =============================================================================


def _is_achromatic_marker_color(params: list[int]) -> bool:
    """Decide whether an SGR foreground state denotes the default text color.

    Assistant text markers are rendered in the theme's default text color (white
    in a dark terminal, black in a light one). Tool-call markers are chromatic
    (e.g. green 114) and status markers are mid-gray (e.g. 246). We therefore
    accept only "default", pure white, and pure black foregrounds and reject
    everything else.

    ``params`` is the flattened list of SGR codes active up to and including the
    marker glyph.
    """
    # Track the most recently set foreground; None means terminal default.
    foreground: int | None = None
    index = 0
    while index < len(params):
        code = params[index]
        if code == 0 or code == 39:
            foreground = None
        elif code == 38 and index + 2 < len(params) and params[index + 1] == 5:
            foreground = 1000 + params[index + 2]
            index += 2
        elif code == 38 and index + 4 < len(params) and params[index + 1] == 2:
            index += 4
            foreground = -1
        elif 30 <= code <= 37 or 90 <= code <= 97:
            foreground = code
        else:
            # Other SGR codes (bold, italic, background, etc.) do not change fg.
            pass
        index += 1

    if foreground is None:
        return True
    # Basic white / black (normal and bright).
    if foreground in (30, 37, 90, 97):
        return True
    # 256-color: 1000 + N. White corner (231, 255, 15, 7), black corner (16, 0, 8).
    if foreground >= 1000:
        color_256 = foreground - 1000
        return color_256 in (0, 7, 8, 15, 16, 231, 255)
    return False


def _marker_prefix_is_assistant(line: str) -> bool:
    """True if ``line`` begins (after ANSI) with an assistant-text marker glyph."""
    marker_index = line.find(_MARKER)
    if marker_index == -1:
        return False
    prefix = line[:marker_index]
    # The marker must be the first visible character on the line.
    if strip_ansi(prefix).strip() != "":
        return False
    return _is_achromatic_marker_color(_iter_sgr_params(prefix))


def _line_is_any_marker(line: str) -> bool:
    """True if the line's first visible character is the marker glyph (any color)."""
    marker_index = line.find(_MARKER)
    if marker_index == -1:
        return False
    return strip_ansi(line[:marker_index]).strip() == ""


# =============================================================================
# Block extraction
# =============================================================================


def _deindent_continuation(line: str) -> str:
    """Strip the two-space block indent from a continuation line, preserving ANSI.

    Leading ANSI escapes and the first two literal spaces (Claude's block indent)
    are consumed; any further spaces (real nested indentation) and the rest of the
    line are kept verbatim.
    """
    result: list[str] = []
    index = 0
    spaces_removed = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char == "\x1b":
            match = _CSI_RE.match(line, index)
            if match is None:
                match = re.match(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", line[index:])
                if match is not None:
                    result.append(match.group(0))
                    index += match.end()
                    continue
            if match is not None:
                result.append(match.group(0))
                index = match.end()
                continue
            result.append(char)
            index += 1
        elif char == " " and spaces_removed < 2:
            spaces_removed += 1
            index += 1
        else:
            result.append(line[index:])
            break
    return "".join(result)


def _strip_marker_prefix(line: str) -> str:
    """Return the content after the assistant marker glyph and its single space."""
    marker_index = line.find(_MARKER)
    rest = line[marker_index + len(_MARKER) :]
    # Drop leading ANSI and a single separating space.
    index = 0
    space_removed = False
    result: list[str] = []
    while index < len(rest):
        char = rest[index]
        if char == "\x1b":
            match = _CSI_RE.match(rest, index)
            if match is not None:
                result.append(match.group(0))
                index = match.end()
                continue
            result.append(char)
            index += 1
        elif char == " " and not space_removed:
            space_removed = True
            index += 1
        else:
            result.append(rest[index:])
            break
    return "".join(result)


def _collect_region_lines(lines: list[str], start_index: int, strip_first_marker: bool) -> list[str]:
    """Collect a de-indented content region starting at ``start_index``.

    The region runs until the end of the message, detected by any of:

    - a run of two or more consecutive blank lines (the TUI pads the viewport
      with blank rows below the message before the footer; the message itself
      only ever uses single blank lines between paragraphs), or
    - a new marker line (the next tool-call/status block), or
    - a non-empty line that is not two-space indented (a column-0 footer such as
      the input-box border), or
    - a ``⎿`` tool-output connector (see below).

    ``strip_first_marker`` controls whether the first line is treated as a marker
    line (strip the ``●`` prefix) or a plain continuation line (de-indent).

    A tool call that begins right as the message ends also ends the region. Its
    marker line ("● Running 1 shell command…") normally ends the region via the
    marker check, but the grey ``●`` flashes off, and while off the line looks like
    an indented continuation ("  Running 1 shell command…"). We do not match that
    spinner text (it may be only partially rendered); instead we key off the ``⎿``
    tool-output connector beneath it: when seen, the message ended at the tool
    marker line above (which we may already have appended), so that line is dropped
    and the tool block is never captured as assistant text. While the marker is
    briefly the last visible line (before the ``⎿`` renders) it is the body's last
    line, held back by the consumer, so it is never emitted; the next poll's ``⎿``
    removes it.
    """
    if strip_first_marker:
        first = _strip_marker_prefix(lines[start_index])
    else:
        first = _deindent_continuation(lines[start_index])
    region: list[str] = [first]
    consecutive_blanks = 0
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        stripped = strip_ansi(line)
        if stripped.strip() == "":
            consecutive_blanks += 1
            if consecutive_blanks >= 2:
                break
            region.append("")
            continue
        consecutive_blanks = 0
        if _line_is_any_marker(line):
            break
        if not stripped.startswith("  "):
            break
        if stripped.strip().startswith(_TOOL_OUTPUT_CONNECTOR):
            # Tool-call output: drop the (circle-off) tool marker line above it
            # along with any trailing blanks, and end the message here.
            while region and region[-1] == "":
                region.pop()
            if region:
                region.pop()
            break
        region.append(_deindent_continuation(line))

    while region and region[-1] == "":
        region.pop()
    return region


def extract_message_region(pane_text: str) -> tuple[list[str], bool] | None:
    """Extract the current assistant message region from a captured pane.

    Returns ``(de_indented_lines, has_marker)`` or None if no plausible message
    region is present. ``has_marker`` is True when the region is anchored at the
    assistant ``●`` marker (the message start is visible); False when only the
    message tail is visible (a long message has scrolled the marker off the top),
    in which case the topmost two-space-indented content region is returned for
    overlap-stitching against the accumulated body.

    Claude's TUI redraws within the viewport rather than scrolling content into
    tmux's scrollback, so scrolled-off lines cannot be recovered by capturing
    more scrollback -- the marker-less continuation path is what allows streaming
    to continue past the point where the marker scrolls off.
    """
    lines = pane_text.split("\n")

    # Prefer the marker-anchored region: find the last assistant marker line.
    for index in range(len(lines) - 1, -1, -1):
        if _line_is_any_marker(lines[index]) and _marker_prefix_is_assistant(lines[index]):
            return _collect_region_lines(lines, index, strip_first_marker=True), True

    # No marker visible: take the topmost two-space-indented content region (the
    # scrolled message tail). Stop scanning at any marker line -- content below a
    # (tool-call) marker is not assistant prose.
    for index, line in enumerate(lines):
        stripped = strip_ansi(line)
        if stripped.strip() == "":
            continue
        if _line_is_any_marker(line):
            break
        if stripped.startswith("  "):
            region = _collect_region_lines(lines, index, strip_first_marker=False)
            return (region, False) if region else None
        break
    return None


def extract_latest_assistant_block(pane_text: str) -> list[str] | None:
    """Return the marker-anchored assistant block, or None if no marker is visible.

    Thin wrapper over :func:`extract_message_region` that yields only the
    marker-anchored block (used by tests and callers that require the message
    start to be present).
    """
    result = extract_message_region(pane_text)
    if result is None or not result[1]:
        return None
    return result[0]


# =============================================================================
# Inline markdown reconstruction
# =============================================================================


class _InlineState:
    """Mutable emphasis/link state carried across the lines of a block.

    Uses class-level defaults (all immutable) rather than __init__; each instance
    shadows them on first assignment, so instances stay independent.
    """

    is_bold: bool = False
    is_italic: bool = False
    is_code: bool = False
    link_url: str | None = None


def _apply_sgr_to_state(params: list[int], state: _InlineState) -> None:
    """Update emphasis state from a list of SGR parameters."""
    index = 0
    while index < len(params):
        code = params[index]
        if code == 0:
            state.is_bold = False
            state.is_italic = False
            state.is_code = False
        elif code == 1:
            state.is_bold = True
        elif code == 3:
            state.is_italic = True
        elif code == 22:
            state.is_bold = False
        elif code == 23:
            state.is_italic = False
        elif code == 39:
            state.is_code = False
        elif code == 38 and index + 2 < len(params) and params[index + 1] == 5:
            state.is_code = params[index + 2] == _INLINE_CODE_COLOR
            index += 2
        else:
            # Other SGR codes do not affect emphasis/link state.
            pass
        index += 1


def _wrap_run(text: str, is_bold: bool, is_italic: bool, is_code: bool, link_url: str | None) -> str:
    """Wrap a single styled run of text in the appropriate markdown markers."""
    if text == "":
        return ""
    if link_url is not None:
        return f"[{text}]({link_url})"
    # Whitespace-only runs are emitted verbatim so we never produce "** **".
    if text.strip() == "":
        return text
    prefix = ""
    suffix = ""
    if is_code:
        # Inline code wins: emphasis markers inside code are not meaningful.
        return f"`{text}`"
    if is_bold:
        prefix += "**"
        suffix = "**" + suffix
    if is_italic:
        prefix += "*"
        suffix = "*" + suffix
    return f"{prefix}{text}{suffix}"


def render_inline_line(line: str, state: _InlineState) -> str:
    """Reverse-map one ANSI line into markdown, threading emphasis state.

    Text is grouped into styled runs (a run ends whenever an SGR or OSC link
    sequence changes the active style) and each run is wrapped independently, so
    no empty or unbalanced markers are ever produced. Emphasis/link state carries
    across lines via ``state``.
    """
    out: list[str] = []
    run: list[str] = []

    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char == "\x1b":
            osc = re.match(r"\x1b\]8;([^;]*);([^\x07\x1b]*)(?:\x07|\x1b\\)", line[index:])
            if osc is not None:
                out.append(_flush_run(run, state))
                url = osc.group(2)
                state.link_url = None if url == "" else url
                index += osc.end()
                continue
            csi = _CSI_RE.match(line, index)
            if csi is not None:
                if csi.group(2) == "m":
                    out.append(_flush_run(run, state))
                    params: list[int] = [0 if part == "" else int(part) for part in csi.group(1).split(";")]
                    _apply_sgr_to_state(params, state)
                index = csi.end()
                continue
            index += 1
            continue
        run.append(char)
        index += 1

    out.append(_flush_run(run, state))
    return "".join(out)


def _flush_run(run: list[str], state: "_InlineState") -> str:
    """Render the accumulated run with the current style, clearing ``run`` in place."""
    if not run:
        return ""
    rendered = _wrap_run("".join(run), state.is_bold, state.is_italic, state.is_code, state.link_url)
    run.clear()
    return rendered


# =============================================================================
# Table reconstruction
# =============================================================================


def _is_table_line(deindented: str) -> bool:
    """True if a de-indented line is part of a box-drawing table."""
    stripped = strip_ansi(deindented)
    return any(char in _BOX_DRAWING_CHARS for char in stripped)


def convert_table(table_lines: list[str]) -> list[str]:
    """Convert box-drawing table lines into GitHub-flavored markdown rows.

    Border lines (top/middle/bottom) are dropped; the first data row becomes the
    header and a ``| --- |`` separator is inserted after it. Returns the original
    lines as-is if no pipe rows are found (not actually a table).
    """
    rows: list[list[str]] = []
    for line in table_lines:
        stripped = strip_ansi(line)
        if "│" not in stripped:
            continue
        cells = [cell.strip() for cell in stripped.split("│")]
        # split on the vertical bar yields empty leading/trailing cells.
        cells = [cell for cell in cells[1:-1]] if len(cells) >= 2 else cells
        rows.append(cells)

    if not rows:
        return [strip_ansi(line) for line in table_lines]

    column_count = max(len(row) for row in rows)
    out: list[str] = []
    header = rows[0]
    out.append("| " + " | ".join(_pad_row(header, column_count)) + " |")
    out.append("| " + " | ".join(["---"] * column_count) + " |")
    for row in rows[1:]:
        out.append("| " + " | ".join(_pad_row(row, column_count)) + " |")
    return out


def _pad_row(row: list[str], column_count: int) -> list[str]:
    return row + [""] * (column_count - len(row))


# =============================================================================
# Block -> markdown (with a deferred trailing table region)
# =============================================================================


class BlockConversion:
    """Result of converting an assistant block to markdown.

    ``lines`` are the resolved markdown lines. ``pending_table`` holds the raw
    de-indented lines of a trailing box-drawing region whose completeness is not
    yet known; it is None when there is no such trailing region.
    """

    def __init__(self, lines: list[str], pending_table: list[str] | None) -> None:
        self.lines = lines
        self.pending_table = pending_table


def convert_block_to_markdown(block_lines: list[str]) -> BlockConversion:
    """Reverse-map an assistant block to markdown, deferring a trailing table.

    A box-drawing region that is the trailing content of the block is returned as
    ``pending_table`` rather than included in ``lines`` (its completeness can only
    be judged by the caller across polls). A box-drawing region with content after
    it is resolved inline, since something following it proves it is complete.
    """
    # Identify a trailing run of table lines (ignoring trailing blanks).
    last_content_index = -1
    for index in range(len(block_lines) - 1, -1, -1):
        if block_lines[index] != "":
            last_content_index = index
            break

    trailing_table_start: int | None = None
    if last_content_index >= 0 and _is_table_line(block_lines[last_content_index]):
        # Walk back over the contiguous run of table and interleaved blank lines.
        cursor = last_content_index
        while cursor - 1 >= 0 and (_is_table_line(block_lines[cursor - 1]) or block_lines[cursor - 1] == ""):
            cursor -= 1
        # Advance past any leading blanks so the run begins at the first table line.
        while cursor < len(block_lines) and not _is_table_line(block_lines[cursor]):
            cursor += 1
        trailing_table_start = cursor

    body_end = trailing_table_start if trailing_table_start is not None else len(block_lines)
    resolved = _convert_body_lines(block_lines[:body_end])

    pending_table: list[str] | None = None
    if trailing_table_start is not None:
        pending_table = [line for line in block_lines[trailing_table_start:] if line != ""]

    return BlockConversion(lines=resolved, pending_table=pending_table)


def _convert_body_lines(block_lines: list[str]) -> list[str]:
    """Convert the non-trailing-table portion of a block into markdown lines.

    Each prose line is rendered with a fresh emphasis state (no state carried
    across lines). This is essential for streaming stability: a finalized line's
    markdown must depend only on its own ANSI, so it does not flip (e.g.
    plain->bold) between polls as a neighbouring line's emphasis streams in. The
    terminal already re-asserts the active SGR at the start of each rendered line,
    so per-line rendering reproduces the same emphasis.
    """
    out: list[str] = []
    index = 0
    length = len(block_lines)
    while index < length:
        line = block_lines[index]
        stripped = strip_ansi(line)

        if stripped.strip() == "":
            out.append("")
            index += 1
            continue

        # A non-trailing box-drawing region is complete; convert it inline.
        if _is_table_line(line):
            table_run: list[str] = []
            while index < length and (_is_table_line(block_lines[index]) or block_lines[index] == ""):
                if block_lines[index] != "":
                    table_run.append(block_lines[index])
                index += 1
            out.extend(convert_table(table_run))
            continue

        # A run of syntax-highlighted lines becomes a fenced code block.
        if _has_code_color(line):
            code_run: list[str] = []
            while index < length and _has_code_color(block_lines[index]):
                code_run.append(strip_ansi(block_lines[index]).rstrip())
                index += 1
            out.append("```")
            out.extend(code_run)
            out.append("```")
            continue

        # Blockquote: a dim left-bar glyph followed by the quoted text.
        bar_index = stripped.find(_BLOCKQUOTE_BAR)
        if bar_index != -1 and stripped[:bar_index].strip() == "":
            quote_text = stripped[bar_index + len(_BLOCKQUOTE_BAR) :].strip()
            out.append(f"> {quote_text}")
            index += 1
            continue

        out.append(render_inline_line(line, _InlineState()).rstrip())
        index += 1

    return out


# =============================================================================
# Stitching state
# =============================================================================


def compute_overlap(existing: list[str], incoming: list[str]) -> int:
    """Return the length of the longest suffix of ``existing`` equal to a prefix of ``incoming``."""
    max_overlap = min(len(existing), len(incoming))
    for overlap in range(max_overlap, 0, -1):
        if existing[-overlap:] == incoming[:overlap]:
            return overlap
    return 0


class StreamBufferState:
    """Accumulates the strict-append buffer body across successive snapshots.

    The prose body (``_prose_lines``) grows monotonically via overlap-stitching.
    A trailing table is tracked separately (``_resolved_table_lines``) so that as
    it streams in row-by-row the rendered body stays append-only: the table is
    deferred until its raw form is stable across two polls, then re-rendered. Each
    successive render is a superset (prefix-extension) of the previous one, so the
    combined body never shrinks and downstream consumers only ever see appends.
    """

    def __init__(self) -> None:
        self._prose_lines: list[str] = []
        self._pending_table_signature: list[str] | None = None
        self._resolved_table_lines: list[str] = []

    @property
    def body_lines(self) -> list[str]:
        """The full rendered body: prose followed by the resolved table (if any)."""
        return self._prose_lines + self._resolved_table_lines

    def reset(self) -> None:
        self._prose_lines = []
        self._pending_table_signature = None
        self._resolved_table_lines = []

    def ingest_block(self, conversion: BlockConversion, has_marker: bool) -> None:
        """Merge a converted block into the body, handling table deferral and resets.

        ``has_marker`` is True when the captured region was anchored at the
        assistant ``●`` marker (the start of the message is visible), and False
        when it is a marker-less continuation (the message has scrolled the marker
        off the top, so only its tail is visible). A marker-less region is aligned
        onto the accumulated body by overlap (tolerating a few re-wrapped volatile
        trailing rows) and replaces the body from the alignment point; it never
        resets or shrinks the body, nor starts one from nothing, because without
        the marker we cannot be sure unrelated content is a new message.
        """
        self._merge_prose(list(conversion.lines), has_marker)

        # The table is kept out of the prose body until its raw form is stable
        # across two polls, then rendered. Keeping the last render while it is
        # still changing avoids the table flickering out of the buffer.
        if conversion.pending_table is None:
            self._pending_table_signature = None
            self._resolved_table_lines = []
        elif conversion.pending_table == self._pending_table_signature:
            self._resolved_table_lines = convert_table(conversion.pending_table)
        else:
            self._pending_table_signature = list(conversion.pending_table)

    def _merge_prose(self, incoming: list[str], has_marker: bool) -> None:
        if not incoming:
            return
        # A marker-anchored region starts at the message's first line, so it IS
        # the full current message: take it directly. (Claude's TUI re-wraps only
        # the last, still-streaming row, so earlier rows are stable; the rendered
        # message therefore grows monotonically and the consumer holds back the
        # volatile last line.)
        if has_marker:
            self._prose_lines = list(incoming)
            return
        # Marker-less region: only the scrolled tail is visible. Align it onto the
        # accumulated body and replace from the alignment point. The body's last
        # few rows are volatile (the currently-streaming row re-wraps), so a strict
        # suffix==prefix overlap can fail; tolerate dropping a few volatile trailing
        # body rows when searching for the alignment (smallest drop first).
        if not self._prose_lines:
            # No anchor to start a marker-less region from.
            return
        for drop in range(0, _MAX_VOLATILE_TAIL_LINES + 1):
            kept = self._prose_lines[: len(self._prose_lines) - drop]
            if not kept:
                break
            overlap = compute_overlap(kept, incoming)
            if overlap == 0:
                continue
            candidate = kept[: len(kept) - overlap] + list(incoming)
            # Only accept an alignment that continues the message forward; a
            # candidate shorter than the current body is a stale/partial snapshot
            # mis-aligning, so keep what we have rather than dropping content.
            if len(candidate) >= len(self._prose_lines):
                self._prose_lines = candidate
            return
        # No alignment found: lost continuity (scrolled too fast) or unrelated
        # content. Keep the accumulated body rather than dropping or replacing it.


def format_buffer(last_complete_id: str, body_lines: list[str]) -> str:
    """Build the stream_buffer file contents: id line followed by the body.

    Trailing blank lines in the body are dropped for presentation; the stored
    body is left untouched so overlap-stitching across polls stays stable.
    """
    trimmed = list(body_lines)
    while trimmed and trimmed[-1] == "":
        trimmed.pop()
    return "\n".join([last_complete_id, *trimmed])


# =============================================================================
# Runtime (host side)
# =============================================================================


def _log(message: str) -> None:
    print(f"stream_snapshot: {message}", file=sys.stderr, flush=True)


def _read_last_complete_assistant_id(transcript_path: Path) -> str:
    """Return the uuid of the last assistant entry in the raw transcript, or ''."""
    try:
        content = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    last_uuid = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "":
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "assistant":
            uuid = event.get("uuid")
            if isinstance(uuid, str) and uuid != "":
                last_uuid = uuid
    return last_uuid


def _agent_pane_target(session_name: str, window_name: str) -> str:
    """Build the exact-match tmux target for the agent's primary window/pane.

    ``=`` forces exact session-name matching; the window is addressed by name
    (not the literal ``:0`` index) so the target is correct regardless of the
    user's tmux ``base-index``, matching how mngr creates and targets the window
    everywhere else.
    """
    return f"={session_name}:{window_name}"


def _capture_pane(session_name: str, window_name: str) -> str | None:
    """Capture the agent's visible tmux pane with ANSI codes and rejoined wrapped lines.

    Only the visible pane is captured: Claude's TUI redraws within the viewport
    rather than scrolling content into tmux's scrollback, so capturing scrollback
    yields no extra message content. Continuation past the point where the marker
    scrolls off is handled by overlap-stitching successive visible captures.
    """
    target = _agent_pane_target(session_name, window_name)
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-e", "-J", "-p", "-t", target],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log(f"capture-pane failed: {exc}")
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _write_buffer_atomically(buffer_path: Path, contents: str) -> None:
    """Write the buffer via a temp file + rename so readers never see a torn write."""
    tmp_path = buffer_path.with_suffix(buffer_path.suffix + ".tmp")
    try:
        tmp_path.write_text(contents, encoding="utf-8")
        os.replace(tmp_path, buffer_path)
    except OSError as exc:
        _log(f"failed to write buffer: {exc}")


def _session_is_alive(session_name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", f"={session_name}"],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def _run_one_poll(
    session_name: str,
    window_name: str,
    state: StreamBufferState,
    active_path: Path,
    transcript_path: Path,
    buffer_path: Path,
) -> None:
    """Perform a single capture/convert/stitch/write cycle."""
    last_id = _read_last_complete_assistant_id(transcript_path)

    # Agent idle: empty the body, keep refreshing the id line.
    if not active_path.exists():
        state.reset()
        _write_buffer_atomically(buffer_path, format_buffer(last_id, state.body_lines))
        return

    pane = _capture_pane(session_name, window_name)
    if pane is None:
        return

    # Extract the message region (marker-anchored when the start is visible, or
    # the scrolled tail otherwise) and stitch it onto the accumulated body.
    region_result = extract_message_region(pane)
    if region_result is None:
        _write_buffer_atomically(buffer_path, format_buffer(last_id, state.body_lines))
        return

    region_lines, has_marker = region_result
    conversion = convert_block_to_markdown(region_lines)
    state.ingest_block(conversion, has_marker)
    _write_buffer_atomically(buffer_path, format_buffer(last_id, state.body_lines))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _log("usage: stream_snapshot.py <tmux_session_name> [primary_window_name]")
        return 1
    session_name = argv[1]
    # The agent's primary window name (default "agent"); the pane is targeted by
    # this name, not the literal :0 index, so capture is base-index agnostic.
    window_name = argv[2] if len(argv) >= 3 and argv[2] else "agent"

    state_dir_str = os.environ.get("MNGR_AGENT_STATE_DIR")
    if not state_dir_str:
        _log("MNGR_AGENT_STATE_DIR must be set")
        return 1
    state_dir = Path(state_dir_str)

    buffer_dir = state_dir / "plugin" / "claude"
    buffer_path = buffer_dir / "stream_buffer"
    interval_path = buffer_dir / "stream_interval"
    active_path = state_dir / "active"
    transcript_path = state_dir / "logs" / "claude_transcript" / "events.jsonl"

    # The poll interval is written to a file at provision time (this avoids
    # depending on env-var propagation into the background-tasks subshell).
    try:
        interval_str = interval_path.read_text(encoding="utf-8").strip()
    except OSError:
        _log(f"no interval file at {interval_path}, exiting")
        return 0
    try:
        interval_seconds = float(interval_str)
    except ValueError:
        _log(f"invalid interval {interval_str!r}")
        return 1
    if interval_seconds <= 0:
        return 0

    # Prevent duplicate instances for the same session.
    pid_path = Path(f"/tmp/mngr_stream_snapshot_{session_name}.pid")
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text().strip())
            os.kill(existing_pid, 0)
            return 0
        except (ValueError, OSError):
            pass
    try:
        buffer_dir.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))
    except OSError as exc:
        _log(f"startup failed: {exc}")
        return 1

    # Clear any stale buffer from a previous run.
    state = StreamBufferState()
    _write_buffer_atomically(buffer_path, format_buffer("", []))

    try:
        while _session_is_alive(session_name):
            _run_one_poll(session_name, window_name, state, active_path, transcript_path, buffer_path)
            time.sleep(interval_seconds)
    finally:
        try:
            pid_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
