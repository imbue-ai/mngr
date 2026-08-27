# Latchkey permissions

Minds-managed agents access third-party services (Slack, GitHub, Google Drive,
...) through [Latchkey](https://github.com/imbue-ai/latchkey). This page
describes how the desktop client surfaces permission decisions to the user
and how the agent receives the answer.

Every workspace sees exactly one gateway at the same loopback URL. Local
workspaces use the desktop-resident gateway. Remote workspaces use the
VPS-resident gateway for third-party calls; its bundled forwarding extension
proxies `/permissions`, `/permission-requests`, and `/minds-api-proxy` requests
back to the desktop gateway over an SSH tunnel. This keeps the permission queue,
permissions files, and Minds API on the user's computer without requiring a
second gateway URL or a different agent skill.

## End-to-end flow

1. **Agent makes a call.** The agent issues an HTTP request to the
   workspace's minds-managed `latchkey gateway` (or to `latchkey curl`
   directly). The agent's environment carries the gateway URL, a shared
   password (sent in `X-Latchkey-Gateway-Password`) and a permissions
   override JWT (sent in `X-Latchkey-Gateway-Permissions-Override`) that
   points the gateway at the agent's own permissions file.
2. **Gateway responds with success, no-credentials, or not-permitted.**
   * 200: success, nothing to do.
   * 400 with `Error: No credentials found for <service>` (or `... are expired`):
     the user has not yet authenticated to the service.
   * 403 with `Error: Request not permitted by the user.`: the user has
     authenticated but has not allowed this kind of request.
3. **Agent files a request with the gateway.** On any of the blocked
   outcomes, the agent POSTs to the gateway's `/permission-requests`
   extension with the request type, its type-specific payload, and a
   one-paragraph rationale, then ends its turn and goes idle. The gateway
   persists the request in its own queue.
