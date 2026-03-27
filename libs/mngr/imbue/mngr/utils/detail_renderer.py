"""Render test detail content as self-contained HTML fragments.

Provides functions to render CLI transcripts, tutorial blocks, and asciinema
recordings from a test output directory into HTML that can be embedded in any
page without requiring a server. Asciinema casts are inlined as base64 data URLs.
"""

import base64
import html
import json
import re
from pathlib import Path

# Standard 8-color and bright 8-color ANSI palette (indices 0-15).
_ANSI_COLORS_16 = [
    "#000",
    "#c00",
    "#0a0",
    "#a50",
    "#00a",
    "#a0a",
    "#0aa",
    "#aaa",
    "#555",
    "#f55",
    "#5f5",
    "#ff5",
    "#55f",
    "#f5f",
    "#5ff",
    "#fff",
]

ASCIINEMA_PLAYER_CSS = "https://cdn.jsdelivr.net/npm/asciinema-player@3.15.1/dist/bundle/asciinema-player.css"
ASCIINEMA_PLAYER_JS = "https://cdn.jsdelivr.net/npm/asciinema-player@3.15.1/dist/bundle/asciinema-player.min.js"

DETAIL_CSS = (
    ".transcript { background: rgb(30,30,30); color: rgb(212,212,212); padding: 1em;"
    " border-radius: 6px; overflow-x: auto; font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;"
    " font-size: 0.85em; line-height: 1.6; white-space: pre-wrap; word-wrap: break-word; }\n"
    ".transcript .cmd-block { border-top: 1px solid rgb(68,68,68);"
    " padding-top: 0.6em; margin-top: 0.6em; }\n"
    ".transcript .cmd-block:first-child { border-top: none; padding-top: 0; margin-top: 0; }\n"
    ".transcript .comment { color: rgb(220,220,170); }\n"
    ".transcript .prompt { color: rgb(86,156,214); }\n"
    ".transcript .stderr-prefix { color: rgb(244,71,71); }\n"
    ".transcript .exit-code { color: rgb(136,136,136); font-style: italic; }\n"
    ".cast-player { margin: 1em 0; display: flex; justify-content: flex-start; }\n"
)


