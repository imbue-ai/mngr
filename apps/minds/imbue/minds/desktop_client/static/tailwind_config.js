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
        // Workspace palette primitives -- the 11 named Figma "Workspace
        // colors" plus the brand seeds. Used by the styleguide picker and
        // (future PR) by the new-workspace color picker. Production pages
        // should reach for these only when they need a literal swatch;
        // everything else should use the semantic tokens above.
        ws: {
          indifference: 'var(--ws-indifference)',
          confusion:    'var(--ws-confusion)',
          courage:      'var(--ws-courage)',
          envy:         'var(--ws-envy)',
          peace:        'var(--ws-peace)',
          belonging:    'var(--ws-belonging)',
          energy:       'var(--ws-energy)',
          strength:     'var(--ws-strength)',
          comfort:      'var(--ws-comfort)',
          inspiration:  'var(--ws-inspiration)',
          clarity:      'var(--ws-clarity)',
        },
        palette: {
          'brand-red':      'var(--palette-brand-red)',
          'brand-red-soft': 'var(--palette-brand-red-soft)',
          'accent-blue':    'var(--palette-accent-blue)',
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
        // Pixel-named keys (matches the --space-N = N pixels convention
        // in tokens.css). The ``s-`` prefix avoids collision with default
        // Tailwind sizes (e.g. ``p-8`` is 32px in default Tailwind, but
        // ``p-s-8`` resolves to --space-8 = 8px).
        's-4':  'var(--space-4)',
        's-8':  'var(--space-8)',
        's-12': 'var(--space-12)',
        's-16': 'var(--space-16)',
        's-20': 'var(--space-20)',
        's-24': 'var(--space-24)',
        's-32': 'var(--space-32)',
        's-40': 'var(--space-40)',
        's-48': 'var(--space-48)',
      },
      borderRadius: {
        // Pixel-named additive scale. Default Tailwind ``rounded-sm/md/lg/xl``
        // stay at their original 2/6/8/12px (so unmigrated production pages
        // don't shift pixels); the design-system scale lives under
        // ``rounded-ds-{6,8,12,16}`` + ``rounded-pill``.
        'ds-6':  'var(--radius-6)',
        'ds-8':  'var(--radius-8)',
        'ds-12': 'var(--radius-12)',
        'ds-16': 'var(--radius-16)',
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