4. **The user opens the request.** The gateway's own persisted queue is
   the pending set: the desktop client reads it on demand (minus requests
   with a recorded verdict) and follows the gateway's stream only as a
   change signal. A pending request never opens anything by itself: it
   waits behind the chat card's "Review & respond" relay and the "Waiting
   on you" rows in a machine's Permissions tab.
   Either one opens the permission popup as a **centered dialog** over the
   current window, on a dim backdrop. The popup is the only review
   surface; the other ways in all lead back to it: the titlebar bell's
   notification feed (whose pending rows carry the request), its toast
   cards, and OS-notification clicks each jump to the asking workspace and
   open the same popup there. It is not a route -- it
   stacks over whatever is on screen, including the workspace-options panel
   -- so dismissing it via Approve/Deny, the close button, a backdrop
   click, or Escape returns the user to their work with nothing changed
   underneath.

   The popup shows one request at a time, headed by a "Permission request
   for [dot] <workspace>" eyebrow. Resolving it advances to the next
   pending request, and the popup dismisses itself when none remain; a
   request resolved on another surface advances the popup the same way.

   The page renders a single-scope permission dialog:
   * The dialog header is the service's brand mark in a rounded square
     followed by the service name plainly (no monospace pill). The asking
     machine is named once, in the popup's own "Permission request for
     <workspace>" eyebrow, so the dialog does not repeat it. Below the
     header sit the **Account** section (whenever anything is signed in,
     see below) and then the agent's rationale under a **Reason** heading.
   * By default the dialog shows a **simple, informative view**: a single
     summary heading ("Approving will let the agent") above a read-only
     list of what will be granted on Approve -- each permission in plain
     English, never its schema name -- plus only the Approve / Deny
     buttons. This keeps the common case approachable for non-technical
     users.
   * A small **"Adjust"** link, below the permission list and indented to
     its text column, reveals the full **editor view**, which exposes a
     switch per [Detent](https://github.com/imbue-ai/detent)
     permission schema available for that scope, grouped by the area it
     acts on. The available schemas are read from the bundled
     `services.json` catalog (shipped with mngr_latchkey) and cached in
     process for the lifetime of the desktop client. The selection lives in
     the dialog rather than in the markup, so the two views are the same
     set seen two ways and the simple view's Approve submits whatever the
     editor was left holding. A **"Back to agent's picks"** link above the
     editor returns to the summary and restores exactly the set the
     request arrived with, discarding whatever was changed in the editor.
   * The detent ``any`` schema (matches every request inside the scope)
     is the sole member of a trailing **Extras** group, behind a divider
     at the end of the editor, so the user can opt into unrestricted
     access without it reading as one more area of the service. It is
     **not** selected by default, and so never appears in the simple
     view's read-only list. Turning it on is exclusive: the specific
     switches gray out, and only the catch-all is submitted.
   * The dialog preselects (and the simple view lists) the union of (a)
     permissions already granted for that scope on the agent's host and
     (b) the permissions the agent declared in the request event.
     Approving without changes grants exactly that union; opening the
     editor and turning more on broadens it, turning them off narrows or
     revokes. The editor therefore doubles as a revocation UI.
   * The Approve button stays disabled while nothing is selected, so if
     the agent submitted an empty ``permissions`` tuple and the user has
     no prior grants for the scope, the simple view shows a prompt to use
     "Adjust" and the user must actively pick something there before
     approving.
6. **User approves.** The desktop client:
   1. Runs `latchkey services info <service>` to read `credentialStatus`,
      `authOptions`, and `setCredentialsExample` (the same call that
      rendered the dialog's account picker and, for a service with no
      browser sign-in, its credential form).
   2. If credentials are not `valid` and the service advertises a
      `browser` auth option (or latchkey reports no `authOptions` at all,
      treated as the legacy fallback), runs `latchkey auth browser <service>`
      synchronously (transparently running the one-off `latchkey auth
      browser-prepare <service>` step first when latchkey asks for it).
      Cancellation or failure of either step produces a `FAILED` outcome:
      the grant is **not** applied and the request stays pending (no
      response event is written), so the dialog surfaces the reason and the
      user can click Approve again to retry. A failed approval is never
      recorded as a denial.
   3. If credentials are not `valid` and the service does not advertise a
      `browser` auth option (e.g. AWS or Coolify, where `authOptions =
      ["set"]`), the grant is **refused for now** and the request stays
      pending while the dialog collects the credentials (see
      [Manual credential entry](#manual-credential-entry) below). Minds
      never asks the user to open a terminal.
   4. Atomically rewrites the agent's `latchkey_permissions.json` so the gateway
      enforces the chosen schemas on the next request.
   5. On success, appends a `GRANTED` response event to
      `~/.minds/events/requests/events.jsonl`. (A `FAILED` approval writes
      no response event and leaves the request pending; see step 6.2.)
   6. On a `GRANTED` outcome, sends the agent a plain-English `mngr message`
      describing the decision (with the request's id embedded, so the chat
      harness can pair the notice with the right card); the agent wakes up
      and decides whether to retry. Delivery is retried with backoff for as
      long as the app runs, so a nudge for a stopped workspace lands when
      that workspace next comes up; the in-chat card does not depend on it
      (see step 8). A `FAILED` or manual-credentials outcome leaves the
      request pending and notifies only the user (in the dialog), not the
      agent.
7. **User denies.** The desktop client appends a `DENIED` response event
   and sends the agent a plain-English denial message (same id embedding
   and retrying as the grant nudge). `latchkey_permissions.json` is not
   touched.
8. **The asking workspace hears the verdict at once.** Either resolution
   also sends the workspace a one-entry `minds:permission-resolutions`
   embed contract message (see `docs/embed-contract.md`), so its in-chat
   card flips to Approved/Denied without waiting for the agent's own
   resolution message to travel back through the transcript. It goes only
   to the workspace that asked, and only when that workspace is the one on
   screen -- the chrome mounts one workspace frame at a time, so no other
   workspace has a live page to update. A workspace page built AFTER the
   verdict (a reload, or returning to a workspace resolved while another
   was displayed) is covered by the snapshot: whenever the chrome (re)loads
   a frame it pushes that workspace's recent verdicts, read from the
   response event log via `/ui/api/inbox/resolutions`. Once the
   transcript's own classified resolution lands, it takes over.

## Manual credential entry

A service latchkey cannot sign in to through a browser advertises an
example of the command that stores its credentials, with each value the
user has to supply written as an angle-bracketed placeholder -- e.g.
`latchkey auth set-nocurl aws <access-key-id> <secret-access-key>`. Minds
parses that example (`imbue.mngr_latchkey.credential_commands`) and turns
it into **one labeled input per placeholder**, which the detail payload
carries (`manual_credentials`) so the dialog renders the form at the top
immediately -- no first Approve needed to discover that credentials are
required. Approve then substitutes the typed values, runs the command
itself with `--account <selected account>` (a global latchkey option, so
it precedes the subcommand) and Minds' own `LATCHKEY_DIRECTORY`, re-reads
`latchkey services info`, and continues the grant. One click, no terminal,
and the credentials land in the store the desktop client actually reads.

Details worth knowing:

* The command itself is **never shown**: it is an implementation detail,
  and the user only ever sees the values it needs. It is built as an argv
  list (no shell), so a pasted value is never re-interpreted, and the
  filled-in argv is never logged.

* The form belongs to the *selected account*: each account choice carries
  `is_credential_setup_needed`, so switching from a not-yet-connected
  account to a working one hides the form (and re-enables Approve) without
  a round trip. Approve stays disabled while any input is empty.

* The account the credentials are stored under is the one the dialog's
  **Account** dropdown selects. The dialog has two shapes, not three:
  with nothing signed in there is no dropdown at all and Approve reads
  "Sign in & approve", so the browser hop is not a surprise; with anything
  signed in -- one account or several -- the dropdown appears, the account
  the grant will ride on already selected and "+ Add account" last. A lone
  account is not a third arrangement: a service can hold several accounts,
  so a review that named none would leave which one unsaid. Choosing
  "+ Add account" flips Approve back to "Sign in & approve" and leaves the
  dropdown in place, so the choice can be taken back; nothing is signed in
  until Approve. A service with no accounts yet resolves to latchkey's
  unnamed default account; "+ Add account" on a service that already has
  one also asks for an **account name** (`is_account_name_needed`), since
  a manual connection cannot discover it the way a browser sign-in does.

* Two different things can go wrong, and they read differently:

  * The value is **malformed** -- the service's own shape check rejects it
    (`latchkey auth set-nocurl` exits non-zero, e.g. "doesn't look like an
    AWS access key ID"). Its explanation is surfaced verbatim, minus the
    usage lines latchkey appends: those either restate a terminal command
    Minds never shows or, for AWS, print the bare `<access-key-id>`
    placeholder as the "example", which is worse than nothing next to an
    input already labelled that way. `describe_credential_command_failure`
    does that trimming (and caps a crash dump).

  * The value is **well-formed but wrong** -- mistyped within the accepted
    shape, or revoked, rotated or expired. `auth set` only validates the
    shape, so these store fine and only fail when latchkey actually calls
    the service. Minds therefore re-reads the *online* `services info`
    after storing and refuses to grant unless the account's credentials
    come back usable. Note that a service Minds cannot reach reads the same
    way (latchkey reports any failed check as `invalid`), so the message
    names that possibility too. The credentials stay stored either way, so
    a later Approve re-checks them.

  In both cases the form comes back with the typed values intact and the
  reason in place of its instruction -- as the design system's `error`
  `Notice` (red, `role="alert"`), and scrolled into view with the smallest
  movement that shows it, since the form sits above a permission list that
  can leave it off-screen from where the Approve button is. The scroll is
  handed out once per failed approval, so later redraws never fight the
  user's own scrolling. An outright `FAILED` approval (a browser sign-in
  that did not complete) gets the same treatment on its own notice.

* If the example has no `<placeholder>` at all (or is not a latchkey
  invocation), there is nothing to ask for: the dialog shows that as an
  error and offers **no Approve button**, leaving Deny as the only action.

## Per-agent isolation

Minds runs a single shared `latchkey gateway` subprocess for every
agent rather than one per agent. The gateway is locked down with two
latchkey 2.8.0 features:

* **Password protection.** The gateway is started with
  `LATCHKEY_GATEWAY_LISTEN_PASSWORD` set, so it rejects every request
  that does not present the same value in the
  `X-Latchkey-Gateway-Password` header. The password is derived
  deterministically from the desktop client's Latchkey encryption key:
  minds calls `latchkey gateway create-jwt --no-validate` against a
  hard-coded sentinel path and SHA-256-hashes the resulting JWT. That
  way the password is stable across desktop-client restarts without
  minds having to persist it in plaintext anywhere.
* **Per-agent permissions.** When an agent is created, minds allocates an
  opaque `~/.minds/latchkey/permissions/<uuid>.json` handle and materializes it
  with the deny-by-default baseline. For a desktop-gateway workspace, minds
  mints a permissions-override JWT pointing at that handle and injects it as
  `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE` at `mngr create` time. For a
  VPS-gateway workspace, the environment omits that override: native requests
  use the VPS gateway's synchronized default `~/.latchkey/permissions.json`.
  Its desktop-forwarding extension holds a separate desktop-target JWT and
  replaces the override header only on requests it forwards.

  After `mngr create` returns the canonical agent id, minds replaces
  the opaque file with a symlink pointing at
  `~/.minds/agents/<agent_id>/latchkey_permissions.json`. The agent-id
  path is the canonical location -- the desktop client's permission-grant
  flow writes to it as before -- and the gateway reads through the
  symlink to see those grants. This indirection lets minds mint and
  inject the JWT before the agent id is known, eliminating a
  previously-fragile post-create injection step.

## Minds API access through the gateway

Minds itself exposes a small REST API on the desktop-client bare
origin (`/api/v1/...`: agent notifications, workspace view refresh,
and the WebDAV file-sharing mount). Agents reach it through the same latchkey
gateway they use for every other outbound HTTP call, via the bundled
`minds-api-proxy` extension at `/minds-api-proxy/api/v1/...`. There is
no per-agent reverse SSH tunnel for the Minds API anymore.

Authentication uses one central `MINDS_API_KEY` per `minds run`,
freshly generated in memory at startup and never handed to agents.
The `minds-api-proxy` extension reads it from the
`LATCHKEY_EXTENSION_MINDS_API_KEY` env var (published to the supervisor
by `minds run`, which restarts the supervisor on every startup so the
current key always wins) and injects `Authorization: Bearer <key>` on
every forwarded request, overwriting any header the agent supplied.
The desktop client matches the same value on the inbound side. The
key rotates per minds startup; nothing else in the monorepo reads it
from disk, so there is no on-disk copy to keep in sync.

Three routes are *not* agent-scoped and are granted to every agent by the
baseline, because they are identical for all callers and carry no
per-workspace data: `GET /minds-api-proxy/api/schema` (the OpenAPI
description of the reachable surface), `GET
/minds-api-proxy/api/v1/timezone` (the IANA timezone of the machine the
desktop client runs on), and `GET /minds-api-proxy/api/v1/app/version`
(the newest workspace-template ref the app supports, which for a released
binary is also its own release tag). That last one is what a workspace's
`update-self` caps itself against, so it does not pull a template newer
than the app driving it. Note that this is self-imposed by the workspace,
not enforced here: nothing stops an agent that skips `resolve-target`, or
a user running `git merge` by hand. The threat model is a workspace
breaking itself by accident, not a hostile one. It is baseline-granted
rather than must-ask because update-self resolves its target from a
background worker, where a permission dialog has nobody to answer it.
Each of the three is pinned by `const` to its exact method and path, so
none of them widens to the rest of `/api/v1` -- note in particular that
the app grant pins `/app/version` and not `/app`, so it cannot widen to
whatever app state a later route hangs off that prefix.
Existing hosts pick a newly-added baseline grant up through
`reconcile_baseline_permissions`, which `register_agent_for_host` applies
whenever it registers a discovered agent -- a baseline addition alone
would otherwise only reach newly-created workspaces. Reconciliation only
*adds permissions to the `latchkey-self` rule*; it never introduces a
rule, so a baseline addition shaped as a new rule reaches newly-created
host files only. Auto-registration de-dupes an agent it has already
registered for the life of the process, so a permission addition lands on
the first discovery after the app restarts, not mid-run. An agent still
waiting on its host file is the exception: it is retried on every
resolver change, so its registration (and the reconciliation that rides
along) does land mid-run.

Per-agent isolation comes from the latchkey gateway's permissions
file. The agent baseline grants the whole per-agent path prefix --
any method on `/minds-api-proxy/api/v1/agents/<agent_id>/...` -- but
only when `<agent_id>` is one the host has registered. Two baseline
rules produce that, and rule order matters because detent stops at the
first matching scope: `minds-api-proxy-per-agent-unauthorized` matches
the prefix for any *unregistered* agent id and resolves to an empty
permission list (an immediate reject), and the `latchkey-self` rule
behind it carries `minds-api-proxy-per-agent`, which allows the path.
Registration adds an agent id to the unauthorized *scope schema's*
`not.anyOf` list, lifting it out of the reject shortcut. It is driven off the
*discovery* stream, not agent creation, so it covers agents the
workspace creates for itself (chat, worktree, worker) as well as the
primary one minds creates. Because discovery can see a brand-new agent
before creation has linked the host's permissions file into place, a
registration with no file to write to is retried on later resolver
changes rather than dropped. A new `/api/v1/agents/<id>/*` route
therefore needs no permissions work.

The bug-report route (`POST .../agents/<...>/report`) is the one
exception: it sits in a rule *ahead* of the unauthorized gate, so it
works for a caller with no per-agent registration at all -- on host files
new enough to carry that rule. Reconciliation adds no rule to older ones,
where an unregistered caller's report still hits the gate.

Because the file is keyed per host, an agent on host A cannot reach the
API on behalf of an agent on host B: host A's permissions file does not
list B's agent id at all. Within a host the allowlist is shared, which
is what lets a sub-agent address its workspace's primary agent id (as
the refresh call does).

The gateway's *default* permissions config
(`~/.minds/latchkey_default_permissions.json`) is materialized with
empty `rules` too, so any request that somehow bypasses the JWT
mechanism still sees a deny-all gateway -- the implicit `allow all`
that latchkey applies when the file is missing must never be observable
by an agent.

`LATCHKEY_DIRECTORY` -- where credentials live -- stays shared across all
agents on the same machine.

## Cross-workspace management API permissions

Minds exposes a cross-workspace management API (`/api/v1/workspaces/...`)
that lets an agent in one workspace act on *other* workspaces -- listing,
reading detail/version/backups, creating, destroying, starting/stopping,
exporting and managing backups, establishing SSH access, updating settings,
and recovering (health check / restart). It is
reached through the same `minds-api-proxy` extension and gated by a single
`minds-workspaces` detent scope with one named permission per verb
(`minds-workspaces-read`, `-create`, `-destroy`, `-lifecycle`,
`-backups-export`, `-backups-manage`, `-ssh`, `-update`,
`-recover`). Nothing is
pre-granted, so an agent's first cross-workspace call gets a 403 until the
user approves; the scope and verb schemas are not part of the agent baseline
at all -- they arrive, fully self-described, with the grant (see below).

This surface has its own permission-request type, distinct from the
`predefined` (service-catalog) and `file-sharing` types: an agent POSTs
`type=workspace` to the gateway's `permission-requests` extension with the
verbs it wants and -- for the verbs that act on a specific workspace -- the
`target_workspace_id` it wants to act on. The desktop client surfaces a
dialog with a checkbox per verb plus, when the request names a target
workspace, an all-vs-selected choice.

The verbs split on a **target axis**:

* `read` and `create` are all-or-nothing: a grant applies to every
  workspace (listing does not leak per-target data, and create takes no
  target).
* `destroy`, `lifecycle`, `backups-export`, `backups-manage`, `ssh`,
  `update`, and `recover` are *target-scoped*.
  A "selected" grant for one of these verbs mints a **uniquely-named
  per-target permission schema** (`minds-workspaces-<verb>-<target_id>`)
  whose path pins that single workspace; an "all workspaces" grant uses
  the broad schema keyed by the plain verb name (with a `[^/]+` id
  wildcard). Because each selected target is a distinct schema name,
  successive grants *accumulate* targets through the gateway's ordinary
  schema-by-name merge -- the same mechanism file-sharing uses for
  per-path schemas -- with no `anyOf` and no special merge logic.

The grant is applied exactly like file-sharing: the agent's request carries
a precomputed `effect` (a self-contained patch of the scope schema + the
verb schemas + the grant rule, computed in `permission_requests.mjs`'s
`computeWorkspaceEffect`), and the desktop client approves it via
`POST /permission-requests/approve/<id>`, which splices the effect into the
requesting agent's per-host `latchkey_permissions.json` (reached through its
opaque handle) and drops the pending record. The approve call sends an
override body (`{permissions, target_workspace_id}`) so the gateway
recomputes the effect from the user's dialog choices (the verb subset they
ticked and the all-vs-selected target). The scope schema is emitted on every
effect and merged by name, so a host file that has never seen the scope gets
it with the first grant -- no baseline entry or startup migration required.
The Python `mngr_latchkey.workspace_permissions` module holds only the
dialog-facing verb metadata; the schema construction lives in the gateway
extension.

