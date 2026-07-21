Phase 0 of the minds chrome mithril migration (`specs/minds-chrome-mithril-migration/spec.md`): the frontend toolchain, with no user-visible behavior change yet.

New `apps/minds/frontend/` TypeScript package (mithril 2.x, strict tsc, vitest + jsdom) compiled by esbuild into a single IIFE classic script, `static/dist/chrome.bundle.js`, exposing the `window.MindsUI` namespace of mount functions. The swap-engine-compatible mount protocol (boot-state JSON island, synchronous mount, `minds:page-teardown` unmount) is implemented in `frontend/src/mount.ts` and covered by vitest.

`ChromeShell.jinja` and `OverlaySurface.jinja` load the bundle as a shell script (once per document, classic/not deferred, so inline per-page mount calls always find it); the swap engine never re-executes it.

The dev styleguide (`/_dev/styleguide`) gained a "JS components" section mounting a smoke component through the full mount protocol -- the live catalog slot for later converted components.

Build wiring: `build:js` / `watch:js` pnpm scripts run everywhere `build:css` does (`prestart`, `pnpm start` watch, `scripts/build.js`, the visual-diff harness); the bundle is force-included in the minds wheel via `[tool.hatch.build] artifacts`; `static/app.css` declares `frontend/src` as a Tailwind `@source` so component-only utilities are generated.
