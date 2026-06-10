// Workspace accent helpers. The server attaches each workspace's `accent`
// (a #rrggbb string) and `accent_fg` (an RGB triple for the foreground)
// to the SSE workspaces payload; chrome.js / sidebar.js drop those into
// CSS variables. This file exposes the shared palette + a few pure
// helpers (lenient hex normalize, WCAG luminance contrast picker) so the
// settings page can validate user input and the create page can render
// swatches off the same palette the server uses.
//
// Usage:
//   window.mindsAccent.palette                 -> {name: '#rrggbb', ...}
//   window.mindsAccent.defaultColor            -> '#0b292b' (confusion)
//   window.mindsAccent.normalizeHex(value)     -> '#rrggbb' | null
//   window.mindsAccent.pickForegroundForHex(h) -> '0 0 0' | '255 255 255'
(function () {
  // Workspace palette. Mirrors WORKSPACE_PALETTE in templates.py;
  // templates_test.py parses this file and asserts the two stay in
  // lockstep. The 11 named entries come from the Figma source
  // (Minds Early IA Explorations, node 356:4113); ``white`` is added
  // as the 12th so users have a neutral light option distinct from
  // the warm-cream Figma entries. Order matters and mirrors
  // WORKSPACE_PALETTE in templates_color (workspace_color.py): the 10
  // chromatic colors first, then the two achromatic neutrals
  // (indifference = black, white) grouped at the end.
  var WORKSPACE_PALETTE = {
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
    indifference: '#000000',
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

  window.mindsAccent = {
    palette: WORKSPACE_PALETTE,
    defaultColor: DEFAULT_WORKSPACE_COLOR,
    normalizeHex: normalizeHex,
    pickForegroundForHex: pickForegroundForHex,
  };
})();
