# Minds chrome layer: migrate dual Jinja/JS rendering to mithril components

## Purpose and scope

The minds desktop client's chrome and modal layers (titlebar, workspace
switcher menu, Landing, Inbox, sharing editor, settings/accounts modals)
are currently rendered twice: once server-side in JinjaX for first paint,
and again client-side in hand-rolled ES5 for live updates. This spec is an
execution plan for collapsing that seam by moving the *live* surfaces to
client-rendered mithril components, while keeping Jinja for the static /
redirect tail (auth flow, consent, welcome, transitional pages).

The plan is phased so each phase is an independently shippable PR that
deletes a specific piece of duplication. It is written to be handed to an
implementing agent with no prior context.

**Out of scope:** the content surface (untrusted agent pages), the
three-WebContentsView process architecture and its IPC contracts
(`electron/main.js` stays as-is), the supertokens auth pages, and any
question of dropping browser mode (browser mode is a first-class surface;
`minds run` on a remote box serves exactly this UI).

## Required reading for the implementing agent

Read these before writing any code, in this order:

1. `apps/minds/imbue/minds/desktop_client/README.md` and
   `apps/minds/imbue/minds/desktop_client/templates/README.md` (the
   component catalog, styling rules, and JinjaX gotchas).
2. `apps/minds/docs/desktop-app.md` (surfaces, accent model, IPC pushes).
3. `apps/minds/style_guide.md` and `apps/minds/CLAUDE.md`.
4. The three files that ARE the current seam:
   `desktop_client/static/chrome.js` (swap engine, titlebar context,
   browser-mode menu), `desktop_client/static/sidebar_workspace_row.js`
   (the shared row builder), and
   `desktop_client/templates/pages/Landing.jinja` (767 of its 1000 lines
   are inline JS).
5. For the mithril style to imitate: the default-workspace-template repo,
   `apps/system_interface/frontend/src/` (closure components, manual
   `m.redraw()`, Vite+TS). Do NOT copy its global-redraw pain points;
   see "Risks".

## Current state (what exists today)

Numbers as of branch `jsify` (2026-07):

- Jinja tree: 7,382 lines across `templates/`, of which ~2,674 lines are
  inline `<script>` blocks. `static/*.js`: ~3,100 lines (framework-free
  ES5). `templates.py`: 2,045 lines of render functions + class-string
  constants + `ICONS_16`/`ICONS_12` SVG path dicts.
- Every live surface renders twice. Landing: Jinja rows + inline JS
  patchers (`applyMindLiveness`, `applyRowHealth`, `applyBackupBadge`)
  + full-page refetch through the swap engine when the row set drifts.
  Titlebar: server-seeded breadcrumb + `classifyContent`/
  `applyTitlebarContext` re-deriving identical state. Switcher menu: two
  Jinja panel copies (`ChromeShell.jinja`, `Sidebar.jinja`) + two driver
  scripts (`chrome.js` inline, `sidebar.js` modal) sharing only the row
  builder. Sharing: `SharingEditor.jinja` + `sharing.js` rebuilding the
  same heading/ACL. Inbox: server HTML fragments refetched and
  innerHTML-swapped.
- 19 copies of the badge-pill class string; 22 files with "keep in sync"
  / "mirrors" comments; the `arrow-up-right` SVG path exists verbatim in
  both `templates.py` and `sidebar_workspace_row.js`.
- Four SSE consumers of `/_chrome/events` (chrome.js, sidebar.js,
  Landing inline, electron/main.js), each with its own reconnect loop.
- A ~125-line hand-rolled SPA router (the "swap engine" in chrome.js:
  fetch -> DOMParser -> adoptNode -> manual script re-execution ->
  pushState) plus the `minds:page-teardown` event protocol.
- Two host modes everywhere: Electron (`window.minds` IPC bridge,
  separate WebContentsViews for content and overlay) and plain browser
  (content iframe, in-document menu, full-page fallbacks instead of
  modals, own EventSource). The branch is sprinkled as
  `window.minds && window.minds.X ? ... : ...` across dozens of sites.

