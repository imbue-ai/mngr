# The minds embed contract

Version: 3 (tracks `CONTRACT_VERSION` in
`apps/minds/imbue/minds/desktop_client/static/embed_contract.js`)

The minds chrome (the "embedder") displays workspace content in a
cross-origin `<iframe>`, both in the Electron app and in plain-browser mode.
This document is the complete, auditable definition of the messaging boundary
between them. The runtime implementation is the `embed_contract.js` module,
which is the ONLY place either side may touch `postMessage` or register
`message` listeners -- ratchet tests in this repo and in
default-workspace-template enforce that confinement, so this file plus that
module are the entire surface to review.

The chrome page loads the module from `/_static/embed_contract.js`; the
workspace UI (system_interface) imports the same file from its vendored mngr
tree (`system/vendor/mngr/apps/minds/imbue/minds/desktop_client/static/embed_contract.js`),
so both sides always ship from one source of truth (skew is possible between
releases; see Compatibility).

## Security model

Three invariants carry the whole design:

1. **The fronting proxy owns embedding policy.** `mngr_forward` (locally;
   the share stack later) appends `Content-Security-Policy: frame-ancestors`
   to every proxied workspace response: `'self'` + the workspace's own
   origin family + the origins passed via `--embedder-origin`. Because the
   browser refuses to render a workspace frame for any other embedder,
   *being framed at all proves the embedder was allowed* -- the workspace
   needs no allowlist of its own.
2. **Structural source checks.** The workspace honours only messages with
   `event.source === window.parent`; the embedder honours only messages with
   `event.source === contentFrame.contentWindow`. Nested third-party frames
   inside workspace apps can hold window references but cannot forge either
   identity.
3. **Embedder-side origin check.** The embedder additionally verifies
   `event.origin` belongs to the workspace-origin family it navigated the
   iframe to (it knows the family: it set `iframe.src`).

Given (1), outbound messages use `targetOrigin: '*'` -- a disallowed
embedder can never load the frame, so there is no one else to leak to.

Payloads that carry ids are validated against conservative server-issued
shapes on receive (and re-validated by anything that builds a URL from them);
see the `*_PATTERN` constants in the module.

## Message inventory (v3)

### workspace -> embedder

| Type | Payload | Meaning |
|---|---|---|
| `minds:open-request-modal` | `{ requestId }` | Open the shell's permission-request modal focused on this request. |
| `minds:open-help` | `{ agentId? }` | Open the get-help / report-a-bug modal, optionally scoped to a workspace. |
| `minds:open-ai-keys-page` | `{ hostId? }` | Open the AI-key mint modal for this workspace. The embedder replies with `minds:open-ai-keys-ack`. |
| `minds:bring-app-to-front` | `{}` | OAuth finished in the external browser; raise the app window (Electron) / no-op (plain browser). |
| `minds:open-share-settings` | `{ serviceName }` | Open the shell's workspace-options panel on its Share tab, focused on that service. Fire-and-forget (no ack). |

### embedder -> workspace

| Type | Payload | Meaning |
|---|---|---|
| `minds:close-active-tab` | `{}` | The close-tab shortcut fired while this workspace was displayed; close the active dockview tab. |
| `minds:open-ai-keys-ack` | `{}` | A minds chrome is present and has opened (or will open) the mint modal. With no chrome (direct share visit) no ack arrives and the workspace shows its fallback text. |
| `minds:permission-resolutions` | `{ resolutions }` | Permission-request verdicts, `{ requestId, resolution }` each. Sent as the workspace's recent-verdicts snapshot when its frame (re)loads, and with one entry the moment the user resolves a request. |

The ack's semantic is "a minds chrome is present" -- NOT "the desktop app is
present". Plain-browser chrome acks too.

`permission-resolutions` is a display channel, not a decision channel: it
flips the workspace's in-chat cards to their verdicts without waiting for the
agent transcript's own resolution message to make the round trip, and the
transcript's classified resolution takes over once it lands. The single-entry
send goes exclusively to the frame showing the workspace that ASKED (the
chrome knows, from the request's agent id); the load-time snapshot is what
makes missing it harmless: the workspace's verdict cache is page-scoped, so
whenever the chrome (re)loads a frame it pushes that workspace's newest
verdicts from the desktop client's response event log
(`/ui/api/inbox/resolutions`) -- scoped to the mounted workspace's own
requests, so verdicts never read across workspace boundaries. The workspace
never has to ask, time, or retry anything.

## Compatibility policy (tolerant)

- No version field travels on the wire.
- Receivers ignore unknown message types silently.
- Existing types are immutable: never change the meaning, payload shape, or
  direction of a shipped type. Evolve by adding new types.
- Receivers ignore unknown payload fields on known types.

This lets a newer chrome face an older workspace (and vice versa)
indefinitely: the intersection of types both sides know keeps working, and
everything else degrades to a no-op.

## Debug logging

Set `localStorage['minds-debug-embed'] = '1'` (or `window.MINDS_DEBUG_EMBED = true`,
or `MINDS_DEBUG_EMBED=1` for the Electron app) and both endpoints log every
sent / received / ignored / rejected message -- type and origin only, never
payloads -- to the console.

## Out-of-band signals (NOT part of this contract)

- The Electron main process re-validates ids with its own copies of the
  shape patterns before building URLs (never trust the renderer). Those
  constants live in `electron/main.js` and mirror this module's.
- Workspace health/readiness/URL state flows through the minds backend's
  `/ui/ws` WebSocket channel, not through postMessage: the shell derives
  titlebar state from its own route plus the channel, identically in Electron
  and browser mode.

## Version history

- **1** -- initial reification of the pre-existing relay message set
  (`open-request-modal`, `open-help`, `open-ai-keys-page`+ack,
  `bring-app-to-front`, `close-active-tab`). `reload-crashed-view` (an
  Electron-internal crash-page affordance) was deliberately dropped: the
  desktop app handles a crashed window natively, and neither mode observes a
  crashed workspace iframe (Electron exposes no OOPIF process-gone signal).
- **2** -- added `permission-request-resolved` (embedder -> workspace),
  restoring the instant in-chat card flip that the deleted Electron content
  relay used to carry. Same postMessage path in Electron and plain browser;
  no Electron IPC is involved.
- **3** -- added `permission-resolutions` (embedder -> workspace): the
  workspace's recent-verdicts snapshot on every frame (re)load, so a rebuilt
  page never offers Approve/Deny for an already-decided request, plus the
  live one-entry flip on resolve; see `specs/permission_state.md` in this
  repo for the failure analysis. It supersedes v2's
  `permission-request-resolved`: v3 chromes no longer send that type and v3
  workspaces no longer handle it (mixed versions degrade to the
  transcript-driven flip), and the string `minds:permission-request-resolved`
  is retired -- never reuse it with a different meaning. Also added
  `open-share-settings` (workspace -> embedder), replacing the workspace's
  instructional share popup with a deep link to the shell's Share tab. A
  well-shaped name for a service the shell does not recognize falls back to
  the whole-machine share (`ShareModel.selectTarget`'s existing behavior for
  an unknown target).