## Service catalog

The catalog of latchkey services (display name + scope schema + the
permission schemas the dialog offers) lives alongside the latchkey
gateway extension at
[`libs/mngr_latchkey/imbue/mngr_latchkey/extensions/services.json`](../../../libs/mngr_latchkey/imbue/mngr_latchkey/extensions/services.json)
and is read directly at desktop-client runtime by
`imbue.mngr_latchkey.services_catalog.ServicesCatalog`. Each service maps
to a *list* of scope entries (a single service may expose more than one
detent scope).
Each entry has the shape:

* `scope` -- the detent scope schema the service owns; used as the rule
  key in `latchkey_permissions.json` and as the value the agent puts
  in its permission request's `scope` field.
* `display_name` -- human-readable label for the *scope*, shown in the
  dialog header (`GitHub (REST API)`).
* `service_display_name` -- label for the whole service (`GitHub`), for
  surfaces that name a connection rather than one of its scopes. Present
  only where it differs from `display_name`.
* `permissions` -- granular detent permission schemas the dialog offers
  as switches. The catch-all ``any`` schema is added client-side as an
  available option (the gateway file does not list it) at the head of
  this list, though the dialog offers it last, in its trailing Extras
  group; it is never preselected, but the user can opt into it
  explicitly.

The minds desktop client caches the response in-process on first access
so each request renders without re-fetching. To add a new builtin
service, edit `services.json` in the gateway extension package (see its
README); those schemas must already exist in detent.