def ansi_to_html(text: str) -> str:
    """Convert ANSI escape sequences in text to HTML spans.

    Handles SGR sequences (ESC[...m) for:
    - Reset (0)
    - Bold (1)
    - Foreground 30-37, 90-97 (standard + bright)
    - 256-color foreground 38;5;N
    """
    result: list[str] = []
    pos = 0
    open_spans = 0
    ansi_re = re.compile(r"\x1b\[([\d;]*)m")

    for m in ansi_re.finditer(text):
        result.append(html.escape(text[pos : m.start()]))
        pos = m.end()

        codes = m.group(1)
        if not codes or codes == "0":
            result.append("</span>" * open_spans)
            open_spans = 0
            continue

        parts = codes.split(";")
        styles: list[str] = []
        i = 0
        while i < len(parts):
            c = int(parts[i]) if parts[i].isdigit() else 0
            if c == 0:
                result.append("</span>" * open_spans)
                open_spans = 0
            elif c == 1:
                styles.append("font-weight:bold")
            elif 30 <= c <= 37:
                styles.append(f"color:{_ANSI_COLORS_16[c - 30]}")
            elif 90 <= c <= 97:
                styles.append(f"color:{_ANSI_COLORS_16[c - 90 + 8]}")
            elif c == 38 and i + 2 < len(parts) and parts[i + 1] == "5":
                n = int(parts[i + 2]) if parts[i + 2].isdigit() else 0
                if n < 16:
                    styles.append(f"color:{_ANSI_COLORS_16[n]}")
                elif n < 232:
                    n -= 16
                    r = (n // 36) * 51
                    g = ((n % 36) // 6) * 51
                    b = (n % 6) * 51
                    styles.append(f"color:rgb({r},{g},{b})")
                else:
                    v = 8 + (n - 232) * 10
                    styles.append(f"color:rgb({v},{v},{v})")
                i += 2
            else:
                pass
            i += 1

        if styles:
            result.append(f'<span style="{";".join(styles)}">')
            open_spans += 1

    result.append(html.escape(text[pos:]))
    result.append("</span>" * open_spans)
    return "".join(result)


def render_tutorial_block(text: str) -> str:
    """Render a tutorial block with comment lines in yellow and commands in blue."""
    rendered_lines: list[str] = []
    for line in text.splitlines():
        escaped = html.escape(line)
        if line.lstrip().startswith("#"):
            rendered_lines.append(f'<span class="comment">{escaped}</span>')
        elif line.strip():
            rendered_lines.append(f'<span class="prompt">{escaped}</span>')
        else:
            rendered_lines.append(escaped)
    return '<pre class="transcript">' + "\n".join(rendered_lines) + "</pre>"


def render_transcript(text: str, cast_stems: list[str] | None = None) -> str:
    """Render a CLI transcript into styled HTML blocks."""
    lines = text.splitlines()

    blocks: list[list[str]] = []
    current_block: list[str] = []
    for line in lines:
        is_new_block_start = (
            (line.startswith("# ") or line.startswith("$ "))
            and current_block
            and any(bl.startswith("? ") for bl in current_block)
        )
        if is_new_block_start:
            blocks.append(current_block)
            current_block = []
        current_block.append(line)
    if current_block:
        blocks.append(current_block)

    html_parts: list[str] = []
    for block in blocks:
        rendered_lines: list[str] = []
        for line in block:
            if line.startswith("# "):
                rendered_lines.append(f'<span class="comment">{html.escape(line)}</span>')
            elif line.startswith("$ "):
                rendered_lines.append(f'<span class="prompt">{html.escape(line)}</span>')
            elif line.startswith("! "):
                rest = line[2:]
                rendered_lines.append(f'<span class="stderr-prefix">! </span>{ansi_to_html(rest)}')
            elif re.match(r"^\? \d+$", line):
                code = line[2:]
                rendered_lines.append(f'<span class="exit-code">exit code: {html.escape(code)}</span>')
            else:
                rendered_lines.append(ansi_to_html(line))
        html_parts.append('<div class="cmd-block">' + "\n".join(rendered_lines) + "</div>")

    rendered = '<pre class="transcript">' + "\n".join(html_parts) + "</pre>"

    if cast_stems:
        for stem in cast_stems:
            escaped_stem = html.escape(stem)
            anchor = f"#cast-{escaped_stem}"
            link = f'<a href="{anchor}" style="color:rgb(108,182,255);text-decoration:underline">{escaped_stem}</a>'
            rendered = rendered.replace(escaped_stem, link, 1)

    return rendered


def render_test_detail(test_dir: Path, detail_id_prefix: str = "") -> str:
    """Render a test output directory into a self-contained HTML fragment.

    The fragment includes the tutorial block, CLI transcript, and asciinema
    recordings (embedded as base64 data URLs). It can be inserted into any HTML
    page that includes the asciinema player CSS/JS and the DETAIL_CSS styles.

    detail_id_prefix is prepended to HTML element IDs to avoid collisions when
    multiple test details are rendered on the same page.
    """
    parts: list[str] = []

    # Tutorial block
    tutorial_path = test_dir / "tutorial_block.txt"
    if tutorial_path.exists():
        parts.append("<h3>Tutorial block</h3>")
        parts.append(render_tutorial_block(tutorial_path.read_text()))

    # Collect cast files for linkification
    cast_files = sorted(test_dir.glob("*.cast"))
    cast_stems = [f.stem for f in cast_files]

    # Transcript
    transcript_path = test_dir / "transcript.txt"
    if transcript_path.exists():
        parts.append("<h3>CLI transcript</h3>")
        parts.append(render_transcript(transcript_path.read_text(), cast_stems=cast_stems))

    # Asciinema recordings (embedded as base64)
    player_inits: list[str] = []
    for i, cast_file in enumerate(cast_files):
        cast_data = cast_file.read_bytes()
        cast_b64 = base64.b64encode(cast_data).decode("ascii")
        cast_src = f"data:text/plain;base64,{cast_b64}"

        anchor_id = f"{detail_id_prefix}cast-{html.escape(cast_file.stem)}"
        div_id = f"{detail_id_prefix}player-{i}"
        parts.append(f'<h3 id="{anchor_id}">TUI recording: {html.escape(cast_file.stem)}</h3>')
        parts.append(f'<div id="{div_id}" class="cast-player"></div>')
        player_inits.append(
            f"AsciinemaPlayer.create({json.dumps(cast_src)}, "
            f"document.getElementById({json.dumps(div_id)}), "
            f"{{fit: 'none', terminalFontSize: '0.85em', theme: 'asciinema'}});"
        )

    if player_inits:
        init_code = "\n  ".join(player_inits)
        parts.append(
            f"<script>\n"
            f"document.addEventListener('DOMContentLoaded', function() {{\n"
            f"  var check = setInterval(function() {{\n"
            f"    if (typeof AsciinemaPlayer !== 'undefined') {{\n"
            f"      clearInterval(check);\n"
            f"      {init_code}\n"
            f"    }}\n"
            f"  }}, 50);\n"
            f"}});\n"
            f"</script>"
        )

    return "\n".join(parts)