## Target architecture

### What stays and what changes

| Piece | Disposition |
|---|---|
| Electron process architecture (`main.js`, preloads, surface-routing, overlay view, IPC) | Unchanged |
| `Base.jinja` / `OverlaySurface.jinja` / `ChromeShell.jinja` shells | Stay Jinja; shrink to scaffolds that emit a boot-state JSON island + mount containers |
| Auth flow (`templates/auth/`, SigninModal), Consent, Welcome, LoginRedirect, OauthClose, AuthError | Stay Jinja (redirect-based, static, security-sensitive) |
| Creating, Destroying, Recovery pages | Stay Jinja for now (simple, transitional; full document lifecycle is a feature). May convert later; not in this plan |
| Create form | Stays Jinja (forms gain the least; server-side error re-render is fine) |
| DevStyleguide | Stays Jinja; gains a section that mounts the JS components as their live catalog |
| Titlebar, switcher menu, Landing, Inbox list/shell, sharing editor, providers panel | Become mithril components (Phases 2-6) |
| Swap engine + `minds:page-teardown` | KEPT during the whole migration (see mount protocol). Converted pages ride it unchanged. Removal is a possible far-future cleanup, not a goal |
| `mngr_forward` loading page, content surface | Untouched |

### Frontend package and toolchain

