Solid.js UI migration, Phases 1 and 2.

* Scaffolded a Solid + Vite + Tailwind v4 + Vitest frontend at
  `apps/minds/frontend/`. Ports the `_macros.html` UI primitives
  (Button, ButtonLink, Card, Notice, Spinner, PageContainer, TextInput,
  OptionCard) to Solid components.
* Added a Node SSR sidecar (`frontend/src/main/server.jsx`) that
  exposes `POST /__ssr/render`. Python supervises the subprocess via a
  new `imbue.minds.desktop_client.ssr_sidecar.SsrSidecar` registered on
  the root `ConcurrencyGroup`.
* Migrated the four trivial routes -- `/welcome`, `/login` /
  `/login_redirect`, `/authenticate` (auth error path) -- to Solid.
  The `render_*` shims in `templates.py` now ask the sidecar to render
  and fall back to a client-render shell that inlines the route key +
  props for client hydration when the sidecar is absent.
* Dropped the runtime Tailwind Play CDN. `base.html`, `chrome.html`,
  and `sidebar.html` now load the Vite-built Tailwind v4 bundle from
  `/_static/_dist/assets/globals.css`. The build scans both the Solid
  components and the still-Jinja templates for class usage.
* Removed `scripts/fetch_tailwind.sh` and the corresponding
  `postinstall` hook.
* New Vitest suite covers UI primitives, the SSE store reducer, and
  the workspace-accent helper (with parity goldens against the Python
  implementation). New Python tests assert the SSR fallback shell's
  hydration payload contract.
