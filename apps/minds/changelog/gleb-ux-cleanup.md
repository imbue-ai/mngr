Replaced the desktop client's runtime Tailwind (Play CDN JIT) with a compiled Tailwind v4 build step. The chrome's styles now come from a single minified, tree-shaken stylesheet (`app.min.css`) built ahead of time from `static/app.css` -- no runtime JIT, fully offline, and smaller. This is the foundation for an upcoming light/dark design-token system.

What changed for developers:

- `static/tokens.css` is gone; its hand-written tokens + component CSS now live in `static/app.css` (the Tailwind v4 source entry), which compiles to the gitignored `static/app.min.css`.

- Build the stylesheet with `just minds-css` (replaces `just minds-tailwind`). It also runs automatically on `pnpm install` (postinstall) and is rebuilt before packaging by `scripts/build.js`.

- `just minds-start` now runs the compiler in `--watch` mode alongside Electron, so class changes rebuild live. Because the sheet is compiled, a new/changed Tailwind class only takes effect after a rebuild.

- The compiled sheet is force-included into the wheel via `[tool.hatch.build] artifacts`; `@tailwindcss/cli` and `tailwindcss` are pinned to exact versions.

Began the light/dark design-token system, starting with text colors:

- New themeable text utilities: `text-primary` / `-secondary` / `-tertiary` (text on the current surface) and `text-inverse-*` (text on an inverted surface). Pure black/white at three alpha steps; regular and inverse mirror each other and swap between light and dark.

- Tokens are built in two layers in `app.css`: a per-mode value layer (`:root`/`.light` for light, `.dark` for dark) and an `@theme inline` token layer. Switching the whole app between modes is a single `.dark` class on `<html>` -- no component changes. A `.light` scope can force a light island under a dark ancestor (and vice versa).

- The dev styleguide (`/_dev/styleguide`) gains a light/dark toggle (persisted in `localStorage`, honored app-wide via a pre-paint script in `Base.jinja`) and a "Text color tokens" section showing both modes side by side.

- Migrated the on-light text call sites off the raw zinc ramp to these tokens (`text-zinc-900/800/700` → `text-primary`, `-600/-500` → `text-secondary`, `-400` → `text-tertiary`) across templates, vanilla JS, and the shared button/input class constants. On-dark / inverse text (e.g. log boxes, the primary button label) is intentionally left until the chrome and button stages.