- Source lives at `apps/minds/frontend/src/` (mirrors
  default-workspace-template's layout). TypeScript, strict.
- Dependencies added to the existing `apps/minds/package.json` (one pnpm
  package, no new workspace): `mithril ^2.2.x`, `@types/mithril`, and
  dev-deps `esbuild`, `typescript`, `vitest`, `jsdom`.
- Build: **esbuild, single entry, IIFE output** ->
  `imbue/minds/desktop_client/static/dist/chrome.bundle.js`, exposing a
  `window.MindsUI` namespace of mount functions. IIFE + classic script
  tags is a deliberate choice: the swap engine re-executes page scripts
  by re-creating tags, and classic scripts have synchronous, ordered
  execution semantics there; ES modules do not. Do not use `type=module`
  for anything the swap engine touches.
- pnpm scripts: `build:js` (esbuild --minify), `watch:js`,
  `test:frontend` (vitest). Wire `build:js` everywhere `build:css`
  already runs (`prestart`, `scripts/build.js`, the `just` recipes that
  invoke `pnpm run build:css` -- grep for them). Add a `watch:js` line to
  the `start` concurrently invocation.
- Packaging: the bundle follows the exact `app.min.css` pattern -- it is
  gitignored (`apps/minds/.gitignore` already has `dist/`) and
  force-included in the wheel via the `artifacts` list in
  `apps/minds/pyproject.toml` (add
  `imbue/minds/desktop_client/static/dist/*`).
- Loading: `ChromeShell.jinja` adds
  `<script src="/_static/dist/chrome.bundle.js"></script>` to its SHELL
  scripts section (the part that runs once per document, next to
  chrome.js -- NOT inside `#local-page-scripts`). Overlay-hosted modal
  pages (separate documents) include the same tag themselves.

### Mount protocol (swap-engine compatibility)

This is the core mechanism; get it right in Phase 0 and never deviate.

A converted page's Jinja shell renders:

```html
<div id="app-landing"></div>
<script type="application/json" id="minds-boot-state">{{ boot_state | tojson }}</script>
```

(The `tojson`-in-a-JSON-island pattern is already established --
`Base.jinja`'s `#minds-sentry-config` and Landing's
`mind_liveness_by_agent_id` use it; Jinja's `tojson` escapes `<` so it is
safe inside a script element.)

and, inside its `#local-page-scripts` block, a classic inline script:

```html
<script>window.MindsUI.mountLanding(document.getElementById('app-landing'));</script>
```

`MindsUI.mountX(el)` must:

1. Read and parse `#minds-boot-state` from the *current* document, seed
   the store with it (see below), and `m.mount(el, Component)`
   synchronously.
2. Register a `minds:page-teardown` listener (once) that calls
   `m.mount(el, null)` and drops the store subscription -- this is how a
   component survives being swapped out by the swap engine, exactly like
   today's inline scripts guard their timers.

Because the bundle is in the shell scripts (loaded once, cached) and the
mount call is in the page scripts (re-run per swap), converted and
unconverted pages coexist behind the same swap engine indefinitely.

### Chrome-state contract and boot islands

Today the SSE `workspaces` payload (built in `app.py`'s `/_chrome/events`
generator: workspaces with id/name/accent/liveness/account/is_remote/
is_stale, `destroying_agent_ids`, `remote_workspace_states`,
`has_accounts`, plus `providers_state`, `requests`, `auth_status`,
`system_interface_status`, `appearance` events) is the de-facto chrome
data contract, but it is assembled as loose dicts. Phase 1 formalizes it:

- Pydantic models in a new `desktop_client/chrome_state.py` (e.g.
  `ChromeWorkspacesPayload`, `ChromeProvidersPayload`,
  `ChromeRequestsPayload`, and a `ChromeBootState` bundling the current
  snapshot of all of them plus page-specific extras).
- The SSE route and the page render handlers both build payloads through
  these models, so the boot island and the SSE stream can never drift.
- Mirror the shapes as TypeScript interfaces in
  `frontend/src/chrome_state.ts`. Keep the two files adjacent in review;
  a comment in each points at the other.

### Host adapter (`frontend/src/host.ts`)

One interface, two implementations, chosen once at bundle init by
`!!window.minds`:

```ts
interface Host {
  onChromeEvent(cb: (e: ChromeEvent) => void): void;  // IPC push | shared EventSource singleton (one reconnect loop for the whole document)
  navigate(url: string): void;                        // minds.navigateContent | swap-engine-aware fallback
  goBack(): void;
  openWorkspaceInNewWindow(agentId: string): void;    // IPC | window.open
  showWorkspaceContextMenu(agentId, x, y): void;      // IPC | no-op
  confirmStopMind(agentId, name): Promise<boolean>;   // native dialog via IPC | window.confirm
  openModal(kind, params): void;                      // IPC (overlay view) | Phase 7 in-document iframe; until then full-page navigate
  closeModal(): void;
}
```

For browser-mode `navigate`, reuse the swap engine rather than
reimplementing it: chrome.js exposes its existing `navigateContent` as
`window.__mindsNavigateContent` (one-line change), and browserHost
delegates to it, falling back to `window.location`. All the sprinkled
`window.minds && ...` branches in converted surfaces are replaced by host
calls; do not add new sprinkled branches.

### Store (`frontend/src/store.ts`)

Module-level state + explicit notification, mithril-style:

- Holds: workspaces list, accent/name cache by agent id, requests
  count/ids, providers state, system-interface status by agent, auth
  status, backup-health warnings (port of `backup_health.js`), and
  optimistic overrides (pending mind start/stop targets, pending provider
  toggles) with the same clobber-guard semantics the Landing inline JS
  implements today (an interim SSE tick must not revert an in-flight
  optimistic transient).
- `seed(bootState)` for mount-time hydration; `subscribe(cb)`;
  mutations call `m.redraw()`.
- Exactly one host event subscription per document, shared by all mounted
  components.

### Components and styling

- Components live in `frontend/src/views/`, closure-component style as in
  default-workspace-template. Planned inventory: `Icon` (ports
  `ICONS_16`/`ICONS_12` path data to `frontend/src/icons.ts` -- which
  becomes the single source; `templates.py` keeps its dicts only for the
  pages that remain Jinja, with a comment cross-linking the two until the
  Jinja consumers shrink enough to inline), `Badge`, `Spinner`,
  `WorkspaceRow`, `WorkspaceMenu`, `TitleBar`, `LandingPage`,
  `InboxList`, `SharingEditor`, `ProvidersPanel`.
- Styling: Tailwind utility strings in `class:` attributes.
  `static/app.css` already declares `static/*.js` as a Tailwind
  `@source`; add another `@source` for `apps/minds/frontend/src/` so
  utilities written in components are generated (the relative path from
  `app.css` is `../../../../frontend/src` -- verify by checking that a
  utility used only in a component appears in the rebuilt
  `app.min.css`). The shared recipe
  classes (`.minds-card`, `.spinner`, `.minds-tooltip`) keep working
  unchanged; as each JinjaX twin loses its last Jinja consumer, fold the
  recipe into the component and delete the bridging class.
- Follow the existing scale/tokens (`type-*`, `bg-fill-*`, etc.). No new
  zinc shades, radii, or weights.

## Invariants (must not regress, verified every phase)

1. **Flashless first paint.** Every page paints complete on arrival: the
   server-seeded `--titlebar-bg` accent, pre-opened breadcrumb, and (for
   Landing) visible rows. Client mounting must be synchronous from the
   boot island. The titlebar keeps its server-rendered skeleton until
   Phase 4's verification protocol proves the mounted replacement cannot
   flash.
2. **Both hosts, always.** Every converted surface works in Electron and
   plain browser in the same PR. No "Electron first, browser later"
   within a phase.
3. **Behavior parity checklists** (each phase lists its own below):
   keyboard (Escape), backdrop dismissal, drag regions
   (`modal-open` no-drag), accent transitions, the Badge 99+ cap, the
   `hidden`-attribute-vs-class rules, optimistic-update semantics.
4. **Security posture.** No `innerHTML` with interpolated data in
   components (mithril vnodes escape by default -- keep it that way);
   the sharing editor's "DOM methods, not innerHTML" rationale carries
   over. Boot islands are `type="application/json"` script tags, never
   interpolated JS.
5. **CI green** (`just test-offload`), ruff + ratchets clean, a changelog
   entry in `apps/minds/changelog/` per PR, and the visual-diff harness
   run for any phase touching templates.

## Migration phases

Each phase is one PR on its own branch off `main`, landed in order.
Each phase DELETES its dead code in the same PR (do not carry the old
path forward "just in case") and ports/retires the corresponding
`templates_test.py` cases.

### Phase 0 -- Toolchain (S)

**Work:** frontend scaffolding (`frontend/src/`, `tsconfig.json`), pnpm
deps + scripts, esbuild IIFE build to `static/dist/chrome.bundle.js`,
wheel `artifacts` entry, bundle `<script>` tag in `ChromeShell.jinja` +
`OverlaySurface.jinja` shells, vitest config with one real test, and a
smoke mount: a "JS components" section in `DevStyleguide.jinja` mounting
a trivial component through the full mount protocol (island + mount call
+ teardown listener).

**Work (CI):** find where the existing electron unit tests
(`pnpm test:unit`) run in CI (check `.github/workflows/`) and wire
`pnpm test:frontend` and `pnpm build:js` in alongside (the e2e job needs
the bundle built before it runs). Workflow changes owe a
`dev/changelog/` entry.

**Acceptance:** `pnpm build:js && pnpm test:frontend` green locally and
in CI; app boots in Electron (`just minds-start`) and plain browser with
the bundle served;
the styleguide section renders; swapping to/from `/_dev/styleguide` (it
is not a hub page -- verify full-navigation behavior unchanged);
`uv build` (or the packaging equivalent) includes the bundle.

### Phase 1 -- Chrome-state contract, host adapter, store (M)

**Work:** `chrome_state.py` pydantic models adopted by the SSE route and
exposed as a `build_chrome_boot_state()` helper; `boot_state` prop
support in `ChromeShell.jinja`; `frontend/src/{chrome_state.ts,host.ts,
store.ts}` with vitest coverage of store transitions (accent cache merge,
optimistic liveness guard, provider-toggle pending logic, requests
count); the one-line chrome.js export of `navigateContent`.

**Deletes:** nothing user-visible yet (pure foundations).

**Acceptance:** Python unit tests for the models (payloads byte-identical
to the previous dict shapes -- snapshot the SSE output before/after);
vitest green; both modes boot unchanged.

### Phase 2 -- Shared primitives + workspace switcher menu (M)

**Work:** `Icon`, `Badge`, `Spinner`, `WorkspaceRow`, `WorkspaceMenu`
(grouping + account headers + the `SidebarBottom` entries, which need
auth/account state from the store). Electron: `Sidebar.jinja` becomes a
positioning shell (keeps its server-side anchor math from query params)
with a mount container; `sidebar.js` shrinks to nothing (dismissal moves
into the component via host). Browser: `ChromeShell.jinja`'s inline
`#sidebar-menu` block becomes a mount container; chrome.js keeps only the
toggle/anchor computation and calls `MindsUI.mountWorkspaceMenu`.
`dev_styleguide.js`'s row samples switch to the component.

**Deletes:** `sidebar_workspace_row.js` (176 lines, incl. the duplicated
arrow SVG path), `sidebar.js`'s `renderWorkspaces` (and most of the
file), chrome.js's `renderWorkspaces`, the duplicated panel markup.

