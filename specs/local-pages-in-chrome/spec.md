# Minds App: Local Pages in Chrome (Content Surface = Agent-Only)

> Branch: `gleb/content-in-chrome`. Follow-on to
> [`specs/minds-webcontentsview-refactor/spec.md`](../minds-webcontentsview-refactor/spec.md),
> which established the current three-surface architecture. Read that first for
> history; note the current code has drifted from it (the standalone sidebar
> view became the shared `modalView`, `content-relay-preload.js` was added, and
> agent content now loads via `/goto/{id}/` + `agent-*.localhost` subdomains
> instead of `/forwarding/`).

## Goal

Stop the `contentView` from rendering two fundamentally different kinds of
content. Today it renders **both** trusted local/native desktop-client pages
(Landing, Create, Settings, Creating, Accounts, recovery, ...) **and**
untrusted foreign agent content (`agent-*.localhost`). This change makes the
**content surface host agent content only**, and moves every trusted local page
onto the **chrome surface** (the trusted app shell).

After this change the trust boundary of the app matches its view boundary:
trusted app (chrome + local pages) on one side, untrusted agent content on the
other.

## Motivation

Two problems, both real:

1. **Trust mismatch.** Local pages are 100% trusted minds-backend pages, yet
   because they share the `contentView` with untrusted agent content they are
   forced to use the minimal `content-relay-preload.js`, which "exposes NOTHING
   to the page via contextBridge" (`electron/content-relay-preload.js:1-9`).
   So `Landing.jinja` / `Create.jinja` talk to the app through a narrow
   `window.postMessage({type:'minds:...'})` relay whose every message type must
   be allowlisted in the preload **and** re-validated in main
   (`electron/main.js:~2989-3037`), while pages in the chrome/modal surfaces
   just call `window.minds.*` (a.k.a. `ln.*`) directly. Every new capability a
   local page needs is relay boilerplate.

2. **Content-type mixing.** One `WebContentsView` whose partition, preload,
   navigation guards, error handling, and accent logic all have to straddle
   "trusted local Jinja page" and "foreign agent origin" is harder to reason
   about than two views that each do one job.

The historical reason local pages lived in the content surface was a **single
navigable surface / unified back-forward history**. Product decision recorded
here: **back/forward is not load-bearing** -- the buttons are kept only as a
safety net for "funny places", real navigation is always explicit
(`navigate-content` / `go-home` -> `loadURL`, `electron/main.js:2875-2903`).
That removes the main argument for keeping local pages in the content surface.

## Current architecture (grounded)

Three `WebContentsView` layers per window (`createBundleWebContentsViews`,
`electron/main.js:445-498`):

| Surface | Renders today | Trust | Preload | Session |
|---|---|---|---|---|
| **chromeView** | *only* `/_chrome` (never navigates; loads at `main.js:1140,1238,1308,2700`) | trusted | `preload.js` (full `window.minds`) | default |
| **contentView** | **local pages AND agent pages** | untrusted | `content-relay-preload.js` (caged relay) | `persist:workspace-content` |
| **modalView** | overlays: `/_chrome/sidebar`, `/inbox`, `/help`, sign-in | trusted | `preload.js` | default |

Facts that make this change feasible (verified in code):

- **SSE lives in the main process,** not the chrome renderer: `runChromeSSELoop`
  maintains one connection to `/_chrome/events` (`main.js:1769`), and
  `latestChromeState` (`main.js:115-123`) exists explicitly to re-prime
  "newly-loaded chrome and modal webContents ... without opening their own SSE
  connection." **So the chrome renderer can reload freely and be re-primed** --
  a full-page navigation in chromeView does not drop the event stream.
- **Cookie sync** (`main.js:2325-2375`) exists partly because auth currently
  happens *inside* `contentView`'s partition: a watcher copies `minds_session`
  from the content partition to the default session so chrome/modal see the
  signed-in state, and a startup step copies it the other way to feed `/goto`.
