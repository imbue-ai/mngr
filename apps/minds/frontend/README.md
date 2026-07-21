# Minds chrome frontend (mithril components)

TypeScript + mithril source for the desktop client's client-rendered chrome
surfaces. esbuild compiles `src/index.ts` into a single IIFE classic script,
`imbue/minds/desktop_client/static/dist/chrome.bundle.js`, which exposes the
`window.MindsUI` namespace of mount functions. The execution plan and the
normative mount protocol live in
`specs/minds-chrome-mithril-migration/spec.md`.

## Commands (run from `apps/minds/`)

- `pnpm run build:js` -- compile the bundle (minified). Runs automatically
  wherever `build:css` runs (`prestart`, `scripts/build.js`, `just minds-js`).
- `pnpm run watch:js` -- rebuild on change (wired into `pnpm start`).
- `pnpm run test:frontend` -- typecheck (`tsc --noEmit`) + vitest.

## Rules that keep the bundle swap-engine compatible

- The bundle is loaded ONCE per document from a shell scripts section
  (`ChromeShell.jinja` / `OverlaySurface.jinja`), never from
  `#local-page-scripts` -- the chrome.js swap engine re-executes page scripts
  per swap, and the IIFE must not re-run.
- Every mount function follows the protocol in `src/mount.ts`: parse the
  page's `#minds-boot-state` JSON island, mount synchronously (complete first
  paint, no post-load pop-in), and unmount on the `minds:page-teardown` window
  event so swapped-out pages release the container.
- Classic scripts only. Do not emit `type="module"` output; the swap engine
  relies on synchronous, ordered execution when it re-creates script tags.
- Styling is Tailwind utility strings in `class:` attributes (plain
  space-separated strings, not hyperscript `div.foo.bar` selectors -- the
  Tailwind scanner reads string literals). `static/app.css` declares this
  tree as an `@source`; a new utility takes effect after `build:css` reruns.
- No `innerHTML` with interpolated data -- mithril vnodes escape by default;
  keep it that way.

Component style mirrors default-workspace-template's
`apps/system_interface/frontend/src/`: closure components
(`export function X(): m.Component<Attrs>`), colocated `*.test.ts` vitest
files, manual `m.redraw()` only from shared state code (later phases' store),
never scattered in views.
