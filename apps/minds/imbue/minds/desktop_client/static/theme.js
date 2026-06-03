// Luminance-driven theme switcher.
//
// Reads --workspace-surface from <body>'s computed style, parses its
// relative luminance, and writes data-theme="dark"|"light" on <html>. The
// semantic CSS variables in static/tokens.css are scoped under
// :root[data-theme=...] so the component palette flips automatically.
//
// Usage:
//   window.mindsTheme.refresh()  -- recompute after changing --workspace-surface
//
// Behavior on a page with no workspace surface signal (no --workspace-surface,
// no --workspace-accent fallback): we leave <html data-theme> alone. base.html
// ships data-theme="light" so semantic tokens resolve to the light defaults
// declared directly on :root in tokens.css -- which match the existing
// bg-zinc-50/text-zinc-900 bodies of unmigrated production pages.
//
// Loaded with `defer`, so it runs after the document is parsed and after
// tokens.css has applied. A single call at script time is enough.
(function () {
  // Accept hex (#rgb, #rrggbb), rgb(...) / rgba(...), and oklch(L% C H) /
  // oklch(L C H) -- the three shapes the server and JS emit today. Anything
  // else is treated as "no signal" and leaves the markup default in place.
  function parseRgb(value) {
    var m = /^#([0-9a-f]{3,8})$/i.exec(value);
    if (m) {
      var hex = m[1];
      if (hex.length === 3) {
        return [parseInt(hex[0] + hex[0], 16), parseInt(hex[1] + hex[1], 16), parseInt(hex[2] + hex[2], 16)];
      }
      if (hex.length === 6 || hex.length === 8) {
        return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
      }
    }
    var rgb = /^rgba?\(\s*(\d+(?:\.\d+)?)\s*[, ]\s*(\d+(?:\.\d+)?)\s*[, ]\s*(\d+(?:\.\d+)?)/.exec(value);
    if (rgb) {
      return [parseFloat(rgb[1]), parseFloat(rgb[2]), parseFloat(rgb[3])];
    }
    return null;
  }

  // sRGB relative luminance per WCAG. Inputs are 0..255. Returns 0..1.
  function srgbLuminance(rgb) {
    function chan(v) {
      var c = v / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * chan(rgb[0]) + 0.7152 * chan(rgb[1]) + 0.0722 * chan(rgb[2]);
  }

  // OKLCH lightness is already perceptual; >= 0.6 reads as a "light" surface.
  function oklchLightness(value) {
    var m = /^oklch\(\s*([0-9.]+)(%?)/i.exec(value);
    if (!m) return null;
    var l = parseFloat(m[1]);
    return m[2] === '%' ? l / 100 : l;
  }

  // Returns 'light' / 'dark' / null. ``null`` means "no usable signal, leave
  // the markup default alone".
  function pickTheme(surfaceValue) {
    if (!surfaceValue) return null;
    var trimmed = surfaceValue.trim();
    if (!trimmed) return null;
    var okl = oklchLightness(trimmed);
    if (okl !== null) return okl >= 0.6 ? 'light' : 'dark';
    var rgb = parseRgb(trimmed);
    if (rgb) return srgbLuminance(rgb) >= 0.5 ? 'light' : 'dark';
    return null;
  }

  function readSurface() {
    // The server inlines --workspace-surface on <body>; fall back to <html>
    // and to a literal --workspace-accent (the existing per-workspace var
    // used on pages that haven't been migrated to --workspace-surface yet).
    var body = document.body;
    var styles = body ? window.getComputedStyle(body) : null;
    var surface = styles && styles.getPropertyValue('--workspace-surface');
    if (!surface || !surface.trim()) {
      var rootStyles = window.getComputedStyle(document.documentElement);
      surface = rootStyles.getPropertyValue('--workspace-surface');
    }
    if (!surface || !surface.trim()) {
      var bodyAccent = styles && styles.getPropertyValue('--workspace-accent');
      surface = bodyAccent || '';
    }
    return (surface || '').trim();
  }

  function refresh() {
    var theme = pickTheme(readSurface());
    if (theme === null) return;
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.style.colorScheme = theme;
  }

  window.mindsTheme = { refresh: refresh, pickTheme: pickTheme };
  refresh();
})();
