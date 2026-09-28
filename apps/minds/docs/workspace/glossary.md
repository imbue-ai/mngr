# Glossary

Key concepts in the minds system:

- **workspace**: the logical unit a user works out of: a collection of permissions (what the agent can access, which outside users can access it), apps, data, and customizations.
  A workspace is identified by its primary agent's id (its *workspace id*, which never changes for the life of the workspace) and discovered via that agent's `is_primary` label; the *machine* it currently runs on is a swappable attribute.
  A workspace holds several agents: exactly one primary agent, plus the chat, worktree, and worker agents created within it over time.
  Workspaces are created from a template repository via `mngr create --new-host` (all configuration lives in the template's `.mngr/settings.toml`), and their backups are substrate-independent: a workspace's data can be restored onto a different machine.

- **machine**: the place a workspace runs -- the thing with CPUs, RAM, disk, and an IP address.
  At the mngr level a machine is a *host*, identified by its host id.
  A machine cannot be copied (there are never two live instances of one machine), though an imbue_cloud machine can be suspended and resumed on different bare metal, keeping its host id.
  mngr-level code speaks host/agent; minds-level code speaks machine/workspace (see `specs/machine-workspace-naming/decisions.md`).

- **creation**: anything a user makes in their workspace.
  Used only at the highest conceptual level; the working vocabulary is the kinds: *apps* (opened as windows on the desktop), *skills* (an *automation* is a skill run automatically on a schedule), *data* (documents, images, notes), and *customizations* (changes to any of the above).

- **app**: something the user can open as a window on the desktop and interact with.
  Lives under `system/apps/<package>/` in the workspace, runs as a supervisord program, and registers its port in `data/.state/apps.toml` via `system/scripts/forward_port.py`.
  Each app gets a local URL (via the desktop client) and, while sharing is enabled, a shared URL (via the workspace's share through the self-hosted relay).
  The built-in apps are the chat, the terminal, the file viewer, the browser, and Getting Started (the page of starting points and templates a fresh workspace opens beside its chat); the system interface is the shell that hosts their windows.
  Never "application" -- always "app".

- **service**: a background supervisord program with no window (host-backup, the share-gateway, the app watcher).
  Standalone services live under `system/services/`; a service that exists solely to support one app lives in that app's folder and is named `<app>-<role>`.
  "Web service" is retired vocabulary: a thing the user can open is an app.

- **automation** [future]: a skill that runs automatically on a schedule, without the user asking.
  The scheduling primitive is landing separately; until then skills run when invoked.

- **customization**: a user's change to any existing part of the workspace -- a modified app, an edited skill, a tweaked chat behavior.
  Not a standalone kind of creation; everything in minds can be modified.

- **template**: a publishable, reusable, *bootable* snapshot of the creations a mind has built, pushed to a GitHub repo so another mind can be created from it or adopt it (one repo can accumulate several templates).
  A template can include zero or more creations plus customizations to existing things.
  See the workspace's publish-template / use-template skills.

- **template base**: the pristine template commit a workspace started from (or last updated itself to), named by the newest template-state marker on its first-parent history: an `Initial workspace commit` is its own base, and an `update-self:` merge's base is its upstream (second) parent, never the merge itself, which also holds the workspace's own work.
  Publishing a template diffs against it; formerly called the "creation snapshot".

- **primary agent**: the single `system-services` agent on each workspace host, labeled `is_primary=true`.
  It runs bootstrap and the background services rather than a user-facing chat -- it is a plain `command`-type agent whose window-0 command is `sleep infinity`, so no claude is ever involved.
  Its `workspace_display_name` label holds the workspace's human-readable name (the normalized slug is the host's name).
  Hidden from the UI agent list and protected against direct destroy.

- **chat**: a user-facing conversation in a workspace, one per chat window: a sequence of agent transcripts run by one agent at a time (the template's `docs/system/blueprint/chat-agent-split/`). Its id is its first agent's id, and every agent the chat app creates for it carries that id as `MINDS_CHAT_ID`. A chat that has run on several agents has a chat record in the workspace (`data/.apps/chat/chats/<chat-id>/record.json`) naming its agents in order; the earlier ones are *archived agents*, kept for their transcripts and never listed as chats. A *handoff* is how a chat moves to another harness: the chat app archives the agent it is leaving and creates a successor with a summary, and the record carries the handoff's state while the chat is *converging*. A *rebind* is how a chat moves to another account on its own harness and lane: the same agent is stopped, repointed at the new account's credential in its own state dir, relabeled, and started again, with its transcript, tk steps, and model settings kept; the record carries a rebind's state the same way. The desktop client's permission-resolution nudge therefore goes to the chat, through the workspace's chat app, rather than to an agent by id.
- **chat agent**: the mngr agent a chat currently runs on, created on demand in a workspace by the chat app; the phrase names the agent, never the chat.
  Created with `--transfer none`, so it shares the primary agent's work_dir, and bound on its create to one signed-in provider account under `~/.minds/accounts/` (an `--env CLAUDE_CONFIG_DIR=<account dir>` for claude). A create that names no account gets the workspace's default one from `.mngr/settings.local.toml`, which the workspace's chat app writes; with no account signed in the create is refused, since `~/.claude` holds no credential.
  Bootstrap seeds the first one on initial container boot; the count grows and shrinks with the user's workload, and is not capped.

- **worktree agent**: a mngr agent the user creates with `--transfer git-worktree` on branch `mngr/<name>`.
  Unlike a chat agent it lives in its own git worktree, outside the repo-root work_dir; no app on the desktop offers a launch path for one.
  Labeled `user_created=true`.

- **worker agent**: a mngr agent created by *another agent* (not by the user) when it delegates a task to a sub-agent, via the `launch-task` skill.
  Labeled `agent_created=true`.
  Not tied to any window.
  The `user_created` / `agent_created` distinction drives the OOM shedding bands.

- **template repository**: a git repository (e.g. default-workspace-template) that defines a workspace's entire runtime: Dockerfile, apps, services, skills, scripts, and mngr configuration.

- **desktop client**: a local process (`minds run`) that handles authentication, agent creation, and reverse proxying.
  Multiplexes access to multiple workspaces through a single local endpoint.

- **browser authorization component** (fully, the *desktop-app backend-server* browser authorization component): the browser-facing part of the *desktop client* -- the bare-origin web UI served by `minds run` (`apps/minds/imbue/minds/desktop_client/`) on a single local endpoint.
  It serves every page the browser reaches, carries the browser's session, and authenticates it.
  The desktop client's other duties (agent and workspace creation, reverse proxying to workspaces) sit outside the browser authorization component.

- **session**: the authenticated state of a browser connected to the *browser authorization component*, carried by the **session cookie** -- an HTTP cookie whose value is a token signed with the *installation*'s session-signing key, so a tampered cookie, one minted under another installation, or one older than 30 days is rejected.
  A browser authenticates a session by opening the one-time authentication URL that `minds run` prints to its terminal; the authenticated session is then the sole credential gating every page the component serves, and it covers all of the user's workspaces.
  It is scoped to a single *installation*, and is distinct from the optional imbue-cloud account sign-in (a separate credential for cloud-backed features).

- **installation**: one copy of the desktop client's local state -- a single data directory (e.g. `~/.minds`), so one installation = one data directory.
  Its one-time code, session-signing key, sessions, and error-reporting consent all live in that data directory and do not carry across to another one on the same machine; `minds run` pointed at a different data directory is a different installation.

- **bootstrap**: `uv run bootstrap`, the process that runs first-boot setup inside each agent container and then execs `supervisord -n` to launch the apps and background services.

- **supervisord**: the process-control system running inside each agent container that supervises the apps and background services, each declared as a `[program:*]` section in `supervisord.conf` -- or, where a template splits them out, in its own file pulled in by that config's `[include]` glob (logs under `/var/log/supervisor`).
  Replaces the old custom service manager that watched `services.toml` and ran services in tmux windows.

- **app watcher**: a background service that monitors `data/.state/apps.toml` and writes service events to `events/services/events.jsonl` so the desktop client can discover an agent's apps.
  (Forwarding reconciliation happens on the minds side, via the `mngr forward` plumbing -- not in the watcher.)

- **to publish**: to make a workspace reachable in a browser at its share URL through the self-hosted relay, for as long as it stays published.
  Publishing is workspace-level and is started from the desktop client's Share tab by a signed-in account associated with the workspace: it provisions the relay materials and certificate that the share-gateway runs the share stack from, and unpublishing drops the tunnel, cutting off anyone connected.
  Who may enter a published workspace is decided by its grants; the URL alone admits nobody.
  The code and older docs say "share" for this; the verb is "publish" from here on, and "share" survives only inside existing names such as share URL, share panel, and share-gateway.

- **grant**: an entry in a workspace's grants document (`data/.secrets/share_grants.toml`) that admits a party to one app of the workspace while it is published.
  The system interface is an app like the others, with one difference: a grant on it admits the entire workspace, every app included, which the Share tab calls the whole machine. A grant on any other app admits that app alone.
  The party is named by an account id, an email address, or a domain. The share-gateway re-reads the document on every request, so removing a grant takes effect immediately.
  A grant names who may enter and nothing more: it does not notify anyone, and it does not make someone a visitor until they visit.

- **visitor**: a person who opens someone else's published workspace in a browser through its share URL.
  A role toward one workspace, not a kind of account: the same person publishes their own workspaces and is a visitor to anyone else's. A visitor needs an imbue account with a verified email, because that is what the grants are checked against; they need no desktop client and no machine.
  Someone becomes a visitor of a workspace the first time the connector's share broker (the sign-in step every visit passes through) authorizes their visit, and stops being one the moment their grant is removed, the workspace is unpublished, or their account is suspended. The account that published a workspace is never a visitor of it: the broker and the share-gateway give that account a separate path that ignores the grants.
  Not the anonymous *visitor id* of download attribution (a browser on imbue.com before any account exists), and not the relay's *visitor connection* (the browser's TCP connection).

- **granter**: the role of whoever authors a grant. Any account with write access to the workspace can: the desktop client lets a signed-in account associated with the workspace edit the grants document, and inside the workspace anyone with a shell can rewrite it.
  The granter of a grant is who may invite its grantee, and whose invitation allowance the delivery counts against.

- **user grant**: a grant naming an account by its user id; the share-gateway matches these first.
  What a typed address becomes when it resolves to an account at grant time, and what an email grant becomes on its grantee's first visit.

- **email grant**: a grant naming an email address, normally one with no account yet; it admits the person who signs in with that address, verified.
  If the address does have an account (the resolution at grant time missed it, or the account came later), the grant still admits them, and the share-gateway upgrades it to a user grant on their first visit. Older docs call this entry an invite; that word now means only an invitation.

- **domain grant**: a grant naming an email domain; it admits everyone who signs in with a verified address at that domain, including people the granter has never met.
  A domain grant cannot be invited: the granter passes the share URL on themselves.

- **grantee**: the party a grant admits: the account of a user grant, the person at an email grant's address, or everyone at a domain grant's domain.

- **invitee**: a grantee of a user grant or an email grant who has been invited, meaning an invitation exists for their grant.

- **registered address**: an email address that is the verified email of an imbue account; *unregistered* otherwise.
  Registration is what invitation routing checks, whatever the grant's kind says; it says nothing about whether minds is installed.

- **share panel**: the granter's surface for creating grants and inviting: the "Share machine: <name>" panel in the desktop client's workspace options.

- **joined**: a grantee's first authorized visit to the workspace, as recorded by the share broker's visit log. The visitor's own action, not a delivery outcome.

- **invitation** [future]: the message telling a user grant's or an email grant's grantee that they have been granted access, delivered by Imbue over one channel from the single invitation content, carrying the plain share URL.
  An invitation belongs to exactly one grant. To *invite* is to create a delivery for it.

- **invitation content** [future]: the one content model behind every invitation (who granted access, which workspace, which app, the share URL, and how to stop receiving mail), rendered once per channel. It carries no message written by the granter.

- **channel** [future]: how a delivery reaches an invitee: email, or an in-app notification in the minds notification feed.
  Imbue selects the channel from the invitee's registration and notification preferences; the granter never chooses. In-app is modelled now and implemented later.

- **delivery** [future]: one attempt to deliver an invitation over one channel, with a delivery outcome.
  An invitation has one or more deliveries; inviting again creates another, subject to the invitation allowance and a per-grant cooldown held in one policy object so that it is easy to change.

- **delivery outcome** [future]: the result of a delivery. At attempt time: `sent`, `suppressed`, `over allowance`, `too soon`, `unroutable`, or `failed`. Later, from the email provider's webhooks: `delivered`, `bounced`, `complained`, or `unsubscribed`.

- **notification preferences** [future]: a registered account's choice of the channels it accepts notifications on, email and in-app, both on by default. Invitations are one kind of notification among others.
  Unregistered addresses have no preferences. The invitation email's unsubscribe link turns email off for a registered address and adds any address to the suppression list.

- **suppression list** [future]: the addresses Imbue will not email, each with a reason (`bounced`, `complained`, `unsubscribed`, `reported`, or `blocked`) and a source (an email provider webhook, the invitation email's own unsubscribe or report link, or an operator).
  Checked before every email delivery; a suppressed delivery still counts against the allowance. `reported` means the recipient used the email's link to report the invitation as unwanted, which also counts against the granter.

- **email provider** [future]: the external service (an ESP) that sends invitation email and reports delivery events by webhook. The design does not name one.

- **invitation allowance** [future]: the number of deliveries a granter may attempt per channel in any rolling 24 hours: a constant per channel in one policy object, counted from delivery rows (a per-account override may come later).
  Every attempted delivery counts, including suppressed ones; refused attempts do not. The granter learns of the allowance only when refused.

- **granter-visible invitation outcome** [future]: what the granter may learn about an invitation: `invited`, `could not invite` (the reason withheld), `over allowance`, `too soon` (the same person was invited within the cooldown), or `joined`.
  Bounces, complaints, opt-outs, reports, and suppression are never shown to the granter, so that abuse is guesswork rather than a probe.

- **share-gateway**: the background service that watches `data/.secrets/share.env` for relay materials and runs the workspace's share stack (relay tunnel + in-workspace TLS) while sharing is enabled.
  Who may access the share is controlled by the grants document (`data/.secrets/share_grants.toml`), which the desktop client rewrites as the user edits grants.

- **service event**: a JSON line in `events/services/events.jsonl` that registers (or deregisters) a name and URL for discovery.
  The desktop client's MngrStreamManager watches these events to discover agent backends.
  (The path and event vocabulary predate the app rename and are treated as plumbing.)

- **launch mode**: how the workspace runs; selects the mngr provider instance and create-template.
  DOCKER runs in a Docker container on the user's machine.
  LIMA runs in a Lima VM.
  VULTR runs in Docker on a Vultr VPS.
  AWS runs on an EC2 instance.
  IMBUE_CLOUD leases a pre-baked pool host via the imbue_cloud provider plugin.
  MODAL runs in a Modal sandbox using the local machine's own Modal token; sandboxes are ephemeral (~1 day max), so it is testing-only.

- **machine size**: how big a remote (imbue_cloud) machine is, in two independent factors (specs/slice-fleet). *Units* are the single compute knob -- 1 unit = 1GiB of machine RAM, with vCPUs and fair-share bandwidth scaling proportionally; allowed sizes are multiples of 8 units up to 128. *Disk* is a second, grow-only factor, sized once at creation (3.5GiB per unit) and grown independently afterwards; it never shrinks. Resizing is record-then-restart: `mngr imbue_cloud machines resize` stamps the desired size, and the machine's next restart applies it (in place when its box has room, otherwise via a restore onto a box that does). Every new workspace starts at the default 8-unit size.

- **environment**: an environment is a single deployed instance of the minds system.
  It owns, among other things, a data root, a Modal environment, a Neon project, and a SuperTokens app.
  Every environment belongs to exactly one tier, and takes its account credentials and deploy configuration from it.
  Production and staging are environments whose names are identical to their tier names, while dev-<user> and ci-<timestamp>-<uuid> are dynamic environments that developers and CI create and destroy within their tiers.

- **tier**: a category of environment, and it determines, among other things, account credentials, deploy configuration.
  Bare metal boxes exclusively belong to one tier, and cannot be shared between them.
  Production and staging are tiers that contain exactly one environment within them, while the CI and Dev tiers may have multiple CI and Dev environments respectively.

- **adoption**: the user's own device taking ownership of a leased imbue_cloud slice's SSH trust material.
  On lease -- and on the first connect for hosts leased earlier -- the client rotates both of the slice's sshd host keys to fresh user-generated keys (pinned user-origin in mngr's host-key store, which connector bake-time material can never displace) and installs an in-VM reconciler that re-asserts the owner's `authorized_keys` and host key on every boot (a slice's cloud-init runs exactly once, at first boot, so the adopted material persists across stop/start and restores on its own; the reconciler is being retired, see imbue-ai/mngr-internal#1327).
  After adoption, host-key trust flows only through the user's synced workspace records; the connector is trusted exactly once, at lease handoff. The pins are bound to an address and port, and the machine changes ports on every restore (driven by this client, an operator, a rollback, or another device), so the client remembers the endpoints it last pinned and moves the pins to the connector's current endpoints before every connection, with no network round trip.
  Idempotent and marker-driven; a served key that matches neither the pins nor an in-flight rotation is refused, never re-trusted.
  See `libs/mngr_imbue_cloud/README.md` ("Adoption and key rotation") and [the lost-device runbook](../deploy/reference/lost-device-runbook.md).

- **stop kind**: why a remote (imbue_cloud) machine's current stop happened, recorded by the connector beside its lifecycle status and cleared by every start (`specs/workspace-stop-kinds.md`).
  `owner` (the user's own stop, from any device) and `idle` (an operator stop to free capacity) are the owner's to end with Start; `maintenance` (an operator hold, such as a fleet migration) and `suspension` (the account suspend fan-out) are not -- a held machine offers no Start control (a `maintenance` hold is named "Maintenance" by its badge; a `suspension` reads as plain "Stopped"), and the connector refuses owner starts of it.
  A kind this build does not recognize is treated as a hold (shown but not actionable).