## Additional (custom) services

Beyond detent's builtin catalog, minds ships a small hardcoded list of
*additional* services in
[`libs/mngr_latchkey/imbue/mngr_latchkey/additional_services.json`](../../../libs/mngr_latchkey/imbue/mngr_latchkey/additional_services.json).
Their catalog entries are folded into `services.json` by that package's
generator, so the dialog and the gateway extensions treat them exactly like a
builtin service. These are third-party services minds supports itself, using
three latchkey features:

* **Registration.** minds writes each additional service into the
  `registeredServices` block of latchkey's own `config.json` -- the same
  place `latchkey services register` would -- so latchkey can match the
  service's domain and inject the user's stored credentials for it. Every
  gateway that serves minds agents gets this: the desktop one (at
  `Latchkey.initialize()` and again at each gateway spawn) and each
  VPS-resident one (during remote provisioning, before its gateway starts).
  A VPS needs it as much as the desktop does -- the credentials
  synchronized to it are unusable by a gateway that cannot resolve a
  request to the service in the first place. Writing the config rather than
  shelling out to the CLI also means a definition that changes in a later
  release reaches installs that already carry the old one; `services
  register` refuses a name that already exists.
* **Browser sign-in (optional).** A service may name a login URL and one of
  latchkey's generic login flows, which is what `latchkey auth browser`
  then runs for it. `claude.ai` uses the `cookie-capture` flow: latchkey
  opens the claude.ai login page and stores the `sessionKey` session cookie
  as the credentials.