- **Browser (non-Electron) mode is a supported, live mode.** The same
  `/_chrome` page (`templates/pages/Chrome.jinja`) is served to browsers, where
  the content surface is a `#content-frame` **iframe** (`Chrome.jinja:120-123`)
  rather than a WebContentsView. In Electron the iframe (and the browser-mode
  floating sidebar) are hidden via JS (`Chrome.jinja:1-8`), and the real
  content surface is the `contentView` WebContentsView. Any change here must
  keep both modes working.

## Guiding principle (applies to both modes)

> The **content surface** -- the `contentView` WebContentsView in Electron, the
> `#content-frame` iframe in browser mode -- renders **only foreign agent
> content** (`agent-*.localhost`). Every trusted local/native page is rendered
> by the **chrome surface** (the trusted app shell, `preload.js` in Electron).

The content surface is shown only while the current screen is an agent path and
hidden/empty otherwise.

## Target architecture

Redraw the same three surfaces by content type. No fourth `WebContentsView`
(honors the standing "three surfaces" constraint):

| Surface | Renders after change | Trust | Preload | Session |
|---|---|---|---|---|
| **chromeView** | titlebar **+ all local/native pages** | trusted | `preload.js` | default |
| **contentView** | **agent content only**, shown on agent paths, hidden otherwise | untrusted | `content-relay-preload.js` | `persist:workspace-content` |
| **modalView** | overlays (unchanged) | trusted | `preload.js` | default |

### Electron realization

- The titlebar becomes a shared server-side layout that wraps a local page
  body. `chromeView` **navigates full-page among local routes** (`/`,
  `/create`, `/settings`, `/creating/{id}`, `/accounts`, `/agents/{id}/recovery`,
  error/loading, ...). Each render is titlebar-layout + page body, so the
  titlebar comes along with every local page.
- On agent paths, `chromeView` shows the titlebar with an empty content region,
  and `contentView` is made visible and layered on top as the inset accent card
  (unchanged `updateBundleBounds` geometry, `main.js:390-412`).
- The brief titlebar re-render on a local navigation is absorbed by
  main-process SSE re-priming from `latestChromeState`; mac traffic lights are
  OS-drawn so they don't flicker. See Open Questions for the fallback if this
  flicker is unacceptable.

### Browser realization

- Same shared titlebar layout wraps local pages; the browser simply does normal
  full-page navigation among local routes (each page includes the titlebar).
- The `#content-frame` iframe is present **only on agent routes**, pointing at
  the agent origin. Local routes render their body inline in the served page --
  no iframe.
- This actually *removes* browser-mode complexity: the "persistent chrome page +
  iframe carries everything + back/forward via `iframe.contentWindow.history` +
  title polling" model collapses into ordinary multi-page navigation with an
  iframe used solely for foreign agent content.

## Expected behavior

### Electron

- Startup unchanged through shell.html; once the backend is up, `chromeView`
  loads `/` (Landing, now titlebar + landing body) and `contentView` is created
  but hidden.