**Behavior checklist:** grouping/sort (Private first), current-row
highlight tracking the accent-source workspace (settings screens
included), open-in-new-window arrows (Electron only, not on current/
remote rows), remote rows non-navigable with location badge, stale +
backup-warning dots live-updating, context menu (Electron), backdrop +
Escape dismissal in both modes, menu anchor alignment (the -24px offset).

### Phase 3 -- Landing page (L)

**Work:** `LandingPage` component covering rows (normal, destroying,
remote with connecting/unreachable/error chips), start/stop/restart with
optimistic transients and confirm-dialog via host, recovery routing
(stopped -> `intent=restart`), backup badges (port `loadBackupStatus`'s
fetch fan-out into a store service), unlock banner, providers panel
(`ProvidersPanel`), bottom-left launchers, empty + discovering states.
`Landing.jinja` shrinks to shell + island + mount; the island carries the
chrome boot state plus `account_email`/`extra_account_count`/
`locked_account_emails`/`mngr_forward_origin`. Row-set changes rerender
from the store -- the refetch-on-drift path and the freshness/teardown
timer plumbing go away (component lifecycle owns them).

**Deletes:** ~767 lines of Landing inline JS, the Jinja row markup, the
`minds:refresh-local-page` dispatches for row drift (the event itself
stays -- sync-unlock and other pages still use it).

