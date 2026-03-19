"""Simple web server for viewing e2e test outputs.

Serves transcript files and asciinema cast recordings from .test_output/.

Usage:
    uv run python -m imbue.mng.e2e.serve_test_output [--port PORT]
"""

import argparse
import html
import json
import re
from http.server import HTTPServer
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

_TEST_OUTPUT_DIR = Path(__file__).resolve().parent / ".test_output"

_ASCIINEMA_PLAYER_CSS = "https://cdn.jsdelivr.net/npm/asciinema-player@3.15.1/dist/bundle/asciinema-player.css"
_ASCIINEMA_PLAYER_JS = "https://cdn.jsdelivr.net/npm/asciinema-player@3.15.1/dist/bundle/asciinema-player.min.js"

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


def _ansi_to_html(text: str) -> str:
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
    # Match ESC[ followed by semicolon-separated numbers followed by 'm'
    ansi_re = re.compile(r"\x1b\[([\d;]*)m")

    for m in ansi_re.finditer(text):
        # Emit text before this escape
        result.append(html.escape(text[pos : m.start()]))
        pos = m.end()

        codes = m.group(1)
        if not codes or codes == "0":
            # Reset -- close all open spans
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
                # 256-color: 38;5;N
                n = int(parts[i + 2]) if parts[i + 2].isdigit() else 0
                if n < 16:
                    styles.append(f"color:{_ANSI_COLORS_16[n]}")
                elif n < 232:
                    # 6x6x6 color cube
                    n -= 16
                    r = (n // 36) * 51
                    g = ((n % 36) // 6) * 51
                    b = (n % 6) * 51
                    styles.append(f"color:rgb({r},{g},{b})")
                else:
                    # Grayscale
                    v = 8 + (n - 232) * 10
                    styles.append(f"color:rgb({v},{v},{v})")
                i += 2
            i += 1

        if styles:
            result.append(f'<span style="{";".join(styles)}">')
            open_spans += 1

    # Emit remaining text
    result.append(html.escape(text[pos:]))
    result.append("</span>" * open_spans)
    return "".join(result)


def _html_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<link rel="stylesheet" type="text/css" href="{_ASCIINEMA_PLAYER_CSS}">
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 2em; background: #fafafa; color: #222; }}
  h1 {{ font-size: 1.4em; }}
  h2 {{ font-size: 1.1em; margin-top: 2em; }}
  a {{ color: #0066cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  nav {{ margin-bottom: 1.5em; font-size: 0.9em; color: #666; }}
  nav a {{ margin-right: 0.3em; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ margin: 0.3em 0; }}
  .transcript {{ background: #1e1e1e; color: #d4d4d4; padding: 1em; border-radius: 6px; overflow-x: auto; font-family: 'SF Mono', 'Menlo', 'Consolas', monospace; font-size: 0.85em; line-height: 1.6; white-space: pre-wrap; word-wrap: break-word; }}
  .transcript .cmd-block {{ border-top: 1px solid #444; padding-top: 0.6em; margin-top: 0.6em; }}
  .transcript .cmd-block:first-child {{ border-top: none; padding-top: 0; margin-top: 0; }}
  .transcript .comment {{ color: #6a9955; }}
  .transcript .prompt {{ color: #569cd6; }}
  .transcript .stderr-prefix {{ color: #f44747; }}
  .transcript .exit-code {{ color: #888; font-style: italic; }}
  .cast-player {{ margin: 1em 0; }}
  .cast-label {{ font-family: 'SF Mono', 'Menlo', 'Consolas', monospace; font-size: 0.85em; color: #666; margin-bottom: 0.3em; }}
</style>
</head>
<body>
{body}
<script src="{_ASCIINEMA_PLAYER_JS}"></script>
</body>
</html>"""


def _render_transcript(text: str) -> str:
    """Render a transcript into styled HTML blocks."""
    lines = text.splitlines()

    # Split into blocks: a new block starts when a comment or command line
    # follows an exit-code line.
    blocks: list[list[str]] = []
    current_block: list[str] = []
    for line in lines:
        is_new_block_start = (
            (line.startswith("# ") or line.startswith("$ "))
            and current_block
            and any(l.startswith("? ") for l in current_block)
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
                # Color only the "! " prefix red; render the rest with ANSI parsing
                rest = line[2:]
                rendered_lines.append(f'<span class="stderr-prefix">! </span>{_ansi_to_html(rest)}')
            elif re.match(r"^\? \d+$", line):
                code = line[2:]
                rendered_lines.append(f'<span class="exit-code">exit code: {html.escape(code)}</span>')
            else:
                # Stdout lines may also contain ANSI sequences
                rendered_lines.append(_ansi_to_html(line))
        html_parts.append('<div class="cmd-block">' + "\n".join(rendered_lines) + "</div>")

    return '<pre class="transcript">' + "\n".join(html_parts) + "</pre>"


def _index_page() -> str:
    """List all test runs."""
    runs = sorted(
        [d for d in _TEST_OUTPUT_DIR.iterdir() if d.is_dir()],
        reverse=True,
    )
    items = "\n".join(f'<li><a href="/run/{r.name}">{r.name}</a></li>' for r in runs)
    return _html_page("E2E Test Runs", f"<h1>Test Runs</h1>\n<ul>\n{items}\n</ul>")


def _run_page(run_name: str) -> str | None:
    """List all tests in a run."""
    run_dir = _TEST_OUTPUT_DIR / run_name
    if not run_dir.is_dir():
        return None
    tests = sorted(d for d in run_dir.iterdir() if d.is_dir())
    items = "\n".join(f'<li><a href="/run/{run_name}/{t.name}">{t.name}</a></li>' for t in tests)
    nav = '<nav><a href="/">&larr; all runs</a></nav>'
    return _html_page(f"Run {run_name}", f"{nav}<h1>Run {html.escape(run_name)}</h1>\n<ul>\n{items}\n</ul>")


def _test_page(run_name: str, test_name: str) -> str | None:
    """Show transcript and cast players for a single test."""
    test_dir = _TEST_OUTPUT_DIR / run_name / test_name
    if not test_dir.is_dir():
        return None

    nav = (
        f'<nav><a href="/">&larr; all runs</a> / '
        f'<a href="/run/{html.escape(run_name)}">{html.escape(run_name)}</a></nav>'
    )
    parts = [f"{nav}<h1>{html.escape(test_name)}</h1>"]

    # Transcript
    transcript_path = test_dir / "transcript.txt"
    if transcript_path.exists():
        parts.append("<h2>Transcript</h2>")
        parts.append(_render_transcript(transcript_path.read_text()))

    # Cast files -- collect player init calls and run them after the JS loads
    cast_files = sorted(test_dir.glob("*.cast"))
    player_inits: list[str] = []
    for i, cast_file in enumerate(cast_files):
        cast_url = f"/cast/{run_name}/{test_name}/{cast_file.name}"
        parts.append(f"<h2>Recording: {html.escape(cast_file.stem)}</h2>")
        parts.append(f'<div class="cast-label">{html.escape(cast_file.name)}</div>')
        div_id = f"player-{i}"
        parts.append(f'<div id="{div_id}" class="cast-player"></div>')
        player_inits.append(
            f"AsciinemaPlayer.create({json.dumps(cast_url)}, "
            f"document.getElementById({json.dumps(div_id)}), "
            f"{{fit: 'width', theme: 'asciinema', rows: 20}});"
        )

    if player_inits:
        # Defer player creation until after the asciinema JS has loaded
        init_code = "\n  ".join(player_inits)
        parts.append(
            f"<script>\n"
            f"document.addEventListener('DOMContentLoaded', function() {{\n"
            f"  // Wait for asciinema-player JS to load\n"
            f"  var check = setInterval(function() {{\n"
            f"    if (typeof AsciinemaPlayer !== 'undefined') {{\n"
            f"      clearInterval(check);\n"
            f"      {init_code}\n"
            f"    }}\n"
            f"  }}, 50);\n"
            f"}});\n"
            f"</script>"
        )

    return _html_page(f"{test_name} - {run_name}", "\n".join(parts))


class _Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path == "/" or path == "":
            self._respond_html(_index_page())
            return

        # /run/<run_name>
        m = re.fullmatch(r"/run/([^/]+)", path)
        if m:
            page = _run_page(m.group(1))
            if page:
                self._respond_html(page)
            else:
                self._respond_404()
            return

        # /run/<run_name>/<test_name>
        m = re.fullmatch(r"/run/([^/]+)/([^/]+)", path)
        if m:
            page = _test_page(m.group(1), m.group(2))
            if page:
                self._respond_html(page)
            else:
                self._respond_404()
            return

        # /cast/<run_name>/<test_name>/<file.cast>
        m = re.fullmatch(r"/cast/([^/]+)/([^/]+)/([^/]+\.cast)", path)
        if m:
            cast_path = _TEST_OUTPUT_DIR / m.group(1) / m.group(2) / m.group(3)
            if cast_path.is_file():
                self._respond_file(cast_path, "application/json")
            else:
                self._respond_404()
            return

        self._respond_404()

    def _respond_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _respond_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _respond_404(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not found")

    def log_message(self, format: str, *args: object) -> None:
        # Quieter logging
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve e2e test output for viewing")
    parser.add_argument("--port", type=int, default=8742, help="Port to listen on (default: 8742)")
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"Serving e2e test output at http://127.0.0.1:{args.port}")
    print(f"Test output dir: {_TEST_OUTPUT_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
