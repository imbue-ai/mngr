Added `specs/minds-chrome-mithril-migration/spec.md`: a phased execution plan for migrating the minds desktop client's chrome and modal layers from dual Jinja/JS rendering to client-rendered mithril components (host adapter + chrome-state store + per-page mount protocol compatible with the existing swap engine), including per-phase deletions, acceptance criteria, testing/packaging strategy, and open decisions.

Logged a spec conflict in `specs/uncertainties.md`: `specs/minds-webcontentsview-refactor/spec.md` describes a superseded sidebar/forwarding architecture; the code is treated as authoritative.

Phase 0 of the migration (toolchain): added a `test-minds-frontend` CI job (Node 24.15.0 + pnpm 10.33.4) running the frontend typecheck + vitest suite, the esbuild bundle build, and the previously-unwired Electron main-process node unit tests (`pnpm test:unit`).

New `just minds-js` recipe (esbuild bundle build, sibling of `minds-css`); `minds-test-electron` / `minds-test-electron-flow` now depend on both. The minds e2e Modal snapshot bake (`scripts/snapshot_minds_e2e_state.py`) compiles the frontend bundle alongside the Tailwind sheet.

`just minds-start` now surfaces `MINDS_REMOTE_DEBUGGING_PORT` when it is set in the shell: it prints the Chrome DevTools Protocol target-list URL (`http://127.0.0.1:<port>/json`) so a CDP client can attach to the running desktop app for local inspection.

The migration spec (`specs/minds-chrome-mithril-migration/spec.md`) gained a "Phases 9+ -- full JS rendering" section recording the decision to supersede the original "keep Jinja for the static tail" scoping: every remaining page body (auth cluster, settings/accounts, workspace settings, recovery, the create form, the inbox detail/permission forms, the dev styleguide) moves to mithril in lettered phases, ending with the shell collapse that deletes JinjaX entirely and reduces `templates.py` to a minimal document builder + boot-state seeding. Phase 9A (Creating + Destroying) landed with this update.