* **Self-shipped detent schemas, referenced via `include`.** A custom scope
  is not one of detent's builtin schemas, so each additional service ships
  its own scope schema (matching the service domain) plus a permission
  schema. Rather than inlining those schemas into every host's
  `latchkey_permissions.json`, minds materializes them **once** into a
  shared `minds_shared_schemas.json` file and has every per-host file
  reference it through detent's [`include`](https://github.com/imbue-ai/detent)
  directive. Granting an additional-service scope is then a plain rule
  write (no schema injection); detent resolves the scope's schema from the
  shared include. The include is a bare relative name, which detent resolves
  relative to the referencing file's directory -- so the same host file
  works both on the desktop (where the shared file lives in the gateway's
  opaque-handle directory) and on a VPS (where it is shipped next to the
  host's `~/.latchkey/permissions.json`).

Additional services are merged into the same catalog the dialog reads, so
they appear and are granted exactly like builtin ones. The seed entry is
`claude.ai`, which exposes a single `everything` permission (full access
to the `claude.ai` domain) and signs in through the browser like any other
service. An additional service that ships without a browser sign-in is
authenticated by hand instead, with `latchkey auth set <name> -H "..."`;
either way, granting the permission and supplying credentials are
independent steps.

## Connectors and accounts (Settings page)

The app-level Settings page's **Connectors** tab lists, per connected
service, the accounts the user has signed in to (latchkey 3.0.0 stores
credentials per account). The account list is read from
`latchkey services info <service> --offline` -- the `credentials` object,
keyed by account name (the unnamed default account keyed by `""`) --
which also drives the aggregate credential status the grant flow uses.

Two per-service actions manage accounts:

* **+ Add account** runs the same browser sign-in as approving a
  permission request whose service has no credentials yet
  (`Latchkey.add_account`), but with `LATCHKEY_EPHEMERAL_BROWSER=1` set so
  the browser starts from a clean session and the user lands on a fresh
  sign-in screen -- letting them add a genuinely new account instead of
  being silently re-authenticated as an already-signed-in one. For a Minds
  Google OAuth service, if signing in with the official Minds client does
  not succeed, it always falls back to a fresh `auth browser-prepare`
  self-setup step and retries.

  Because the action *is* that sign-in, it is **disabled** for a service
  that has no browser flow (`is_browser_sign_in_supported`, resolved per
  listed service with one `latchkey services info <service> --offline`
  call -- all of them probed on a thread each, so the page's wall time
  does not grow with the number of connectors), with the reason on hover,
  instead of failing with an error after the click. Such a service is connected from a permission dialog, which
  asks for its credentials directly (see
  [Manual credential entry](#manual-credential-entry)).
* **Disconnect** clears one account's stored credentials
  (`latchkey auth clear <service> --account <account>`). Disconnecting the
  *last* account for a service also runs the per-service "revoke all"
  cleanup in the background -- stripping that service's grants from every
  workspace host file, since they would otherwise have no credentials
  behind them.

Below the accounts, the panel shows the existing per-workspace grants
("Allowed on all accounts:"), which are unchanged.

This page is app-wide. To edit what one machine's agents may reach, see
[Permissions tab (per machine)](#permissions-tab-per-machine) below.

## Permissions tab (per machine)

The workspace options panel's **Permissions** tab is the other half of the
story above: Settings owns accounts across the app, this owns what agents
in *one* machine may reach. It renders every permission that machine's
host file can carry as a toggle, so the file is editable without going
back through a request.

The left nav has one entry per **connection** -- a (service, account) pair
that either has stored credentials or still appears in the host's rules,
so a grant left behind by a disconnected account is never invisible --
plus **Add connection**, **Local files** (the `minds-file-server-*`
shared-path grants) and **Other machines** (the `minds-workspaces-*`
verbs). A "Waiting on you" strip leads the pane when this machine's agents
have pending requests; each row opens the review popup on that request.

Three properties are worth knowing:

* **A flip posts one permission, and the server answers with the whole
  view.** The client sends only the permission it flipped and its new
  state; the desktop client reads the host's permissions file, recomputes
  the affected rule's *complete* permission set, and writes that through
  the gateway's `permissions` extension -- never a diff. Recomputing
  server-side is what keeps a buggy or hostile client from clobbering the
  baseline permissions that share the `latchkey-self` rule with the
  toggleable ones. The response is the refreshed view, adopted verbatim,
  so the pane shows what was stored rather than what it hoped for; a
  refused write leaves the screen exactly as it was, with the reason
  beside the row. Turning a connector rule's last permission off deletes
  the rule rather than leaving an empty one behind.

* **Add connection connects a service two ways**, because latchkey does:
  the browser sign-in for the services that have one, and -- for AWS,
  Coolify and the like -- a form with one input per value the service's
  own `setCredentialsExample` asks for, stored by running that command
  (see [Manual credential entry](#manual-credential-entry)). Which of the
  two travels with each offered service, so the pane never offers a
  sign-in that cannot happen. Either way the new account arrives with
  nothing granted.

* **Revoke all and Sign out are different in scope, deliberately.**
  *Revoke all*, in a connection's heading, drops that account's grants
  **on this machine only**; the account stays signed in and its grants on
  other machines are untouched. *Sign out*, at the foot of the panel,
  clears the stored credential itself -- so the account is gone from
  **every** machine, and its grants, which would otherwise have nothing
  behind them, are stripped from every active workspace's host file. It
  asks first, naming the service and account. An account with leftover
  grants but no stored credential offers no Sign out -- there is nothing
  left to clear -- and its toggles can be turned off but not on, since
  turning one on would grant something with no credentials behind it.

Re-enabling a `latchkey-self` toggle needs the permission's schema
definition to still be in the host file -- detent fails the whole check on
a reference it cannot resolve. Revoking leaves the definition there
precisely so the row can be turned back on. A grant whose definition has
gone can still be turned off, but not back on: only the agent asking again
brings it back.

The pane loads independently of the panel's other tabs. A latchkey gateway
that cannot be reached shows as "permissions can't be loaded" rather than
an empty, misleading "nothing granted", and does not take Share machine or
Machine settings down with it.

## Agent-side responsibilities

Agents are expected to:

* Detect the three blocked outcomes from the gateway response.
* POST a permission request to the gateway's `permission-requests`
  extension (`POST /permission-requests` with `scope`, `permissions`,
  and `rationale`).
* Stop the turn and wait. The agent will receive an `mngr message` from
  the desktop with the decision and can decide whether to retry.

The detection-and-wait logic for Claude Code lives in the
`default-workspace-template` repository's latchkey skill, not in this
monorepo.
