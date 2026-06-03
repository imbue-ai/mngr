Added the implementation plan for the Solid.js UI migration
(`blueprint/solidjs-ui-migration/plan-solidjs-ui-migration.md`),
describing the page-by-page port of the Jinja2 desktop client to a
Solid + Vite + Tailwind v4 frontend rendered by a Node SSR sidecar.

Build the Solid frontend bundles before the Electron e2e test runs --
`just minds-test-electron` now invokes `pnpm --dir apps/minds frontend:build`
first, and the CI workflow's `test-docker-electron` job gains a matching
"Build minds frontend bundles" step. Without this, the Electron app
launches against an empty `static/_dist/` and the SSR sidecar can't
start, so the create form's `#create-form` selector never appears and
the Playwright `wait_for_selector` hits its 10s timeout.
