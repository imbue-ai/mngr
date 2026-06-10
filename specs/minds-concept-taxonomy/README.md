# Minds concept taxonomy

A code-grounded specification of every concept that exists today (in some form) across
the Minds stack, with the goal of **standardizing the canonical term and definition for
each concept** so the same word means the same thing everywhere.

Scope corresponds to the "Concepts that exist today (in some form)" section of
`Minds_concepts.md`. The "Concepts we might want someday" section is intentionally out of
scope.

## What this covers

Two codebases, treated as one system:

- **`apps/minds/`** — the desktop client (`minds run`): auth, agent creation, reverse
  proxy, backups, latchkey, onboarding, notifications. Plus the Electron shell
  (`apps/minds/electron/`).
- **`.external_worktrees/forever-claude-template/`** (FCT) — the template that runs
  *inside* each agent container: the `system_interface` web app (Python backend +
  TypeScript dockview frontend), the `libs/*` services (bootstrap, app_watcher,
  cloudflare_tunnel, host_backup, runtime_backup, web_server, telegram_bot), `scripts/`,
  and `.agents/skills/`.
- **`libs/mngr*`** — the mngr platform both build on (providers, hosts, agents,
  lifecycle, plugins, hooks, git, message, events, snapshots).

## How to read this

- **`USER-FACING-CONCEPTS.md`** — **start here.** The top-down, primary taxonomy: a list of
  the things a user perceives/chooses/acts on (in the spirit of `Minds_concepts.md`), each
  with its candidate name(s), a working definition, the lower-level technical pieces it
  subsumes, and the ambiguities that make it hard to name precisely. The premise: settling
  the user-facing name for each concept is the lever that unblocks the technical cleanup.
- **`README.md`** (this file) — the bottom-up canonical glossary: one standardized term, a
  plain-language definition, the precise/technical definition, the canonical code location,
  and a status flag per concept. The per-term reference behind the user-facing view.
- **`HUMAN-READABLE.md`** — the expanded plain-language, app/docs-voice definitions for
  non-technical users, with the full reasoning behind each flagged user-facing naming
  decision. The "Plain language" column here is the condensed version of that doc.
- **`CROSS-CUTTING.md`** — the heart of the standardization work: every word that is
  **overloaded** across subsystems (agent, provider, plugin, hook, service/application,
  task/ticket/step, message/notification/event, secret/credential, backup/snapshot,
  panel/tab/view, session, sharing, remote, …), with the concrete disambiguation
  decision for each.
- **`DOC-CODE-DIVERGENCES.md`** — a consolidated register of every place where the code
  does **not** do what the docs (or `Minds_concepts.md`) say, with the authoritative code
  citation.
- **`groups/01..08-*.md`** — the full per-concept evidence: canonical definition, *all*
  usages, competing definitions, terminology variants, ambiguities, divergences, and the
  per-concept recommendation. These are the detailed backing for the glossary.

## Grounding caveat (important)

This taxonomy is grounded in the **actual code**, not the documentation. The docs,
glossaries, specs, and docstrings in both repos have drifted out of date in several
places; those are called out as `DOC/CODE DIVERGENCE` throughout and consolidated in
`DOC-CODE-DIVERGENCES.md`. Where a claim depends on a specific `file:line`, treat the
line number as accurate-as-of-this-analysis and re-confirm before editing — the repos
move. A sample of the highest-impact claims (the `RequestStatus` enum having no `FAILED`,
`layout_inspect.active_panel` actually returning `activeGroup`, latchkey permissions being
keyed by `host_id` not `agent_id`) was spot-verified against the code during synthesis.

## Reading the "Plain language" column

The **Plain language** column is how you'd describe the concept to a non-technical user in
the app or docs. Conventions:

