The minds desktop client's UI is being rebuilt as a client-rendered Mithril single-page app that talks to the backend over one long-lived WebSocket, replacing the server-rendered JinjaX pages and the per-window Server-Sent-Events stream.

What lands in this change:

- A new `apps/minds/frontend/` package (Mithril + TypeScript + Vite + Tailwind) whose bundle is built into the wheel at build time. It renders the whole app -- titlebar, sidebar/switcher, the workspace iframe, and every hub page (Home, Create, Creating, Settings, Accounts, AI keys, workspace options and settings, backups, destroying, recently-destroyed, recovery, requests inbox, get-help, welcome, consent) -- with client-side routing, so hub navigation is instant and the titlebar never rebuilds.

- One WebSocket per window (`/ui/ws`) carries all live state (workspaces, accounts, providers, requests, per-workspace health, discovery health, one-shot events). Because a WebSocket does not count against the browser's six-connections-per-host limit, this removes the parallel-request contention that made page loads crawl under the old SSE + full-page-fetch model. The connection reconnects with backoff and re-syncs from a fresh snapshot; a schema-version mismatch triggers a single reload.

- First paint is seeded from a bootstrap document inlined into the page, so workspace tiles and the accent color render with no extra round trips and no color pop-in.

- Login is a minimal dependency-free page (the one-time-code flow is unchanged). Requests-inbox auto-open is now decided in the app rather than the Electron main process, which no longer consumes any HTTP/SSE stream of its own.

- Recovery is now something you click into (logs plus a manual Restart) rather than a place the app throws you: the automatic stuck-workspace redirect, the redirect latches, the periodic re-assert timers, and the auto-restart-on-observe behavior are gone.

Behavior parity with the old UI is preserved, including the latchkey permission dialogs, the file-sharing native picker (with a plain path field in browser mode), and the bug-report / agent-assist help flows.

- The visual-diff harness (`apps/minds/scripts/visual_diff.py`) gains a `capture-spa` mode: it builds the frontend bundle, serves the SPA index per route with a deterministic fixture bootstrap built from the real wire models, and screenshots every route. SPA captures are namespaced separately from the legacy JinjaX captures so both coexist across the migration.