**Acceptance:** the full behavior checklist above exercised manually in
both modes (use the e2e workspace runner for a real workspace);
`templates_test.py` Landing cases ported to vitest or rewritten to assert
the boot island JSON; visual-diff scenario for Landing updated (see
Testing strategy -- this phase carries the harness extension).

### Phase 4 -- Titlebar (M, highest polish risk)

**Work:** `TitleBar` component (breadcrumb, icon-tabs, back/home
visibility, requests badge, window controls, accent surface toggling).
`classifyContent` + `applyTitlebarContext` + `updateRequestsBadge` +
accent application move into the component/store as selectors over
`lastContentUrl` (still pushed by main over `content-url-changed` /
derived from the browser poll -- host exposes an `onContentUrlChange`).
The ChromeShell titlebar markup stays server-rendered as the pre-mount
skeleton; the component mounts into the same container and takes over.
Server seeding of accent + crumb via the island keeps first paint
identical.

**Deletes:** the titlebar-context half of chrome.js (~350 lines). After
this phase chrome.js is: swap engine, mode setup, and host glue.

**Acceptance -- no-flash protocol (mandatory):** Playwright captures at
first paint with CPU throttling for (a) Electron cold boot to Home, (b)
browser reload on a workspace-scoped page (accent must never flash
neutral), (c) hub-page swaps, (d) workspace -> settings -> workspace
navigation. Compare against pre-phase captures. Plus the drag-region and
`modal-open` checks, Badge cap, welcome-page home-button hiding.

### Phase 5 -- Inbox list/shell (M)

