# In-memory persistent terminals for the minds dockview

## Overview

- Today the dockview "New terminal" tab spawns a fresh `bash` on every connection (via the ttyd `workdir.sh` dispatch), so all terminal state is lost on tab-close, iframe reload, or dockview-layout restore. This makes terminal tabs feel disposable and prevents agents from inspecting what a user ran.
- Fix: back each ad-hoc terminal with its own **named tmux session** (`terminal-1`, `terminal-2`, …) created via `tmux new-session -A` (attach-or-create), so the tab reattaches to the same live session across close, reload, and restore.
- **tmux is the source of truth.** Any session on the shared default socket whose name does *not* start with `MNGR_PREFIX` ("mngr-") is a user terminal; `mngr-`prefixed sessions are agents. Running `tmux ls` shows agents and terminals together — deliberate transparency, no separate registry to keep in sync.
- Persistence is **in-memory only**: sessions survive tab-close, iframe reload, dockview restore, and ttyd-service restart, but *not* a container/host restart (the tmux server dies with the container). No terminal input/output, scrollback, history, or env is ever written to disk — this keeps the security surface unchanged. A future, opt-in tutorial will cover on-disk persistence for users who accept the trade-offs.
- Close vs. destroy mirrors chats: **Close** detaches (session keeps running), **Destroy** runs `tmux kill-session`. A dismissable per-terminal banner explains this lifecycle.

## Expected behavior

- Clicking "New terminal" creates a new named tmux session (`terminal-N`, next free N) anchored at the primary agent's `work_dir`, and opens it in a tab.
- Closing a terminal tab detaches only — the session keeps running. Reopening it (reload, dockview restore, or the "+" menu) reattaches to the same session with its shell, cwd, in-memory scrollback, and running processes intact.
- The group "+" menu lists every live non-`mngr-` session that is not already open in a tab (plus a "New terminal" entry), exactly as chats are listed. Selecting one reattaches in that group.
- Each terminal tab has a **Close** button (detach) and a **Destroy** button (kills the session, with a confirm dialog); Destroy also closes the tab.
- `mngr-`prefixed agent sessions are not listed as terminals. They remain reachable as they are today (the existing chat-panel "Open agent terminal" link, or raw `tmux attach`).
- The tab title tracks the live session: if the user switches sessions inside the pane (tmux `switch-client`) or renames the session (`tmux rename-session`), the title updates automatically.
- After a container restart, a restored terminal tab comes back as a fresh shell under the same name at the primary `work_dir`, with a one-line "session was reset (container restarted)" notice printed into its scrollback. Brand-new terminals never show that notice.
- A dismissable banner at the top of each terminal panel explains the in-memory lifecycle and links to a stub persistence doc on GitHub. It offers "dismiss" (this instance) and "never show again" (persisted server-side, so it stays hidden across browsers).
- Agent-driven terminal creation (`layout.py` / `service:terminal`) now also produces real, listable, reattachable named sessions; an agent may pass an optional session name, otherwise it gets the next `terminal-N`.
- With `history-limit` raised to 10000 and `window-size latest` set globally, terminals keep more scrollback and don't shrink to the smallest client when the same session is open in two tabs.

## Changes

**forever-claude-template — ttyd dispatch & tmux config**

- Add a new `session` dispatch key to `scripts/run_ttyd.sh` that attaches-or-creates a named tmux session (`tmux new-session -A -s <name> -c <work_dir>`) on the shared default socket, replacing the retired ephemeral `workdir.sh` path for ad-hoc terminals.
- Have the dispatch record the per-tab `terminal_id → $(tty)` mapping (the ttyd pty) before exec'ing the attach, so a tab's tmux client can be identified later. Clean the mapping up on client detach.
- Gate the "session was reset (container restarted)" notice on "session did not exist before this attach AND the request came from a restored tab", so brand-new terminals stay quiet.
- Extend the tmux config written from `.mngr/settings.toml` (`extra_provision_command`) to set `history-limit 10000` and `window-size latest`, and to install `client-session-changed` and `session-renamed` hooks that notify system_interface for live title tracking.

**forever-claude-template — system_interface backend**

- Add endpoints to: list live non-`mngr-` terminal sessions (names + `session_id`), allocate the next free `terminal-N` name (atomically, guarding against races), and destroy (`kill-session`) a session.
- Broadcast terminal title/session changes to the frontend over the existing `ws_broadcaster` channel, driven by the tmux hooks (session switched / renamed), keyed by `terminal_id`/`client_tty`.
- Persist the banner "never show again" flag in the workspace layout/prefs state alongside the existing `/api/layout` storage.
- Route agent-driven terminal refs (`service:terminal`, `terminal:` in `layout_ops.py`) to named sessions, honoring an optional agent-supplied name with a `terminal-N` fallback.

**forever-claude-template — system_interface frontend (dockview)**

- Change the "New terminal" flow (`DockviewWorkspace.ts` `buildTerminalUrl`/`openIframeTab`) to allocate a session name from the backend and build a `session`-key ttyd URL carrying the name and a per-tab `terminal_id`.
- Persist the session name and `session_id` in `panelParams`/`layout.json` so reload and restore reattach; on restore where the session is gone, recreate under the same name.
- Populate the group "+" menu with live non-`mngr-` sessions not already open in a tab, mirroring the chat-listing logic.
- Add per-tab **Close** (detach) and **Destroy** (confirm → `kill-session` → close tab) controls, mirroring the chat close/destroy affordances.
- Subscribe to the `ws_broadcaster` title/session updates and update the tab title on session switch/rename.
- Add the dismissable per-terminal banner ("dismiss" + "never show again") with copy explaining the in-memory lifecycle and a link to the stub doc.

**mngr monorepo — docs**

- Add a stub `apps/minds/docs/persistent-terminals.md` (public on GitHub) with a brief placeholder description of terminal persistence, targeted by the banner link.
- Add changelog entries: one under `apps/minds/` (mngr monorepo) for the stub doc, and the corresponding forever-claude-template changelog entry for the feature.
