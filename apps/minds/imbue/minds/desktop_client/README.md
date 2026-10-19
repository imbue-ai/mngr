# Architecture

The local desktop client is a Flask app that handles authentication and traffic forwarding. It is the gateway through which users access all their workspaces.

> **UI is a Mithril single-page app.** The user-facing UI is a client-rendered
> Mithril SPA under [`../../../frontend/`](../../../frontend) (its own
> Vite/TypeScript/Tailwind package, built into `static/ui/` at wheel-build
> time). Flask serves the SPA's `index.html` for every hub route and exposes a
> session-authed JSON surface at `/ui/api/*` plus one WebSocket per window at
> `/ui/ws`. The WebSocket carries all live state (workspaces, accounts,
> providers, requests, the notification feed, per-workspace health, discovery
> health, and one-shot events) from a single edge-driven publisher
> (`ui_publisher.py`) through a
> per-client-queue broadcaster (`ui_channel.py`); the handshake is driven
> directly with `simple_websocket` (no flask-sock, so the session check runs
> before the socket is hijacked) on top of the cheroot gateway adapter in
> `ws_gateway.py`. First paint is seeded from a bootstrap document inlined
> into the served page.
>
> All UI work belongs in the `frontend/` package. The only server-rendered
> documents left are the dependency-free static pages that must work before
> (or without) the SPA bundle: the one-time-code login flow (`ui_login.py`)
> and the friendly error pages (`static_pages.py`). `static/` holds only the
> embed contract module, the vendored Sentry browser bundle + its init, the
> service icons, and the built SPA bundle (`static/ui/`, gitignored).

Each workspace already runs its own `system_interface`, which serves the desktop at the workspace's bare origin; every other registered service owns its own origin (`<service>.agent-<hex>.localhost:PORT/`), so nothing proxies or rewrites service traffic. The desktop client's job is to route browser traffic for `[<service>.]agent-<hex>.localhost:PORT/*` to the right in-workspace backend -- it does not rewrite paths or inject anything itself.

This desktop client is a separate component from any individual workspace's web server -- the desktop client does not define what workspaces do or how they respond to messages. It only handles routing and authentication so that the URLs being served by the workspace are accessible locally.

## Authentication

Authentication is global (one session grants access to all agents). The desktop client uses `itsdangerous` for cookie signing. Three credentials are accepted, by three kinds of caller: the signed session cookie (the browser UI), the gateway-injected bearer key on `/api/v1` (in-workspace agents, through the latchkey `minds-api-proxy`), and arrival over the same-machine Unix domain socket (agents running as the user; the listener in `api_socket.py` verifies the peer's uid before any request is read and marks the WSGI environ, which `api_transport.py` reads and every auth check accepts). The cookie flow works as follows:

Note this is the *local* session with the desktop client itself. *Imbue account* sign-up/sign-in happens on the connector's hosted accounts pages in the system browser: the SPA drives `POST /auth/api/web-login/start` (which runs `mngr imbue_cloud auth login` -- browser + loopback + PKCE code exchange) and polls `GET /auth/api/web-login/status/<flow_id>` to render the waiting/copy-link modal. There are no in-app account sign-in pages anymore; the retired `/auth/login` and `/auth/signup` URLs 302 into the SPA with `?web-login=1`, which starts the browser flow on load.

- **Signing key**: generated once on first server start, stored at `{data_directory}/signing_key`. Used to sign all auth cookies.
- **One-time codes**: a login code is generated and printed to the terminal when the server starts. Codes are stored in `{data_directory}/one_time_codes.json` and can only be used once.
- **Session cookie**: after successful authentication, the server sets a signed `minds_session` cookie. The cookie is host-only (no `Domain` attribute): browsers treat `localhost` as a public suffix and refuse to send `Domain=localhost` cookies to subdomains. Workspace subdomains instead get their own session via the forward server's `/goto/<agent-id>/` auth bridge, so a single bare-origin sign-in still covers every workspace.

## Local desktop client routes

`/login` route (takes one_time_code param):
    if you already have a valid session cookie, it redirects you to the main page ("/")
    if you don't have a session cookie, it uses JS to redirect to "/authenticate?one_time_code={one_time_code}"
        this is done to prevent preloading servers from accidentally consuming your one-time use codes

`/authenticate` route (takes one_time_code param):
    validates the one-time code against stored codes
    if valid: marks it as used and sets a signed session cookie, then redirects to "/"
    if invalid: explains to the user that they need to use the login URL printed in the terminal

`/` route is special:
    if you don't have a valid session cookie, shows a login prompt
    if you are authenticated:
        while the error-reporting consent question is unanswered, shows the consent screen
        if any workspaces are known (discovered locally or synced from other devices), lists them all -- even when there is exactly one
        if none are known and the initial discovery is still running, shows a self-refreshing "Discovering workspaces" page
        once discovery completes with no workspaces, shows the agent creation form

`/create` route (requires auth):
    GET: shows a form to enter a git URL for creating a new workspace
    GET with ?retry={create_attempt_id}: pre-fills the form from an interrupted/failed
        create attempt's pending record (the interrupted row's Retry action); opening
        the form is non-destructive -- the dead create attempt's leftover host and
        record are cleaned up only when the new create is submitted
    POST: accepts form data with git_url, starts a create attempt, redirects to /creating/{agent_id}