**Work:** add card summaries to the requests payload (extend
`ChromeRequestsPayload`; the SSE already pushes count + ids);
`InboxList` component for the left pane + shell (selection, deny-in-
flight visual state, auto-open checkbox, keep_open semantics,
auto-advance). The right detail pane STAYS a server-rendered HTML
fragment fetched per selection (`/inbox/detail/<id>`) -- the latchkey
permission forms (`PermissionsForm` etc.) are server-side POST forms and
are explicitly not being componentized in this plan.

**Deletes:** the list-fragment refetch machinery and
`render_inbox_list_fragment` (the detail fragment route stays).

**Acceptance:** approve/deny flows incl. auto-advance vs dismiss
(keep_open both ways), notification-driven single-request opens,
SSE-driven list refresh, empty state, backdrop/Escape.

### Phase 6 -- Sharing editor (S/M)

**Work:** `SharingEditor` component replacing the `SharingEditor.jinja`
innards + `sharing.js` (heading with plain-links mode, ACL rows, submit/
disable states, in-place state refresh after Update/Disable). Both the
full `/sharing/...` page and the sharing modal mount it. Check what the
current in-place refresh fetches; if it is HTML, add a small JSON state
endpoint.

**Deletes:** `sharing.js` (397 lines), the dual-rendered editor markup.

### Phase 7 -- Browser-mode modal parity (M) -- CHECKPOINT: confirm with Gleb before starting

**Work:** browserHost implements `openModal` by mounting a `ModalHost`
component that renders an in-document backdrop + an iframe of the same
server modal page Electron's overlay view loads (`/settings/modal`,
`/accounts/modal`, `/inbox`, ...). This deliberately mirrors Electron's
overlay-iframe architecture instead of componentizing modal content --
the settings/accounts content stays JinjaX (`AppSettingsSections`) and
works in both hosts unchanged. Escape/backdrop close the ModalHost. The
full-page twins (`/settings`, `/accounts`, help/inbox full pages) remain
as deep-link routes, but in-app browser-mode entry points (launchers,
titlebar buttons) switch to `host.openModal`.

**Why:** browser users currently lose workspace context on every modal
open (full-page navigation). This closes the largest UX gap between the
modes with near-zero new rendering code.

**Acceptance:** every modal opens over the content iframe in browser
mode; sign-in modal keeps its `MINDS_AUTH_NAV` return-to flow; full-page
routes still work when hit directly.

### Phase 8 -- Guardrails + docs (S)

