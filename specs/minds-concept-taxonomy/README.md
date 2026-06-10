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

- **`README.md`** (this file) — the canonical glossary: one standardized term + one-line
  definition + canonical code location per concept. Start here.
- **`HUMAN-READABLE.md`** — the plain-language, app/docs-voice definition of each concept
  for non-technical users, with explicit flags wherever the *user-facing* name is unsettled
  or has competing candidates (the product naming decisions to actually resolve).
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

## The canonical glossary

The recommended canonical term for each concept, its one-line definition, and where it is
(or should be) defined in code. "Status" flags concepts whose current naming is
inconsistent and needs a rename/cleanup (see `CROSS-CUTTING.md`).

### Compute substrate (mngr)

| Canonical term | Definition | Canonical location | Status |
|---|---|---|---|
| **provider backend** | Stateless factory (one per backend type) registered via pluggy `register_provider_backend`; identified by `ProviderBackendName` (`"local"`, `"docker"`, `"modal"`, `"lima"`, `"vultr"`, `"ovh"`, `"imbue_cloud"`). | `libs/mngr/imbue/mngr/interfaces/provider_backend.py:12` | OK |
| **provider instance** | A configured endpoint that creates/manages hosts; identified by `ProviderInstanceName`; declared at `[providers.<name>]`. Reserve the bare word **provider** for this. | `libs/mngr/imbue/mngr/interfaces/provider_instance.py:291` | naming: `VultrProvider` plays both roles |
| **region** | A provider-specific datacenter string on a cloud provider's config. No shared type; three incompatible formats today (`US-EAST-VA`, `ewr`, `us-east`). | per-provider `config.py` (`default_region`) | needs shared type |
| **host** | A managed compute environment (container/VM/sandbox/local machine) belonging to one provider instance; identified by `HostId` + `HostName`. | `libs/mngr/imbue/mngr/interfaces/host.py:49` | OK |
| **host pool** (imbue_cloud) | Server-side pool of pre-baked VPS hosts the `imbue_cloud` provider *leases* (vs. provisions). | `libs/mngr_imbue_cloud/.../instance.py` (`lease_host`) | OK |
| **agent** | A named, identified process (typically a coding agent) running in a tmux session on a host; identity `(AgentId, AgentName, AgentTypeName, host_id)`. Roles are layered on top via labels/templates. | `libs/mngr/imbue/mngr/interfaces/agent.py:39` | OK (roles overloaded — see CROSS-CUTTING) |
| **host state** | Host lifecycle enum: BUILDING/STARTING/RUNNING/STOPPING/STOPPED/PAUSED/CRASHED/FAILED/DESTROYED/UNAUTHENTICATED/UNKNOWN. | `libs/mngr/imbue/mngr/primitives.py:244` | OK |
| **agent lifecycle state** | Agent lifecycle enum: STOPPED/RUNNING/WAITING/REPLACED/RUNNING_UNKNOWN_AGENT_TYPE/DONE/UNKNOWN. | `libs/mngr/imbue/mngr/primitives.py:263` | no CREATING state |

### Git & coding-agent infrastructure

