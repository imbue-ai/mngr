// Workspace accent helpers. The server attaches each workspace's `accent`
// (a #rrggbb string) and `accent_fg` (an RGB triple for the foreground)
// to the SSE workspaces payload; chrome.js / sidebar.js drop those into
// CSS variables. This file exposes the shared palette + a few pure
// helpers (lenient hex normalize, WCAG luminance contrast picker) so the
// settings page can validate user input and the create page can render
// swatches off the same palette the server uses.
//
// The legacy SHA-256-from-agent-id accent (``get`` + ``hueFromAgentId``)
// is retained during the rollout so existing consumers continue to work
// while the read/write paths are wired up; it is removed in a follow-up
// commit once the SSE payload becomes the single source of truth.
//
// Usage:
//   window.mindsAccent.palette                 -> {name: '#rrggbb', ...}
//   window.mindsAccent.defaultColor            -> '#0b292b' (confusion)
//   window.mindsAccent.normalizeHex(value)     -> '#rrggbb' | null
//   window.mindsAccent.pickForegroundForHex(h) -> '0 0 0' | '255 255 255'
//   window.mindsAccent.get(agentId, cb)        -> [legacy] SHA-derived oklch
//   window.mindsAccent.pickForeground()        -> [legacy] no-arg, returns '0 0 0'
(function () {
  // Workspace palette. Mirrors WORKSPACE_PALETTE in templates.py;
  // templates_test.py parses this file and asserts the two stay in
  // lockstep. The 11 named entries come from the Figma source
  // (Minds Early IA Explorations, node 356:4113); ``white`` is added
  // as the 12th so users have a neutral light option distinct from
  // the warm-cream Figma entries.
  var WORKSPACE_PALETTE = {
    indifference: '#000000',
    confusion: '#0b292b',
    courage: '#492222',
    envy: '#3c3d06',
    peace: '#9fbbd3',
    belonging: '#e8a7a8',
    energy: '#cecd0c',
    strength: '#cfc7b3',
    comfort: '#f5d6a0',
    inspiration: '#e9ecd9',
    clarity: '#fcefd4',
    white: '#ffffff',
  };

  var DEFAULT_WORKSPACE_COLOR = WORKSPACE_PALETTE.confusion;

  // WCAG relative luminance threshold below which white text reads
  // better than black on the background; see templates.py for the
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

  // -- Legacy SHA-from-agent-id accent (slated for removal). --
  //
  // Retained during the palette rollout so chrome.js / sidebar.js stay
  // green while the SSE-backed color label propagates through the read
  // and write paths. The values it returns are still OKLCH so any
  // consumer reading from this helper sees the pre-palette colors --
  // intentional, so a half-rolled-out system doesn't mix palette and
  // hash colors in the same UI.
  var ACCENT_L = 85;
  var ACCENT_C = 0.08;
  var colorCache = {};
  var hueCache = {};

  function hueFromAgentId(agentId) {
    var cached = hueCache[agentId];
    if (cached !== undefined) return Promise.resolve(cached);
    var enc = new TextEncoder().encode(agentId);
    return crypto.subtle.digest('SHA-256', enc).then(function (digest) {
      var view = new DataView(digest);
      var hue = view.getUint32(0, false) % 360;
      hueCache[agentId] = hue;
      return hue;
    });
  }

  function pickForeground() {
    return '0 0 0';
  }

  function get(agentId, cb) {
    if (colorCache[agentId] !== undefined) { cb(colorCache[agentId]); return; }
    hueFromAgentId(agentId).then(function (hue) {
      var c = 'oklch(' + ACCENT_L + '% ' + ACCENT_C + ' ' + hue + ')';
      colorCache[agentId] = c;
      cb(c);
    });
  }

  window.mindsAccent = {
    palette: WORKSPACE_PALETTE,
    defaultColor: DEFAULT_WORKSPACE_COLOR,
    normalizeHex: normalizeHex,
    pickForegroundForHex: pickForegroundForHex,
    // Legacy surface -- removed in a follow-up commit.
    get: get,
    pickForeground: pickForeground,
  };
})();
