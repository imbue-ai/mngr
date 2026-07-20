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
