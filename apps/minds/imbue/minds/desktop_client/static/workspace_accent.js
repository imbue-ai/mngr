// Per-agent accent color helper. Mirrors workspace_accent() in
// templates.py: SHA-256 over the agent id, first four bytes mod 360 picks
// the OKLCH hue, fixed L/C match server. Loaded by both
// templates/pages/Chrome.jinja and templates/pages/Sidebar.jinja (and
// any other page that needs a client-side accent
// fallback) so the logic lives in one place rather than being copy-pasted
// into every per-page script.
//
// Usage:
//   window.mindsAccent.get(agentId, function (color) { ... });
//   window.mindsAccent.getForeground(agentId, function (rgb) { ... });
//   window.mindsAccent.pickForeground(L) -> "0 0 0" | "255 255 255"
//
// In the common case the server attaches `accent` to each workspace dict
// over SSE and this helper is only used when that field is missing.
(function () {
  // Fixed lightness / chroma for the workspace accent. Mirrored in
  // templates.py. 80% L / 0.1 C is calmer than the prior 65 / 0.15 so
  // the full-width titlebar reads as a chrome surface rather than a
  // saturated highlight.
  var ACCENT_L = 80;
  var ACCENT_C = 0.1;
  // Threshold for choosing a contrasting foreground. Above this L the
  // background is light enough that black ink reads better; below it,
  // white. OKLCH lightness is perceptual so a fixed cutoff works
  // across the hue wheel.
  var FOREGROUND_L_THRESHOLD = 0.5;

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

  // ``pickForeground`` -- pure function over the OKLCH lightness of the
  // accent. Returns the foreground RGB triple as a space-separated string
  // ("0 0 0" or "255 255 255") so consumers can drop it straight into
  // ``rgb(var(--titlebar-fg) / <alpha>)``.
  function pickForeground(oklchL) {
    return oklchL >= FOREGROUND_L_THRESHOLD ? '0 0 0' : '255 255 255';
  }

  function get(agentId, cb) {
    if (colorCache[agentId] !== undefined) { cb(colorCache[agentId]); return; }
    hueFromAgentId(agentId).then(function (hue) {
      var c = 'oklch(' + ACCENT_L + '% ' + ACCENT_C + ' ' + hue + ')';
      colorCache[agentId] = c;
      cb(c);
    });
  }

  // Foreground for the hash-derived accent. All hash-derived accents
  // share the same lightness (ACCENT_L), so the foreground is the same
  // for every agent today; the per-agent signature is kept so future
  // user-chosen accents with varying lightness slot in without changing
  // the call sites.
  function getForeground(_agentId, cb) {
    cb(pickForeground(ACCENT_L / 100));
  }

  window.mindsAccent = {
    get: get,
    getForeground: getForeground,
    pickForeground: pickForeground,
  };
})();
