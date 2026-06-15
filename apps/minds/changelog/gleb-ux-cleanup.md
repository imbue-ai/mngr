Replaced the desktop client's runtime Tailwind (Play CDN JIT) with a compiled Tailwind v4 build step. The chrome's styles now come from a single minified, tree-shaken stylesheet (`app.min.css`) built ahead of time from `static/app.css` -- no runtime JIT, fully offline, and smaller. This is the foundation for an upcoming light/dark design-token system.

What changed for developers:

- `static/tokens.css` is gone; its hand-written tokens + component CSS now live in `static/app.css` (the Tailwind v4 source entry), which compiles to the gitignored `static/app.min.css`.

- Build the stylesheet with `just minds-css` (replaces `just minds-tailwind`). It also runs automatically on `pnpm install` (postinstall) and is rebuilt before packaging by `scripts/build.js`.

- `just minds-start` now runs the compiler in `--watch` mode alongside Electron, so class changes rebuild live. Because the sheet is compiled, a new/changed Tailwind class only takes effect after a rebuild.

- The compiled sheet is force-included into the wheel via `[tool.hatch.build] artifacts`; `@tailwindcss/cli` and `tailwindcss` are pinned to exact versions.
