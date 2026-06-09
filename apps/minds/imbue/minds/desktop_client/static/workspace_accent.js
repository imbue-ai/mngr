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
//   window.mindsAccent.pickForeground() -> "0 0 0" | "255 255 255"
//
// In the common case the server attaches `accent` to each workspace dict
// over SSE and this helper is only used when that field is missing.
(function () {
  // Fixed lightness / chroma for the workspace accent. Mirrored in
  // templates.py. A light / low-saturation tone so the full-width
  // titlebar reads as a chrome surface rather than a saturated highlight.
  var ACCENT_L = 85;
  var ACCENT_C = 0.08;
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

  // ``pickForeground`` -- pure function. All hash-derived accents share the
  // fixed ``ACCENT_L`` lightness today, so the foreground is the same for
  // every agent: black on a light bar. Returns the foreground RGB triple as
  // a space-separated string ("0 0 0" or "255 255 255") so consumers can
  // drop it straight into ``rgb(var(--titlebar-fg) / <alpha>)``. When
  // user-chosen accents with varying L land, this will need to take a
  // lightness argument; for now it's argument-less and synchronous.
  function pickForeground() {
    return ACCENT_L / 100 >= FOREGROUND_L_THRESHOLD ? '0 0 0' : '255 255 255';
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
    get: get,
    pickForeground: pickForeground,
  };
})();