| Canonical term | Definition | Canonical location | Status |
|---|---|---|---|
| **agent branch** | The `mngr/<agent_name>` branch an agent commits to. | `libs/mngr/imbue/mngr/primitives.py:334` (`DEFAULT_BRANCH_PREFIX`) | OK |
| **git remote** | A standard git named-URL remote (`origin`). Never use bare "remote" — see **remote host**. | `libs/mngr/imbue/mngr/api/git.py` | overloaded with "remote host" |
| **LLM auth mode** | How a coding agent authenticates to the model API: LiteLLM virtual key, raw `ANTHROPIC_API_KEY`, or OAuth subscription. Currently implicit/untyped. Never call this an "AI provider". | (no type — env-determined) | needs a type |
| **model alias** | The Claude-Code-level model string (e.g. `opus[1m]`); resolved to a **concrete model ID** (`claude-opus-4-8`) by the LiteLLM proxy. | `litellm_proxy/config.yaml` (de facto registry) | no typed registry |
| **MCP server** / **MCP tool** | An external process exposing tools to the agent via MCP; tools are named `mcp__<server>__<tool>`. Distinct from built-in **Claude Code tools**. | `.claude/settings.json`, `.mngr/settings.toml` | OK |
| **skill** | A markdown `SKILL.md` under `.agents/skills/<name>/` (+ optional `scripts/run.py`), invoked as `/<name>`. | `.agents/skills/` | OK |
| **skills lock** | `skills-lock.json` pinning *externally sourced* skills by content hash (covers only 2 of 18 FCT skills). | `skills-lock.json` | partial coverage |
| **Claude Code hook** | Shell command on a Claude Code lifecycle event in `.claude/settings.json`. Distinct from **mngr plugin hook** (pluggy), **git hook**, and the `LifecycleHook` enum. | `.claude/settings.json` | "hook" 4-way overloaded |
| **mngr plugin** | A pluggy-based Python extension of mngr (provider backends, agent types, CLI, lifecycle). Distinct from **Claude Code plugin** (npm). | `libs/mngr/imbue/mngr/plugins/hookspecs.py` | "plugin" 2-way overloaded |
| **Claude Code plugin** | An npm/Node Claude Code extension from a marketplace; `name@marketplace`. | `.claude/settings.json` (`enabledPlugins`) | "plugin" 2-way overloaded |

### Compute & runtime

| Canonical term | Definition | Canonical location | Status |
|---|---|---|---|
| **workspace** | The persistent unit a user interacts with: a host + its **services agent**, identified at the API by that agent's `AgentId`; discovered by the labels `workspace=<host_name>` + `is_primary=true`. | `apps/minds/.../backend_resolver.py:711` | OK |
| **mind** | Product-/UI-level synonym for **workspace**. Not a code type (only `MindLiveness` + UI copy). | `apps/minds/.../mind_liveness.py:55` | UI-only synonym |
| **create template** | A named preset of `mngr create` args under `[create_templates.<name>]`, applied via `--template`; stackable. | `.mngr/settings.toml`; `libs/mngr/.../cli/common_opts.py:739` | name clashes with "template repository" |
| **template repository** | The git repo (forever-claude-template) cloned to form a workspace. | FCT root | name clashes with "create template" |
| **service** | A named background process declared in `services.toml`, run by the bootstrap manager in tmux window `svc-<name>` with restart policy `never`/`on-failure`. | `.external_worktrees/.../libs/bootstrap/.../manager.py` | overloaded with "application" |
| **forwarded service** (today: "application") | A **service** that registered a forwardable URL via `forward_port.py` into `runtime/applications.toml`. | `scripts/forward_port.py`; `libs/app_watcher` | naming split service/application |
| **deferred install** | A heavy package installed idempotently on first boot (not baked into the image), gated by a marker file. | `scripts/deferred_install.sh` | OK |
| **mind liveness** | Container up/down state (RUNNING/STOPPED/UNKNOWN) derived from `HostState`; docker/lima only. | `apps/minds/.../mind_liveness.py:55` | OK |
| **system interface health** | Whether the in-container `system_interface` web server is responding (HEALTHY/STUCK/RESTARTING/RESTART_FAILED). | `apps/minds/.../system_interface_health.py:80` | enum base style differs |
| **recovery probe** | On-demand in-container diagnostic (7 checks) classifying *why* a workspace is broken into a `DispatchTier`. | `apps/minds/.../recovery_probe.py` | dup `_OFFLINE_HOST_STATES` |

### Agents & work

