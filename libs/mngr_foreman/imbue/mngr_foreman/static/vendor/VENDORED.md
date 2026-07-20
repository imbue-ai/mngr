# Vendored frontend assets

## marked.min.js
- **Library:** [marked](https://github.com/markedjs/marked) — Markdown parser/compiler.
- **Version:** 12.0.2 (pinned)
- **Source:** https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js
- **License:** MIT
- **Why vendored:** foreman has no build step and the server enforces no external
  network for the UI; the assistant-message markdown renderer (`app.js`
  `renderMarkdown`) needs marked available locally. Raw HTML in markdown is
  escaped via a `renderer.html` override (see `app.js` `setupMarked`) so the
  parser cannot inject raw HTML into the page.

To update: download the same URL at a new pinned version, replace the file, and
bump the version above.

---

## xterm.min.js, xterm.min.css (web terminal page)
- **Library:** [xterm.js](https://github.com/xtermjs/xterm.js) — terminal emulator for the browser.
- **Version:** 5.3.0 (pinned)
- **Source:** https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js
  and https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css
- **License:** MIT

## xterm-addon-fit.min.js
- **Library:** [xterm-addon-fit](https://github.com/xtermjs/xterm.js/tree/master/addons/xterm-addon-fit) — fits the terminal to its container.
- **Version:** 0.8.0 (pinned, compatible with xterm 5.3)
- **Source:** https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js
- **License:** MIT
- **Used by:** `terminal.html` / `app.js` `initTerminal`, bridged to the pty
  websocket at `/ws/agents/<name>/terminal`.

---

## highlight.min.js, highlight-atom-one-dark.min.css (code syntax highlighting)
- **Library:** [highlight.js](https://github.com/highlightjs/highlight.js) — syntax highlighter.
- **Version:** 11.9.0 (pinned; "common languages" build)
- **Source:** https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js
  and .../build/styles/atom-one-dark.min.css
- **License:** BSD-3-Clause
- **Used by:** `app.js` `highlightCodeIn` — lazy-injected on the first fenced code block.

## katex/ (katex.min.js, katex.min.css, auto-render.min.js, fonts/*.woff2)
- **Library:** [KaTeX](https://github.com/KaTeX/KaTeX) — LaTeX math rendering.
- **Version:** 0.16.9 (pinned)
- **Source:** https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/{katex.min.js,katex.min.css,contrib/auto-render.min.js,fonts/*.woff2}
- **License:** MIT
- **Used by:** `app.js` `renderMathIn` — lazy-injected only when text looks like math.
  `katex.min.css` references `fonts/KaTeX_*.woff2` relatively (20 woff2 vendored under `katex/fonts/`).

## mermaid.min.js (diagrams)
- **Library:** [mermaid](https://github.com/mermaid-js/mermaid) — text-to-diagram. UMD build (global `mermaid`).
- **Version:** 10.9.1 (pinned)
- **Source:** https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js
- **License:** MIT
- **Used by:** `app.js` `renderMermaidIn` — lazy-injected on the first ```mermaid fence;
  `initialize({theme:"dark", securityLevel:"strict"})`.

## fonts/*.woff2 + atkinson.css (typography)
- **Library:** [Atkinson Hyperlegible](https://www.brailleinstitute.org/freefont/) and
  Atkinson Hyperlegible Mono (Braille Institute).
- **Version:** Google Fonts snapshot — Atkinson Hyperlegible v12, Atkinson Hyperlegible Mono v8.
- **Source:** https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400;1,700
  and family=Atkinson+Hyperlegible+Mono:wght@400;700 (woff2 pulled from fonts.gstatic.com; the
  @font-face `url()`s in `atkinson.css` were rewritten to `/static/vendor/fonts/`).
- **License:** SIL Open Font License 1.1 (free for any use, incl. embedding).
- **Used by:** `foreman.css` (`@import`), `--prose` (Atkinson Hyperlegible) for all prose/UI and
  `--mono` (Atkinson Hyperlegible Mono) for code/diffs/tool output; also the xterm terminal
  `fontFamily` in `app.js` `initTerminal`.
