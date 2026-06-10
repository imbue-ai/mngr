// Workspace accent helpers. The server attaches each workspace's `accent`
// (a #rrggbb string) and `accent_fg` (an RGB triple for the foreground)
// to the SSE workspaces payload; chrome.js / sidebar.js drop those into
// CSS variables. The palette itself lives server-side only
// (workspace_color.py) and reaches the client as server-rendered
// swatches carrying data-color attributes -- there is intentionally no
// JS palette mirror to keep in sync.
//
// This file exposes the two pure helpers the picker pages need at
// runtime: a lenient hex normalizer (validating typed input before
// save) and the WCAG luminance contrast picker (computing the titlebar
// foreground for instant local previews, where waiting on the server
// round-trip would defeat the purpose). Both mirror their Python
// counterparts in workspace_color.py.
//
// Usage:
//   window.mindsAccent.normalizeHex(value)     -> '#rrggbb' | null
//   window.mindsAccent.pickForegroundForHex(h) -> '0 0 0' | '255 255 255'
(function () {
  // WCAG relative luminance threshold below which white text reads
  // better than black on the background; see workspace_color.py for the
  // derivation (sqrt(1.05 * 0.05) - 0.05, rounded).
  var FOREGROUND_LUMINANCE_THRESHOLD = 0.179;

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

  function linearize(channel) {
    if (channel <= 0.03928) return channel / 12.92;
    return Math.pow((channel + 0.055) / 1.055, 2.4);
  }

  function pickForegroundForHex(hex) {
    var r = parseInt(hex.slice(1, 3), 16) / 255;
    var g = parseInt(hex.slice(3, 5), 16) / 255;
    var b = parseInt(hex.slice(5, 7), 16) / 255;
    var luminance =
      0.2126 * linearize(r) +
      0.7152 * linearize(g) +
      0.0722 * linearize(b);
    return luminance > FOREGROUND_LUMINANCE_THRESHOLD ? '0 0 0' : '255 255 255';
  }

  window.mindsAccent = {
    normalizeHex: normalizeHex,
    pickForegroundForHex: pickForegroundForHex,
  };
})();
