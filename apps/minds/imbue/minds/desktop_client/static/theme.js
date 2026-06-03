// Luminance-driven theme switcher.
//
// Reads --workspace-surface from <body>'s computed style, parses it into a
// relative luminance, and writes data-theme="dark"|"light" on <html>. The
// semantic CSS variables in static/tokens.css are scoped under
// :root[data-theme=...] so the component palette flips automatically.
//
// Usage:
//   window.mindsTheme.refresh()  -- recompute after changing --workspace-surface
//
// The pure-black non-workspace baseline (no --workspace-surface set) lands
// on dark theme because luminance(#000) === 0. Workspace-scoped pages with
// a pastel/cream surface land on light theme.
(function () {
  // Accept hex (#rgb, #rrggbb), rgb(...) / rgba(...), and oklch(L% C H) /
  // oklch(L C H) -- the three shapes the server and JS emit today. Anything
  // else falls through to dark theme as a safe default.
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

  function pickTheme(surfaceValue) {
    if (!surfaceValue) return 'dark';
    var trimmed = surfaceValue.trim();
    var okl = oklchLightness(trimmed);
    if (okl !== null) return okl >= 0.6 ? 'light' : 'dark';
    var rgb = parseRgb(trimmed);
    if (rgb) return srgbLuminance(rgb) >= 0.5 ? 'light' : 'dark';
    return 'dark';
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
    return surface.trim();
  }

  function refresh() {
    var theme = pickTheme(readSurface());
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.style.colorScheme = theme;
  }

  window.mindsTheme = { refresh: refresh, pickTheme: pickTheme };

  // First pass runs as soon as the script is parsed so the <html> attribute
  // is set before the first paint. A second pass on DOMContentLoaded covers
  // the case where <body>'s inline --workspace-surface only becomes
  // readable once the body element exists.
  refresh();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', refresh, { once: true });
  }
})();