`/api/create-agent` route (POST, JSON API, requires auth):
    accepts JSON body with git_url, starts a create attempt, returns agent_id and status

`/api/create-agent/{agent_id}/status` route (GET, JSON API, requires auth):
    returns current create attempt status (INITIALIZING, CLONING_REPO, CHECKING_OUT_BRANCH, CREATING_WORKSPACE, WAITING_FOR_READY, DONE, FAILED) and redirect_url when done

`/creating/{agent_id}` route (requires auth):
    shows a progress page that polls the create operation status and streams the
    create attempt log (the log buffer is replayable, so re-entering the page mid-create
    replays the history before tailing live). The status carries a `progress` object
    (`create_progress.py`): a tree of steps -- the desktop's own (fetching the template,
    the `mngr create` call, the boot wait), mngr's phases and the provider's sub-steps
    from the `--format jsonl` progress stream nested under the call, and the template
    bootstrap's events nested under the wait -- weighted by the durations the device
    has learned for each step (`create_progress_history.py`, seeded from
    `create_progress_defaults.toml`), folded into a percent that never moves backwards,
    a rate the page extrapolates between polls, and a coarse remaining time
    auto-redirects to the agent when the create attempt completes
    when no live create attempt is tracked but a pending-create-attempt record survives, the
    page becomes the record-backed detail view: retry + discard for an interrupted
    create attempt, persisted error + log tail + dismiss for a failed one

`/api/v1/account-shares/<account_id>` (GET, cookie or bearer auth):
    the shared-workspaces page's rows for one signed-in account: every active share (the ones counted
    against ``max_shared_workspaces``) plus any suspended one, which holds no slot but is listed so its
    owner can see and remove it (inactive rows are history and are left out), each
    joined to the account's workspace records by workspace id (a legacy host-keyed share through the
    host it names) for the workspace's name and device, with ``record_state`` (``active``,
    ``destroyed``, ``missing``), when the share was created, when its tunnel was last alive
    (``last_seen_display``, from the connector's heartbeat stamp), ``is_likely_leaked`` (the record is
    destroyed or missing) and ``is_hosted_here`` (this device runs the workspace). One connector round
    trip; 404 for an account not signed in here, 503 when Imbue Cloud is not configured on this app, 502
    when the connector cannot be read. Lives in its own ``account-shares`` namespace (like
    ``workspace-sharing``) rather than under ``/api/v1/accounts`` because the gateway's
    ``minds-accounts-read`` permission covers the whole ``/api/v1/accounts`` subtree, and an account's
    share inventory is more than that permission (listing the signed-in accounts) lets an agent see;
    no agent verb maps the namespace, so agents deny-all at the gateway.