| Canonical term | Definition | Canonical location | Status |
|---|---|---|---|
| **worker** | A short-lived agent created by `launch-task` (or crystallize/heal/update) to do one bounded task on its own `mngr/<name>` branch, reporting back via a report file. | `.agents/skills/launch-task/scripts/create_worker.py` | called worker / sub-agent / background agent |
| **lead agent** | The agent that dispatches a worker and merges its branch. | `.agents/skills/launch-task/...` | OK |
| **task brief** | The `task.md` file handed to a worker. Avoid bare "task". | `runtime/launch-task/<name>/task.md` | "task" catastrophically overloaded |
| **ticket** | A markdown+frontmatter record under `$TICKETS_DIR`, managed by the `tk` CLI. | `vendor/tk/ticket`; parsed by `system_interface/tickets_parser.py:50` | OK |
| **step record** | A `step: true` ticket (ID contains `-step-`): a turn-bound, creator-private progress marker that drives the progress view. | `tickets_parser.py:78`; `vendor/tk/ticket` | "step" overloaded |
| **code review gate** | The `.reviewer`/imbue-code-guardian automated review (autofix, verify-architecture, verify-conversation). Distinct from worker **approval gates**. | `.reviewer/settings.json` | "review" overloaded |
| **crystallized skill** | A skill with `metadata.crystallized: true` (validator then requires `scripts/run.py`). Opposite: **hand-authored skill**. | `.agents/skills/.../SKILL.md`; `validate_skill.py` | validator vs spec-summary divergence |
| **services agent** | The hidden `system-services` agent (`is_primary=true`) that runs only bootstrap/services and never actually runs Claude. Prefer this term over "primary agent". | created in `apps/minds/.../agent_creator.py:541`; guarded in `system_interface/server.py` | "primary" overloaded |

### Conversation & communication

| Canonical term | Definition | Canonical location | Status |
|---|---|---|---|
| **chat agent** | An agent created with `--template chat` that the user talks to via the chat panel. No runtime type marker — only creation-time signal. | `system_interface/agent_manager.py:466` | no persistent type label |
| **transcript** | The ordered parsed event sequence (`user_message`/`assistant_message`/`tool_result`) built from one or more Claude **session** JSONL files. | `system_interface/session_parser.py`, `session_watcher.py` | OK |
| **session** (transcript) | One `<session_id>.jsonl` Claude Code file. A transcript spans one or more sessions. Collides with auth **session**. | `system_interface/session_watcher.py:196` | collides with auth session |
| **send message** | Injecting text into a running agent's tmux stdin (`mngr message` / `POST /api/agents/{id}/message`). Avoid bare "message". | `libs/mngr/imbue/mngr/api/message.py:46` | "message" triple-overloaded |
| **progress view** (today docs say "plan") | The rendered timeline of step records for one turn. The word "plan" is docs-only; code says progress view / timeline / sections. | `system_interface/frontend/.../ProgressBlock.ts` | "plan" not a code term |
| **inbox** | The desktop-client drawer of pending permission requests (event-sourced `RequestInbox`). | `apps/minds/.../request_events.py:213` | exclusively permission requests |
| **permission request** | A structured `RequestEvent` an agent emits for user authorization; resolved GRANTED/DENIED (no FAILED). | `apps/minds/.../request_events.py:62` | doc says "failed" — wrong |
| **notification** | An OS-level desktop alert (message/title/urgency/url) dispatched by the desktop client; `POST /api/v1/agents/{id}/notifications`. | `apps/minds/.../notification.py:67` | OK |

### Security, identity & access

| Canonical term | Definition | Canonical location | Status |
|---|---|---|---|
| **runtime secret** | A `runtime/secrets/<name>.env` file inside a container, watched by a service. Distinct from Modal Secrets and Vault secrets. | `apps/minds/.../tunnel_token_injection.py:28` | "secret" 3-system overload |
| **service credential** | Latchkey-managed third-party service auth (OAuth/API keys via `latchkey auth`). | `libs/mngr_latchkey/.../core.py:142` | OK |
| **account credential** | The SuperTokens session (access/refresh JWT) for a Minds cloud account. | `libs/mngr_imbue_cloud/.../session_store.py` | called "session" |
| **permission** / **scope** (detent) | A detent permission schema name granted under a scope (e.g. `slack-api` → `slack-read-all`). | `libs/mngr_latchkey/.../store.py:206` | collides w/ `permissions_preference` |
| **account** | A signed-in Minds cloud user (`user_id`, email, display name, workspace list). | `apps/minds/.../session_store.py:50` (`AccountSession`) | `AccountSession` misnamed |
| **workspace sharing** | Exposing a workspace service to external users via Cloudflare tunnel + Access. Distinct from **file access grant** (WebDAV). | `apps/minds/.../sharing_handler.py:127` | "sharing" 2-way overload |
| **data preference** | The onboarding Q1 choice (CONVENIENCE/PRIVACY/CONTROL) controlling the local context scan. | `apps/minds/.../primitives.py` (`UserDataPreference`) | CONVENIENCE==PRIVACY today |

