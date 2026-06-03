/*
 * Tailwind Play CDN config. Loaded BEFORE static/tailwind.js so the runtime
 * JIT engine picks it up on first scan. Extensions (not theme replacements)
 * so the default Tailwind palette (zinc, blue, red, ...) and default
 * sizes/spacing/radii remain available -- production pages currently use
 * those defaults directly and continue to render unchanged until they're
 * migrated onto the semantic tokens below.
 *
 * Class-name conventions (avoid collisions like ``text-text`` /
 * ``border-border``):
 *   - ``bg-surface`` / ``bg-surface-elevated`` / ...  -- backgrounds.
 *   - ``text-ink`` / ``text-ink-secondary`` / ...     -- foreground text.
 *   - ``border-line`` / ``border-line-strong`` / ...  -- borders.
 *   - ``bg-accent`` / ``bg-accent-soft`` / ...        -- interactive accent.
 *   - ``bg-palette-*``                                -- raw Figma swatches
 *                                                        (workspace picker).
 *
 * Semantic color values reference the CSS variables declared in
 * static/tokens.css. The variables flip between light and dark values
 * automatically when <html data-theme> changes (static/theme.js), so
 * markup that uses bg-surface / text-ink / border-line adapts without
 * conditional class lists.
 *
 * darkMode is keyed off the data-theme attribute so any "dark:" variant
 * Tailwind syntax (used by the page authors, not us) co-exists with the
 * semantic-token approach.
 */
window.tailwind = window.tailwind || {};
window.tailwind.config = {
  darkMode: ['selector', '[data-theme="dark"]'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      colors: {
        surface: {
          DEFAULT:   'var(--surface-base)',
          elevated:  'var(--surface-elevated)',
          overlay:   'var(--surface-overlay)',
          sunken:    'var(--surface-sunken)',
        },
        ink: {
          DEFAULT:    'var(--text-primary)',
          secondary:  'var(--text-secondary)',
          muted:      'var(--text-muted)',
          'on-accent': 'var(--text-on-accent)',
        },
        line: {
          DEFAULT:  'var(--border-default)',
          strong:   'var(--border-strong)',
          focus:    'var(--border-focus)',
        },
        accent: {
          DEFAULT: 'var(--accent)',
          hover:   'var(--accent-hover)',
          soft:    'var(--accent-soft)',
        },
        // Workspace palette primitives -- used by the styleguide picker and
        // (future PR) by the new-workspace color picker. Production pages
        // should reach for these only when they need a literal palette
        // swatch; everything else should use the semantic tokens above.
        palette: {
          'forest-deep': 'var(--palette-forest-deep)',
          forest:        'var(--palette-forest)',
          claret:        'var(--palette-claret)',
          olive:         'var(--palette-olive)',
          charcoal:      'var(--palette-charcoal)',
          sky:           'var(--palette-sky)',
          blush:         'var(--palette-blush)',
          lime:          'var(--palette-lime)',
          lavender:      'var(--palette-lavender)',
          sage:          'var(--palette-sage)',
          cream:         'var(--palette-cream)',
          taupe:         'var(--palette-taupe)',
        },
      },
      fontSize: {
        display: ['var(--type-display-size)',  { lineHeight: 'var(--type-display-line)',  fontWeight: 'var(--type-display-weight)' }],
        h1:      ['var(--type-h1-size)',       { lineHeight: 'var(--type-h1-line)',       fontWeight: 'var(--type-h1-weight)' }],
        h2:      ['var(--type-h2-size)',       { lineHeight: 'var(--type-h2-line)',       fontWeight: 'var(--type-h2-weight)' }],
        h3:      ['var(--type-h3-size)',       { lineHeight: 'var(--type-h3-line)',       fontWeight: 'var(--type-h3-weight)' }],
        body:    ['var(--type-body-size)',     { lineHeight: 'var(--type-body-line)',     fontWeight: 'var(--type-body-weight)' }],
        caption: ['var(--type-caption-size)',  { lineHeight: 'var(--type-caption-line)',  fontWeight: 'var(--type-caption-weight)' }],
        label:   ['var(--type-label-size)',    { lineHeight: 'var(--type-label-line)',    fontWeight: 'var(--type-label-weight)' }],
      },
      spacing: {
        // Additive: default 0..96 stays. These are semantic aliases the
        // styleguide can demo and macros can reach for when they want to
        // commit to a specific scale step rather than a raw Tailwind size.
        's-1':  'var(--space-1)',
        's-2':  'var(--space-2)',
        's-3':  'var(--space-3)',
        's-4':  'var(--space-4)',
        's-5':  'var(--space-5)',
        's-6':  'var(--space-6)',
        's-8':  'var(--space-8)',
        's-10': 'var(--space-10)',
        's-12': 'var(--space-12)',
      },
      borderRadius: {
        // Additive scale: keep Tailwind defaults intact (rounded-sm/md/lg/xl
        // stay at the original 2/6/8/12px so unmigrated production pages
        // don't shift pixels) and expose the design-system scale under
        // ``rounded-ds-{sm,md,lg,xl}`` and ``rounded-pill``.
        'ds-sm': 'var(--radius-sm)',
        'ds-md': 'var(--radius-md)',
        'ds-lg': 'var(--radius-lg)',
        'ds-xl': 'var(--radius-xl)',
        pill:    'var(--radius-pill)',
      },
      boxShadow: {
        seam:    'var(--shadow-seam)',
        card:    'var(--shadow-card)',
        popover: 'var(--shadow-popover)',
      },
    },
  },
};