**Work:** a new ratchet in `apps/minds`'s `test_ratchets.py` (use
`/writing-ratchet-tests`) counting `createElement|innerHTML|
insertAdjacentHTML` in `desktop_client/static/*.js` and inline template
`<script>` blocks, locked at the post-migration count, so hand-rolled DOM
building cannot silently return. Update `templates/README.md` (the
"CSS classes for JS-rendered surfaces" section becomes "components vs
remaining Jinja"), `desktop-app.md`, and the styleguide. Sweep for dead
"keep in sync" comments.

## Testing strategy

- **vitest** (`frontend/src/**/*.test.ts`): store transitions, host
  adapters (jsdom + fake EventSource/window.minds), component rendering
  via `m.mount` into jsdom, ported assertions from `templates_test.py`
  for converted surfaces.
- **pytest**: `chrome_state.py` model tests; route tests for converted
  pages assert the boot-island JSON (parse it out of the HTML) instead of
  markup; `templates_test.py` cases for converted surfaces are ported or
  deleted in the same phase PR -- never left asserting dead templates.
- **Visual diff** (`apps/minds/scripts/visual_diff.py`): today it renders
  scenario HTML by calling Python render functions and screenshots the
  static files. Converted pages render client-side, so Phase 3 must
  extend the harness: serve the capture directory (plus `/_static`)
  from a local HTTP server and wait for a `data-minds-mounted` attribute
  the mount functions set, instead of loading `file://` HTML. Budget this
  into Phase 3; it is not optional -- visual diff is the regression net
  for every later phase.
- **e2e** (`apps/minds/test/e2e/` Playwright + the e2e workspace
  runner): at least one flow per phase (open menu and switch workspace;
  landing start/stop; inbox approve; browser-mode modal open in
  Phase 7). Browser mode is the cheap harness: prefer plain-browser
  Playwright for component behavior, Electron e2e for host-adapter
  wiring.
- **Manual verification** before declaring any phase done: run the real
  app in both modes (`just minds-start`; browser via the printed login
  URL) and walk the phase's behavior checklist. tmux-drive where
  interactive (per CLAUDE.md, do not crystallize tmux checks into
  pytest).

## Per-PR discipline (repo rules that apply to every phase)

- One phase per PR; branch off `main`; never rebase/amend.
- Changelog entry per touched project (`apps/minds/changelog/<branch>.md`;
  `dev/changelog/` too if CI/root files change).
- Run `uv run ruff check`, `uv run ruff format`, and the minds ratchets
  before each commit; run the full desktop_client subtree tests before
  minor commits and `just test-offload` before finishing.
- Commits carry both co-author trailers (Claude + Sculptor).
- Do not push or open PRs without explicit permission from Gleb.

## Open decisions (defaults chosen; confirm at the marked points)

1. **esbuild vs Vite** -- default esbuild (IIFE output is first-class and
   the swap engine requires classic scripts; dwt's Vite serves a true
   SPA, which this is not). Revisit only if the frontend later needs
   code-splitting.
2. **TypeScript vs JS** -- default TypeScript (the test surface is being
   rebuilt anyway; dwt precedent).
3. **Phase 7 go/no-go** -- product decision; ask before starting.
4. **`ICONS_16` single-sourcing** -- default: TS file becomes canonical,
   Python keeps a shrinking copy for remaining Jinja pages. Alternative
   (codegen from one source) only if drift actually bites.
5. **Inbox detail pane componentization** -- explicitly deferred; the
   fragment hybrid is the intended end state of this plan.

## Risks and mitigations

- **First-paint flash on converted pages.** Mitigation: synchronous
  mount from inline islands, server-rendered titlebar skeleton until
  Phase 4's protocol passes, throttled-CPU Playwright captures as the
  gate.
- **Swap engine vs bundle execution.** The bundle must live in shell
  scripts (once per document) and mounts in page scripts (per swap); a
  bundle tag inside `#local-page-scripts` would re-execute the IIFE on
  every swap. The mount protocol section is normative.
- **Mithril global-redraw pitfalls** (from dwt experience): guard
  scroll/selection-sensitive regions with `onbeforeupdate`; never mix
  keyed and unkeyed siblings; keep `m.redraw()` calls in the store, not
  scattered. The minds chrome has no streaming transcripts, so exposure
  is low, but the Inbox list (SSE-refreshed while a user reads a detail)
  is the one surface to watch.
- **Behavior drift during ports.** The inline JS encodes years of edge
  cases in comments (optimistic-guard semantics, `.hidden`-vs-inline-flex
  cascade traps, recovery `intent=restart`, deny-in-flight states). The
  phase checklists above enumerate them; the implementing agent must
  treat the old code's comments as the requirements document and port
  them as component/store comments where the constraint survives.
- **Test-surface regression.** The biggest single cost. Rule: no phase
  PR merges with a net loss of coverage for its surface -- ported vitest
  cases + island-asserting route tests replace the template tests in the
  same PR.
- **Packaging misses.** The wheel `artifacts` addition and the todesktop
  build order (`build:js` before packaging) are easy to forget; Phase 0's
  acceptance includes verifying the built artifact contains the bundle.

## Rough sequencing and effort

Phases 0-2 are the foundation (S+M+M) and deliver the first visible
deduplication (the switcher). Phase 3 (L) is the bulk of the value.
Phases 4-6 (M+M+S/M) finish the live surfaces. Phase 7 (M) is the
browser-mode payoff and needs a go/no-go. Phase 8 (S) locks it in.
Estimated total: 9 PRs (Phases 0-8). Do not parallelize phases that
touch chrome.js (0-4 are strictly sequential; 5 and 6 can proceed in
parallel after 4; 7 waits for the go/no-go; 8 is last).