`/api/v1/account-shares/<account_id>/<host_id>` (DELETE):
    frees the share's slot: a workspace this device runs for that account is unpublished (``share.env`` cleared, then
    the share deleted; the grants document stays), answering ``outcome: stopped_sharing``; otherwise
    the share alone is deleted at the connector, ``outcome: removed``. Idempotent: a share already
    gone answers ``removed``. The same 404, 503 and 502 answers as the GET.

`/api/v1/desktop/cloud-accounts` (POST, cookie or bearer auth):
    registers a cloud connection: body carries the backend (aws / gcp / azure / modal), an alias,
    the credentials, the region for the pinned backends, and the owner (an account's user id, or
    "local" for this device only; default: the default account). A this-device-only Modal connection
    may also name its Modal namespace (``modal_user_id``, the block's ``user_id`` and so the
    environment name) instead of deriving it from the connection id; 400 under an account owner. The connection is stored
    and its provider block materialized at once; then the route answers 202 with an operation handle
    (``operation_id`` is the connection's instance name, ``kind`` is ``connection_add``) and a worker
    validates the credentials against the cloud (``mngr <backend> prepare``, or one listing for Modal)
    and, for an account owner, pushes the document. A validation failure (``credentials_rejected``,
    502) or a failed push (``sync_failed``, 502: adding a synced connection needs the connector) rolls
    the connection back. 400 for an unknown or signed-out owner; 409 when the owning account is locked
    here or has no master password; 503 (the row dropped) when the signed-in account listing fails, so
    the caller retries, and 503 when the connection stores or the app's concurrency group are not wired.
    Error bodies of the add, update and delete routes (and the failure a status read reports) carry a
    ``code`` naming the failure where the status alone is ambiguous: ``master_password_required`` (409),
    ``connection_in_use`` (409), ``connection_removed_elsewhere`` (409, PUT), ``credentials_rejected``
    (502), ``sync_failed`` (502), ``account_listing_failed`` (503).

    Instead of pasted keys, a connection may name a login installed on each device that uses it
    (``credential_source: "local_profile"``): an AWS profile (``aws_profile``), the tenant and
    subscription of an ``az login`` (``azure_tenant_id``, ``azure_subscription_id``), or a Modal
    profile (``modal_profile``); GCP has no such form (400). The login rides the connection's
    plaintext placement, the document carries no payload, and the provider block names the login
    instead of keys, so nothing secret is stored or synced. The same validation runs. A device
    without that login sees the connection as ``PROFILE_MISSING`` in the providers panel, with the
    command that sets it up; discovery re-checks it every two minutes on its own.

`/api/v1/desktop/cloud-accounts/local-profiles?backend=<aws|azure|modal>` (GET):
    the logins on this machine a connection of that backend can name, read from the tools' own
    files (``~/.aws/config`` and ``~/.aws/credentials``, ``azureProfile.json``, ``~/.modal.toml``):
    ``name``, ``label``, the AWS profile's ``region``, the Azure subscription's ``tenant_id`` and
    ``subscription_id``, and an ``unusable_reason`` for a login the app cannot use (an AWS profile
    with an ``mfa_serial``).

`/api/v1/desktop/cloud-accounts/<instance>/recheck` (POST):
    re-checks a connection now by bouncing discovery: the providers panel's "Check now" button
    after a login was set up outside the app. 404 for an unknown connection.

`/api/v1/desktop/cloud-accounts/operations/<instance>` (GET):
    the state of a registration or update: ``status`` (RUNNING, DONE, FAILED), ``is_done``, the
    ``account`` once done, and on failure the ``error``, its ``code`` and the ``status_code`` the
    synchronous route used to answer with. ``.../logs`` streams the prepare's output as server-sent
    events (``{"log": line}`` frames, replayed from the start, then ``{"done": true}``), the way the
    create operation's logs are streamed; the add-connection dialog shows them under "Show details".

`/api/v1/desktop/cloud-accounts/<instance>` (PUT):
    replaces a connection's credentials (and optionally its alias) in place, keeping its id, instance
    name, owner, backend, and pinned placement: the body carries the backend (which must be the connection's
    own, 400 otherwise) and every one of that backend's credential fields, or ``credential_source:
    "local_profile"`` with the login to name, so a connection may switch between pasted keys and a local
    login either way. The route answers 202 with
    an operation handle (``kind`` ``connection_update``) once the new row and block exist; the worker
    validates the new credentials the way the add route does and, for an account owner, pushes the
    document. On a failure the previous credentials come back and the status read reports 502, or 409
    when the connection was removed on another device meanwhile. An account-owned connection needs the account unlocked
    here with a master password (409); a this-device-only connection needs nothing. While the
    connection's registration or an update is validating, a second update or a removal of the
    connection is refused (409, ``connection_in_use``).
    An app killed mid-update restores the previous credentials on its next start; an account that signs
    out mid-update drops the row, and the update answers 409 (the connection is no longer on this device).

`/api/v1/desktop/cloud-accounts/<instance>` (DELETE):
    removes a connection; 409 while any active machine on it exists in local discovery or in
    any account's synced records, while a workspace is still being created on it, or while its
    registration or an update of its credentials is still validating.

The providers panel on the home screen is fed by the `/ui/ws` channel's ``providers`` frame:
    one section per signed-in account (its Imbue Cloud instance and its connections, each with
    a status), a section for an account that is signed out but still hosts local machines,
    the local profile's section, and the device providers (docker, lima, vultr).

`[<service>.]agent-<hex>.localhost:PORT/*` (workspace-origin catch-all, requires auth):
    a host-header middleware and a catch-all WebSocket route recognize
    `[<service>.]agent-<hex>.localhost(:port)` hosts and byte-forward the
    HTTP or WebSocket request to that workspace's matching backend: the
    bare origin reaches the system_interface, `<service>.` origins reach
    that registered service (resolved via the backend resolver, optionally
    through an SSH tunnel). Unknown hosts return 404; unauthenticated HTML
    navigations redirect to the bare-origin landing page so the user can
    sign in.

## Proxying design

The desktop client only byte-forwards requests. Each workspace owns a family of origins keyed by its workspace id (its agent id -- so URLs survive machine changes; legacy host-keyed origins redirect): the bare `agent-<hex>.localhost` origin serves the shell (system_interface), each registered service owns `<service>.agent-<hex>.localhost`, and deeper labels route to the same service (its own sub-origin space for multi-origin apps). Services therefore run unmodified -- root-absolute URLs, WebSockets, cookies, and service workers all work as written; nothing rewrites anything. One session cookie scoped `Domain=agent-<hex>.localhost` (set by the forwarder's `/goto/` bridge, with `SameSite=None; Secure; Partitioned` so it is sent from inside the chrome's cross-site iframe) covers the whole family.

## The SPA shell and the workspace iframe

The user-facing UI is one web context per window (both in the desktop app and in a plain browser): the Mithril SPA shell, which renders the titlebar, the hub pages (Home, Create, Settings, ... routed client-side by `frontend/src/router.ts`), the sandboxed cross-origin iframe that displays workspace content (`frontend/src/views/shell/WorkspaceFrame.ts`), and in-DOM Mithril modals. Workspace entry goes through `GET /forward-bridge?next=/goto/<workspace-id>/`: Imbue Studio verifies its own session and 302s to the forward plugin's `/_bridge` with a spawn-time secret, which sets the plugin's bare-origin cookie and redirects onward -- the browser twin of the Electron shell's programmatic cookie injection.

Shell<->workspace messaging flows exclusively through the embed contract (`static/embed_contract.js`, documented in `apps/minds/docs/embed-contract.md`); the forward plugin's appended `frame-ancestors` policy is what makes "being framed at all" proof the embedder was allowed.