- Clicking a workspace (Landing row, sidebar, or `navigate-content`) shows and
  navigates `contentView` to the agent origin; the titlebar tints to the
  workspace accent (existing `onContentNavigate` / accent logic moves to key off
  the content surface's agent navigation).
- Home / Create / Settings navigate `chromeView`; `contentView` is hidden (and
  torn down -- see Open Questions on hide-vs-teardown).
- Local pages call `window.minds.*` directly; no `postMessage` relay hop.
- `contentView` only ever loads an `agent-*.localhost` origin; a guard rejects
  any bare-`localhost` load (defense in depth, impossible to add today).

### Browser

- Local routes render server-side with the titlebar; agent routes render the
  titlebar layout wrapping the `#content-frame` iframe at the agent origin.
- Sidebar / inbox / help continue to work (browser-mode floating sidebar and
  overlays unchanged).

## Implementation plan

### A. Templates / backend (`apps/minds/imbue/minds/desktop_client`)

1. **Extract a shared chrome/titlebar layout** from `templates/pages/Chrome.jinja`
   (the `#minds-titlebar` block, the browser-mode `#sidebar-backdrop`/
   `#sidebar-menu`, the accent/body-bg wiring, and the `chrome.js` script hook)
   into a reusable layout component (e.g. `templates/ChromeShell.jinja` wrapping
   `Base.jinja`).
2. **Local pages extend the chrome layout** instead of bare `Base.jinja`, so
   each local page renders titlebar + body. Local page templates:
   `Landing.jinja`, `Create.jinja`, `Creating.jinja`, `Settings.jinja`,
   `WorkspaceSettings.jinja`, `Accounts.jinja`, `Sharing.jinja`,
   `Destroying.jinja`, `Consent.jinja`, `Welcome.jinja`, plus the auth/recovery
   pages. (`Inbox.jinja`, `Help.jinja`, `Sidebar.jinja`, `SigninModal.jinja`
   stay modal-only and do **not** get the chrome layout.)
3. **Agent-route render:** on an agent path in browser mode, serve the chrome
   layout wrapping the `#content-frame` iframe (today's `Chrome.jinja` body).
   The `/_chrome` route may become the agent-path/wrapper render; local routes
   render their own page. Reconcile so there is exactly one titlebar per served
   page.
4. **Swap local-page IPC from relay to bridge:** in `Landing.jinja` /
   `Create.jinja` (and any other local page using `window.postMessage('minds:...')`),
   replace the relay calls with `window.minds.*` (guarded by `!!ln`, matching
   `chrome.js` / `sidebar.js` style). Confirmed relay senders to migrate:
   `open-workspace-in-new-window`, `confirm-stop-mind` (Landing),
   `open-signin-modal` (Create).
5. Keep `chrome.js` working when it is now the main-frame script of a local page
   (it already guards on `var isElectron = !!ln`).

### B. Electron (`apps/minds/electron/main.js`, `preload.js`, `content-relay-preload.js`)

1. **`chromeView` becomes navigable among local routes.** Route
   `go-home` / `navigate-content` to a local URL -> `chromeView.loadURL`; route
   an agent URL -> show + `contentView.loadURL`. Add a `parseWorkspaceId`-based
   classifier (`main.js:141-156` already exists) at the navigation entry points
   to pick the surface.
2. **`contentView` shows/hides by path** (`setVisible`), created hidden. It only
   ever loads agent origins; add an assertion/guard that rejects non-agent
   loads.
3. **Move accent / title / current-workspace updates** (currently
   `onContentNavigate`, `main.js:644-679`) to key off the content surface's
   agent navigation and off chrome's local navigation, so the titlebar tints
   correctly whichever surface changed.
4. **Simplify cookie sync:** auth now happens on the chrome surface (default
   session), so the content->default watcher (`setupContentPartitionCookieSync`,
   `main.js:2331-2350`) can likely be dropped; keep only the default->content
   push that seeds `minds_session` for `/goto` forwarding (`main.js:2638-2648`).
   Verify `/goto` still receives the cookie it needs.
5. **`preload.js`:** local pages now use it; add any bridge methods a local page
   needs that previously went through the relay (e.g. a real
   `openWorkspaceInNewWindow` / `confirmStopMind` if not already present on the
   bridge).
6. **`content-relay-preload.js`:** shrink the allowlist to only what foreign
   agent content legitimately needs (e.g. `open-request-modal`,
   `preview-workspace-accent`); drop message types only local pages used.
7. **Startup restore / deep-link routing** (`main.js:2718`, `startup-routing.js`,
   session restore) must branch on path type: local path -> chrome, agent path
   -> content.
8. **Error / loading / recovery** overlays move to (or stay on) the chrome
   surface, which is cleaner than tearing down the content surface.

## Phases

Each phase is independently shippable and verifiable.

- **Phase 1 -- Templating (backend only).** Extract the shared chrome layout;
  make local pages extend it; serve them standalone with the titlebar. Migrate
  local-page relay calls to `window.minds` (guarded, still works in browser).
  Result: local pages render with the titlebar as full pages; Electron still
  loads them in `contentView` (unchanged wiring) -- app keeps working.
- **Phase 2 -- Electron rewiring.** Point `chromeView` at local routes; make
  `contentView` agent-only + show/hide; move accent/title/current-workspace
  updates; branch startup/deep-link routing. Result: the de-mixed architecture
  is live in Electron.
- **Phase 3 -- Lockdown + cleanup.** Add the `contentView` non-agent-origin
  guard; simplify cookie sync (drop the content->default watcher); shrink the
  relay allowlist; delete now-dead relay/branching code. Lock in any reduced
  ratchet counts (`uv run pytest --inline-snapshot=trim <test_ratchets.py>`).

## What should get simpler or be deleted

- Relay message types used only by local pages (allowlist entries in
  `content-relay-preload.js` + their re-validation handlers in `main.js`).
- The content->default `minds_session` watcher (`setupContentPartitionCookieSync`).
- "Is this a local page or an agent page?" branching in the content surface's
  navigation/error/accent handling.
- Browser-mode "persistent chrome + iframe-carries-everything" machinery
  (title polling, `iframe.contentWindow.history` back/forward).

## Testing strategy

- **Unit / template tests** (`templates_test.py`): each local page renders with
  the titlebar block; agent-path render still emits the `#content-frame` iframe;
  modal pages (`Inbox`/`Help`/`Sidebar`/`SigninModal`) do **not** get the chrome
  layout.
- **Integration** (`test_desktop_client.py`, `api_v1_test.py`): local routes
  return full pages with titlebar markup; `/goto` still installs the subdomain
  cookie after the sync change; auth flow sets `minds_session` in the default
  session and chrome/modal see it.
- **Electron acceptance** (`minds-electron-acceptance-test`, run via offload):
  navigating Home <-> workspace <-> Settings shows the correct surface;
  `contentView` is hidden off-agent-paths; a workspace tint appears only on
  agent paths.
- **Manual (tmux/GUI, not crystallized):** verify no jarring titlebar flicker on
  local navigation; verify browser mode renders local pages and the agent
  iframe correctly; verify the relay is unreachable from a local page (it now
  uses the bridge).
- **Ratchets:** confirm counts don't increase; trim any that decrease.

## Open questions / decisions

1. **Titlebar flicker on local navigation (Electron).** Expected minor
   (localhost render + SSE re-prime + OS traffic lights). If unacceptable, the
   fallback is to keep `chromeView` a persistent `/_chrome` shell and render
   local pages into an inner region via same-origin fetch-and-swap -- more code,
   so only if needed. **Decision needed after Phase 2 measurement.**
2. **Hide vs. tear down `contentView` when leaving an agent.** Tear-down matches
   today's behavior and is simplest; keep-alive-hidden gives instant resume at a
   resource cost. Recommend tear-down first, keep-alive as a later perf tweak.
   The one-window-per-workspace invariant (`main.js:688-695`) holds either way.
3. **Local pages lose the rounded content "card"** and become full-bleed inside
   the window (the card was only ever meaningful for accent-tinted agent
   content). Confirm this is the desired visual.
4. **Browser-mode SSE reconnects per local navigation.** Cheap and native to
   EventSource, but confirm no missed-event edge cases across a local
   navigation.
5. **Any local page that must be visible simultaneously with agent content?**
   None found in this analysis, but not exhaustively audited. If one exists, the
   show/hide model needs rethinking. **Blocker to confirm before Phase 2.**
6. **Local-page storage in the content partition.** Grep local pages for
   `localStorage`/cookie use tied to `persist:workspace-content`; anything found
   moves to the default session.

## Risks

- Cross-mode divergence: the trust win is Electron-only; browser mode gains only
  the de-mixing/cleanup. Keep the served templates identical across modes to
  avoid two code paths.
- Accent/title correctness: these updates currently hang off one
  `did-navigate`; splitting the source across two surfaces is the most
  error-prone part -- cover it with the acceptance test above.
