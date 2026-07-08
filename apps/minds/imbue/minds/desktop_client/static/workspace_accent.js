// Workspace accent helper. The server attaches each workspace's `accent`
// (a #rrggbb string) to the SSE workspaces payload; chrome.js / sidebar.js
// drop it into the --titlebar-bg CSS variable, and the titlebar derives its
// own contrasting foreground from that color in pure CSS (see
// `.titlebar-surface` in app.css) -- no JS contrast math. The palette lives
// server-side only (workspace_color.py) and reaches the client as
// server-rendered swatches carrying data-color attributes -- there is
// intentionally no JS palette mirror to keep in sync.
//
// This file exposes the one runtime helper the picker pages need: a lenient
// hex normalizer (validating typed input before save), mirroring
// `normalize_workspace_color` in workspace_color.py.
//
// Usage:
//   window.mindsAccent.normalizeHex(value) -> '#rrggbb' | null
(function () {
  var HEX_PATTERN = /^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

  function normalizeHex(value) {
    var match = HEX_PATTERN.exec(String(value).trim());
    if (!match) return null;
    var body = match[1].toLowerCase();
    if (body.length === 3) {
      body = body.split('').map(function (ch) { return ch + ch; }).join('');
    }
    return '#' + body;
  }

  window.mindsAccent = {
    normalizeHex: normalizeHex,
  };
})();