- **⚠** = the *user-facing* name is unsettled or has multiple candidates — a product
  decision to resolve. (The technical canonical term can be settled even when this isn't.)
  Full options are in `HUMAN-READABLE.md`.
- **"internal"** = the *concept itself* has no user-facing manifestation — not merely that
  the code term is plumbing. A concept can have an internal code term and still be
  user-facing: e.g. users never see the word *provider*, but "where does my mind run
  (this computer vs the cloud)?" is a user-facing question that the provider concept
  answers, so it is **not** internal — its plain-language cell describes the user-facing
  thing and points to the control (*launch mode*). Only label a row "internal" when there
  is genuinely nothing a user perceives, chooses, or acts on.
- Where a concept's plain-language framing is genuinely impossible to pin down without a
  prior product decision, the cell says so and points to the blocking decision.

The **Status** column flags concepts whose current *code* naming is inconsistent and needs
a rename/cleanup (see `CROSS-CUTTING.md`) — distinct from the ⚠ user-facing flags.

## The canonical glossary

### Compute substrate (mngr)

| Canonical term | Plain language (user-facing) | Technical definition | Canonical location | Status |
|---|---|---|---|---|
| **provider backend** | *"Where and how your mind runs — on this computer or in the cloud."* User-facing concept (chosen via *launch mode*); users never see the word "provider." | Stateless factory (one per backend type) registered via pluggy `register_provider_backend`; identified by `ProviderBackendName` (`local`/`docker`/`modal`/`lima`/`vultr`/`ovh`/`imbue_cloud`). | `libs/mngr/.../interfaces/provider_backend.py:12` | OK |
| **provider instance** | the *specific* configured "where it runs" a mind was created with. Same user concept as above; surfaced as *launch mode*. | A configured endpoint that creates/manages hosts; identified by `ProviderInstanceName`; declared at `[providers.<name>]`. Reserve bare "provider" for this. | `libs/mngr/.../interfaces/provider_instance.py:291` | `VultrProvider` plays both roles |
| **launch mode** | *"Where your mind runs"* — the actual choice at creation (this Mac / a VM / the cloud). ⚠ raw labels (DOCKER/LIMA/CLOUD/IMBUE_CLOUD) are developer-facing; needs friendly location/plan names | The minds-level `LaunchMode` enum mapping a user's choice to a provider + create templates (`--template <mode>`). | `apps/minds/.../primitives.py` (`LaunchMode`); `agent_creator.py` | enum labels not user-ready |
| **region** | *"Where in the world your mind's computer runs."* ⚠ "region" vs "location" | A provider-specific datacenter string; no shared type; 3 incompatible formats (`US-EAST-VA`/`ewr`/`us-east`). | per-provider `config.py` (`default_region`) | needs shared type |
| **host** | *"Your mind's machine — what you Start and Stop, and whose status you see."* User-facing as an action target; users shouldn't see the word "host." | A managed compute environment (container/VM/sandbox/local) belonging to one provider instance; `HostId` + `HostName`. | `libs/mngr/.../interfaces/host.py:49` | OK |
| **host pool** (imbue_cloud) | internal — pre-warmed machines that make creating a mind faster | Server-side pool of pre-baked VPS hosts the `imbue_cloud` provider *leases* rather than provisions. | `libs/mngr_imbue_cloud/.../instance.py` | OK |
| **agent** | internal primitive — users meet it only via roles (chat agent, worker). ⚠ avoid showing users the bare word "agent" | A named process `(AgentId, AgentName, AgentTypeName, host_id)` in a tmux session on a host; roles layered via labels/templates. | `libs/mngr/.../interfaces/agent.py:39` | roles overloaded (CROSS-CUTTING §1) |
| **host state** | the status badge — *Awake / Asleep / Stopped / Starting*. ⚠ "Paused" vs "Stopped" wording | Host lifecycle enum: BUILDING/STARTING/RUNNING/STOPPING/STOPPED/PAUSED/CRASHED/FAILED/DESTROYED/UNAUTHENTICATED/UNKNOWN. | `libs/mngr/.../primitives.py:244` | OK |
| **agent lifecycle state** | internal — whether an agent is running/waiting/done; feeds status UI indirectly | Agent lifecycle enum: STOPPED/RUNNING/WAITING/REPLACED/RUNNING_UNKNOWN_AGENT_TYPE/DONE/UNKNOWN. | `libs/mngr/.../primitives.py:263` | no CREATING state |

### Git & coding-agent infrastructure

| Canonical term | Plain language (user-facing) | Technical definition | Canonical location | Status |
|---|---|---|---|---|
| **agent branch** | *"Your mind's full change history — viewable and reversible."* ⚠ show as "history"/"versions", not git terms | The `mngr/<agent_name>` branch an agent commits to. | `libs/mngr/.../primitives.py:334` | OK |
| **git remote** | internal git plumbing | A standard git named-URL remote (`origin`). Never bare "remote" (see "remote host"). | `libs/mngr/.../api/git.py` | overloaded with "remote host" |
| **LLM auth mode** | *"How your mind connects to and pays for its AI."* ⚠ frame as "AI connection/billing"; the 3 options need friendly names | LiteLLM virtual key vs raw `ANTHROPIC_API_KEY` vs OAuth subscription. Currently implicit/untyped. Never "AI provider". | (no type — env-determined) | needs a type |
| **model alias** | *"Which AI brain your mind uses — smarter = slower/costlier."* ⚠ tiers (capable/balanced/fast) vs raw model names; `opus[1m]` is jargon | Claude-Code model string resolved to a **concrete model ID** by the LiteLLM proxy. | `litellm_proxy/config.yaml` (de facto registry) | no typed registry |
| **MCP server** / **MCP tool** | *"Extra abilities your mind can use (web search, browser)."* ⚠ never show "MCP"; say "tools"/"abilities" | An external process exposing tools via MCP; tools named `mcp__<server>__<tool>`. Distinct from built-in **Claude Code tools**. | `.claude/settings.json`, `.mngr/settings.toml` | OK |
| **skill** | *"A saved how-to your mind can reuse — a recipe for a task it's done before."* ⚠ "skill" vs "command" vs "recipe" | A markdown `SKILL.md` under `.agents/skills/<name>/` (+ optional `scripts/run.py`), invoked as `/<name>`. | `.agents/skills/` | OK |
| **skills lock** | internal versioning detail | `skills-lock.json` pinning externally sourced skills by content hash (covers 2 of 18 FCT skills). | `skills-lock.json` | partial coverage |
| **Claude Code hook** | internal — if an extensions UI exists, fold under "add-ons" | Shell command on a Claude Code lifecycle event. Distinct from mngr plugin hook, git hook, `LifecycleHook` enum. | `.claude/settings.json` | "hook" 4-way overloaded |
| **mngr plugin** | internal/power — *"add-ons that extend your mind"* | A pluggy-based Python extension of mngr (backends, agent types, CLI, lifecycle). | `libs/mngr/.../plugins/hookspecs.py` | "plugin" 2-way overloaded |
| **Claude Code plugin** | internal/power — same "add-ons" framing as above | An npm/Node Claude Code extension from a marketplace; `name@marketplace`. | `.claude/settings.json` (`enabledPlugins`) | "plugin" 2-way overloaded |

### Compute & runtime

| Canonical term | Plain language (user-facing) | Technical definition | Canonical location | Status |
|---|---|---|---|---|
| **workspace** | *"The mind you create and use."* ⚠⚠ **the top-level naming decision** — mind vs workspace vs assistant (and one-chat-per-mind vs many) | A host + its **services agent**, identified by that agent's `AgentId`; discovered via labels `workspace=<host_name>` + `is_primary=true`. | `apps/minds/.../backend_resolver.py:711` | OK |
| **mind** | product/UI synonym for workspace. ⚠⚠ same decision as above | Not a code type (only `MindLiveness` + UI copy). | `apps/minds/.../mind_liveness.py:55` | UI-only synonym |
| **create template** | *"The starting point a mind is built from."* ⚠ users say "template" | A named preset of `mngr create` args under `[create_templates.<name>]`, applied via `--template`; stackable. | `.mngr/settings.toml`; `cli/common_opts.py:739` | name clashes w/ template repository |
| **template repository** | *"The source a mind is cloned from"* — users call it the "template" | The git repo (forever-claude-template) cloned to form a workspace. | FCT root | name clashes w/ create template |
| **service** | *"An app your mind runs that you can open in a tab."* ⚠ user word "app" (not service/application/view) | A named background process in `services.toml`, run by bootstrap in tmux `svc-<name>` (restart `never`/`on-failure`). | `FCT/libs/bootstrap/.../manager.py` | overloaded with "application" |
| **forwarded service** (today: "application") | same as above — *"an app you can open in a tab"* | A **service** that registered a forwardable URL via `forward_port.py` into `runtime/applications.toml`. | `scripts/forward_port.py`; `libs/app_watcher` | naming split service/application |
| **deferred install** | internal — heavy extras installed on first boot | A package installed idempotently on first boot (not baked into the image), gated by a marker file. | `scripts/deferred_install.sh` | OK |
| **mind liveness** | *"Whether your mind is running"* (the status badge) | Container up/down state (RUNNING/STOPPED/UNKNOWN) from `HostState`; docker/lima only. | `apps/minds/.../mind_liveness.py:55` | OK |
| **system interface health** | internal — drives auto-recovery; user sees only the recovery page | Whether the in-container `system_interface` server responds (HEALTHY/STUCK/RESTARTING/RESTART_FAILED). | `apps/minds/.../system_interface_health.py:80` | enum base style differs |
| **recovery probe** | internal — *"figuring out why your mind isn't responding"* | On-demand in-container diagnostic (7 checks) classifying failure into a `DispatchTier`. | `apps/minds/.../recovery_probe.py` | dup `_OFFLINE_HOST_STATES` |

### Agents & work

| Canonical term | Plain language (user-facing) | Technical definition | Canonical location | Status |
|---|---|---|---|---|
| **worker** | *"A helper your mind spins up to work in the background."* ⚠ "helper" vs "background task" vs "worker" | A short-lived agent created by `launch-task` to do one bounded task on its own `mngr/<name>` branch, reporting via a report file. | `.agents/skills/launch-task/scripts/create_worker.py` | called worker/sub-agent/background agent |
| **lead agent** | internal — the agent that hands work to a helper | The agent that dispatches a worker and merges its branch. | `.agents/skills/launch-task/...` | OK |
| **task brief** | *"The instructions you give a helper."* ⚠ avoid bare "task" | The `task.md` file handed to a worker. | `runtime/launch-task/<name>/task.md` | "task" catastrophically overloaded |
| **ticket** | *"A tracked unit of work / to-do."* ⚠ do users see "tickets"? "tasks"/"to-dos" vs "tickets" | A markdown+frontmatter record under `$TICKETS_DIR`, managed by the `tk` CLI. | `vendor/tk/ticket`; `tickets_parser.py:50` | OK |
| **step record** | *"One step of what your mind is doing now"* (the progress-view items) | A `step: true` ticket (ID has `-step-`): turn-bound, creator-private progress marker. | `tickets_parser.py:78`; `vendor/tk/ticket` | "step" overloaded |
| **code review gate** | internal automated check; the worker **approval** is the user-facing part | The `.reviewer`/imbue-code-guardian review (autofix, verify-architecture/-conversation). | `.reviewer/settings.json` | "review" overloaded |
| **crystallized skill** | internal — *"learned automatically"* vs *"written by hand"* | A skill with `metadata.crystallized: true` (validator then requires `scripts/run.py`). | `.agents/skills/.../SKILL.md`; `validate_skill.py` | validator vs spec-summary divergence |
| **services agent** | hidden by design — **never show users**; never narrate as "primary agent" | The hidden `system-services` agent (`is_primary=true`) running only bootstrap/services; never runs Claude. | `apps/minds/.../agent_creator.py:541`; `system_interface/server.py` | "primary" overloaded |

### Conversation & communication

| Canonical term | Plain language (user-facing) | Technical definition | Canonical location | Status |
|---|---|---|---|---|
| **chat agent** | *"A conversation with your mind"* OR *"an assistant working for you."* ⚠⚠ chat/conversation vs agent/assistant (different products) | An agent created with `--template chat`. No runtime type marker — only creation-time signal. | `system_interface/agent_manager.py:466` | no persistent type label |
| **transcript** | *"Your conversation history."* ⚠ show as "conversation"/"history", never "transcript" | The ordered parsed event sequence (`user_message`/`assistant_message`/`tool_result`) from session files. | `system_interface/session_parser.py` | OK |
| **session** (transcript) | internal — a piece of a conversation; collides with login "session", keep hidden | One `<session_id>.jsonl` Claude file; a transcript spans one or more. | `system_interface/session_watcher.py:196` | collides with auth session |
| **send message** | *"Message your mind."* (settled) | Injecting text into a running agent's stdin (`mngr message` / `POST .../message`). | `libs/mngr/.../api/message.py:46` | "message" triple-overloaded |
| **progress view** (docs say "plan") | *"A live checklist of what your mind is doing."* ⚠⚠ "steps/progress" vs "plan" (and de-collide from build "plans") | The rendered timeline of step records for one turn. "Plan" is docs-only; code says progress view/timeline/sections. | `system_interface/.../ProgressBlock.ts` | "plan" not a code term |
| **inbox** | *"Where your mind's requests wait for you."* ⚠ "Inbox" vs "Approvals/Requests" (it holds only permission requests today) | Event-sourced aggregate (`RequestInbox`) of pending permission requests. | `apps/minds/.../request_events.py:213` | permission-requests only |
| **permission request** | *"Your mind asking to do something — allow or deny."* ⚠ item name + allow/deny wording | A structured `RequestEvent`; resolved GRANTED/DENIED (no FAILED — see divergences). | `apps/minds/.../request_events.py:62` | doc says "failed" (wrong) |
| **notification** | *"An alert from your mind."* (settled) | OS-level desktop alert (message/title/urgency/url); `POST /api/v1/agents/{id}/notifications`. | `apps/minds/.../notification.py:67` | OK |

### Security, identity & access

| Canonical term | Plain language (user-facing) | Technical definition | Canonical location | Status |
|---|---|---|---|---|
| **runtime secret** | internal plumbing | A `runtime/secrets/<name>.env` file inside a container, watched by a service. | `apps/minds/.../tunnel_token_injection.py:28` | "secret" 3-system overload |
| **service credential** | *"A connected account your mind can use (Slack, Google)."* ⚠ "connection"/"connected account" vs "credential" | Latchkey-managed third-party service auth (OAuth/API keys via `latchkey auth`). | `libs/mngr_latchkey/.../core.py:142` | OK |
| **account credential** | *"Your Minds login."* ⚠ collides with "connected accounts" | The SuperTokens session (access/refresh JWT) for a Minds cloud account. | `libs/mngr_imbue_cloud/.../session_store.py` | called "session" |
| **permission** / **scope** (detent) | *"What your mind is allowed to access."* ⚠ separate from autonomy "preference" (don't both say "permissions") | A detent permission schema granted under a scope (`slack-api` → `slack-read-all`). | `libs/mngr_latchkey/.../store.py:206` | collides w/ `permissions_preference` |
| **account** | *"Your Minds account."* ⚠ collides with connected accounts | A signed-in Minds cloud user (`user_id`, email, display name, workspace list). | `apps/minds/.../session_store.py:50` | `AccountSession` misnamed |
| **workspace sharing** | *"Let someone open your mind's app via a link."* ⚠ "Share" (out) vs **file access** (in) | Expose a workspace service to external users via Cloudflare tunnel + Access. | `apps/minds/.../sharing_handler.py:127` | "sharing" 2-way overload |
| **data preference** | setup question *"How much should your mind learn about you?"* ⚠ labels **and** behavior (CONVENIENCE==PRIVACY today — see divergences) | Onboarding Q1 choice (CONVENIENCE/PRIVACY/CONTROL) controlling the local context scan. | `apps/minds/.../primitives.py` | CONVENIENCE==PRIVACY today |

### Data & durability

| Canonical term | Plain language (user-facing) | Technical definition | Canonical location | Status |
|---|---|---|---|---|
| **runtime state** | internal plumbing | Persistent per-feature data under `runtime/<feature>/` (gitignored from main). | `FCT/CLAUDE.md`; convention | events/ dir undocumented |
| **spec** | *"A description of what to build"* (developer-facing) ⚠ collapse spec/blueprint/plan for any user surface | A freeform human-authored design/architecture doc in `specs/<name>/`. | `FCT/specs/` | spec vs blueprint undocumented |
| **blueprint** | *"A step-by-step build plan"* (developer-facing) ⚠ "plan" collides with the progress view | A structured implementation plan in `blueprint/<slug>/` from the `blueprint`(+`-generate`) skills. | `.agents/skills/blueprint-generate` | output also called "plan" |
| **changelog entry** | *"What changed in each update."* (settled) | A per-PR `<project>/changelog/<branch>.md`, fanned nightly into `CHANGELOG.md` + `UNABRIDGED_CHANGELOG.md`. | repo CLAUDE.md convention | CI enforcement opaque |
| **workspace backup** (today: host_backup) | *"A saved copy of your mind to restore from."* ⚠⚠ collapse all 3 durability mechanisms to one user-facing "Backup" | Encrypted, deduplicated restic backup of the whole host_dir to R2/S3, hourly. | `FCT/libs/host_backup/` | "backup" overloaded |
| **runtime checkpoint** (today: runtime_backup) | internal — frequent state checkpoints; part of "Backup" to users | A git commit of `runtime/` to orphan branch `mindsbackup/<agent_id>` every 60s. | `FCT/libs/runtime_backup/` | called "backup" |
| **host snapshot** | internal/power — a provider-level VM snapshot | A provider-level VM/disk snapshot (`mngr snapshot`). | `libs/mngr/.../cli/snapshot.py` | "snapshot" 3-way overload |
| **file sharing** | *"Let your mind read/write specific files on your computer."* ⚠ name must not collide with "Share" | The WsgiDAV/WebDAV service at `/api/v1/files` exposing home + `/tmp`. | `apps/minds/.../webdav.py` | OK |
| **agent memory** | *"What your mind remembers about you and your work."* ⚠ one unified "Memory" vs two (workspace vs user) | Claude auto-memory at `runtime/memory/` (`autoMemoryDirectory`). Distinct from **user memory**. | `FCT/.claude/settings.json` | format opaque |
| **event log** | *"A record of what your mind did."* ⚠ frame as "Activity"/"History" (also the future audit-log concept) | Persistent append-only `events/<source>/events.jsonl` read by `mngr event`. Distinct from in-memory SSE **event stream**. | `libs/mngr/.../api/events.py:54` | SSE system undocumented |
| **upstream** | *"The template your mind updates from."* ⚠ users say "template", not "upstream"/"parent" | The template repo a workspace derives from (`parent.toml`); `update-self` pulls, `submit-upstream-changes` pushes. | `FCT/parent.toml` | "parent" vs "upstream" |

### Presentation

| Canonical term | Plain language (user-facing) | Technical definition | Canonical location | Status |
|---|---|---|---|---|
| **panel** | internal term — users say "tab" | The atomic addressable content unit in the dockview layout (component `chat`/`iframe`/`subagent`), addressed by a typed **ref**. | `system_interface/.../DockviewWorkspace.ts` | OK |
| **group** | internal layout term | A set of panels sharing one pane + tab bar; exactly one active at a time. | dockview; `layout_ops.py` | OK |
| **tab** | *"A tab."* (settled) | User-facing name for a panel's tab-bar entry. | `manage-layout/SKILL.md` | OK |
| **layout** | *"How your tabs are arranged."* (mostly settled) | The persisted dockview tree of groups/panels (`workspace_layout/layout.json`). | `DockviewWorkspace.ts:101` | `active_panel` returns a group id |
| **terminal** | *"A command line into your mind's computer."* ⚠ "terminal" vs "command line/console" for non-technical users | An iframe panel loading a `ttyd` web shell at `/service/terminal/`. | `scripts/run_ttyd.sh`; `libs/mngr_ttyd` | two ttyd provisioning paths |
| **browser** | *"A web browser your mind can see and click for you."* ⚠ **not built yet** — don't promise in UI/docs | Aspirational; closest is an ad-hoc `url:<hash>` iframe navigable via `replace-url`. | (none) | listed as existing, isn't |

See `HUMAN-READABLE.md` for the full reasoning behind each ⚠ user-facing decision,
`CROSS-CUTTING.md` for the internal overloaded-term decisions, and
`DOC-CODE-DIVERGENCES.md` for the divergence register.