### Data & durability

| Canonical term | Definition | Canonical location | Status |
|---|---|---|---|
| **runtime state** | Persistent per-feature data under `runtime/<feature>/` (gitignored from main). | `FCT/CLAUDE.md`; convention | events/ dir undocumented |
| **spec** | A freeform, human-authored design/architecture doc in `specs/<name>/`. | `FCT/specs/` | spec vs blueprint undocumented |
| **blueprint** | A structured implementation plan in `blueprint/<slug>/` produced by the `blueprint` + `blueprint-generate` skills. | `.agents/skills/blueprint-generate` | output also called "plan" |
| **changelog entry** | A per-PR `<project>/changelog/<branch>.md`, fanned nightly into `CHANGELOG.md` (concise) + `UNABRIDGED_CHANGELOG.md` (verbatim). | repo CLAUDE.md convention | CI enforcement opaque |
| **workspace backup** (today: host_backup) | Encrypted, deduplicated restic backup of the whole host_dir to R2/S3, hourly. | `FCT/libs/host_backup/` | "backup" overloaded |
| **runtime checkpoint** (today: runtime_backup) | A git commit of `runtime/` to orphan branch `mindsbackup/<agent_id>`, every 60s. | `FCT/libs/runtime_backup/` | called "backup" |
| **host snapshot** | A provider-level VM/disk snapshot (`mngr snapshot`). Distinct from **restic backup artifact** and host_backup **consistency capture**. | `libs/mngr/imbue/mngr/cli/snapshot.py` | "snapshot" 3-way overload |
| **file sharing** | The WsgiDAV/WebDAV service at `/api/v1/files` exposing home + `/tmp`. | `apps/minds/.../webdav.py` | OK |
| **agent memory** | Claude auto-memory at `runtime/memory/` (`autoMemoryDirectory`). Distinct from **user memory** (`~/.claude/.../MEMORY.md`). | `FCT/.claude/settings.json` | format opaque |
| **event log** | The persistent on-disk append-only `events/<source>/events.jsonl` read by `mngr event`. Distinct from the in-memory SSE **event stream**. | `libs/mngr/imbue/mngr/api/events.py:54` | SSE system undocumented in style guide |
| **upstream** | The template repo a workspace derives from (`parent.toml`), pulled by `update-self`, pushed to by `submit-upstream-changes`. Standardize on "upstream" over "parent". | `FCT/parent.toml` | "parent" vs "upstream" |

### Presentation

| Canonical term | Definition | Canonical location | Status |
|---|---|---|---|
| **panel** | The atomic addressable content unit in the dockview layout (component `chat`/`iframe`/`subagent`), addressed by a type-prefixed **ref** (`chat:`, `service:`, `terminal:`, …). | `system_interface/frontend/.../DockviewWorkspace.ts` | OK |
| **group** | A set of panels sharing one pane + tab bar; exactly one active at a time. | dockview; `layout_ops.py` | OK |
| **tab** | User-facing name for a panel's tab-bar entry. UI copy only. | `manage-layout/SKILL.md` | OK |
| **layout** | The persisted dockview tree of groups/panels (`workspace_layout/layout.json`). | `DockviewWorkspace.ts:101` | `active_panel` returns a group id |
| **terminal** | An iframe panel loading a `ttyd` web shell at `/service/terminal/`. | `scripts/run_ttyd.sh`; `libs/mngr_ttyd` | two ttyd provisioning paths |
| **browser** | *Aspirational* — no dedicated browser panel exists; closest is an ad-hoc `url:<hash>` iframe navigable via `replace-url`. | (none) | listed as existing, isn't |

See `CROSS-CUTTING.md` for the overloaded terms and `DOC-CODE-DIVERGENCES.md` for the
divergence register.
