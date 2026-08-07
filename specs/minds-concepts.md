# Concepts that could be first-class in minds

A brainstorm of the "nouns" of the minds system: things a user (or the mind itself) might want to
list, inspect, create, destroy, share, back up, or get notified about. Drawn from the current
`apps/minds` code, `libs/mngr`, and the `forever-claude-template` workspace template.

Intentionally over-generated: recall over precision. Names are placeholders, not final.

## Concepts that exist today (in some form)

These already exist somewhere in the system -- as a file convention, a service, a skill, a CLI
command, or a UI affordance -- even if they are not yet uniformly modeled or exposed.

### Compute and runtime

- **workspaces / minds** -- the persistent agent containers themselves (labeled `workspace=<name>`)
- **templates** -- template repositories (forever-claude-template) plus mngr create templates
  (`main`, `docker`, `lima`, `vultr`, `imbue_cloud`, `worker`, `crystallize-worker`) and template
  stacking
- **launch modes / environments** -- DOCKER, LIMA, CLOUD, IMBUE_CLOUD
- **hosts** -- mngr hosts, host pools, imbue_cloud leases of pre-baked pool hosts
- **providers** -- compute backends (local, docker, lima, modal, vultr, imbue_cloud)
- **regions** -- region preference for cloud workspaces
- **services** -- `services.toml` entries reconciled into tmux windows by the bootstrap service
  manager; restart policies; the `edit-services` skill
- **applications / ports** -- services that expose a port, registered via `forward_port.py` into
  `runtime/applications.toml`, each with a local and optional global URL
- **dependencies / deferred installs** -- the `deferred-install` service, marker files, Dockerfile
  contents, vendored dependencies (`vendor/mngr`, `vendor/tk`)
- **lifecycle state** -- start/stop/destroy, container status badges, idle detection, quit-time
  shutdown prompts
- **health / liveness** -- mind liveness probes, system-interface health, recovery probe

### Agents and work

- **agents** -- worker mngr agents created via `launch-task` (separate branches, separate
  containers)
- **chats / chat agents** -- the mngr agents running out of `/mngr/code` that the user talks to
- **services agent** -- the hidden primary agent (`is_primary=true`) that runs only bootstrap and
  background services
- **tasks / delegation** -- the `launch-task` flow: worker branches (`mngr/<name>`), per-dispatch
  runtime dirs, merge gates via main
- **tickets** -- `tk` records in `runtime/tickets/`, cross-agent work units with assignment
- **steps** -- turn-bound `tk create --step` progress records that drive the chat progress view
- **reviews** -- worker code review (`.reviewer` settings, review in worker template)
- **skills** -- `.agents/skills/` directories with SKILL.md, `skills-lock.json`
- **skill lifecycle** -- crystallize-task, heal-skill, update-skill, do-something-new; crystallized
  metadata; scenario testing by crystallize workers
- **shared assets** -- `.agents/shared/` scripts and references consumed by multiple skills
- **scripts and hooks** -- `scripts/`, Claude hooks (stop hooks, pretool checks, status line), git
  hooks (auto-push post-commit)
- **plugins** -- mngr plugins; Claude Code plugins and marketplaces (shared via the common
  `CLAUDE_CONFIG_DIR`)

### Conversation and communication

- **chats / conversations / transcripts** -- Claude session JSONL files rendered by the
  system_interface chat view; `mngr transcript`
- **messages** -- `mngr message` delivery into agents; user-facing replies
- **channels** -- the `send-user-message` dispatch abstraction over concrete channels (telegram
  today), `read-telegram-history`, the telegram bot service
- **notifications** -- desktop notifications, the `/api/v1/.../notifications` endpoint every agent
  may call
- **inbox** -- the desktop client inbox drawer of pending cards (permission requests today)
- **progress views** -- the rendered timeline of steps/tickets per turn

### Security, identity, and access

- **secrets** -- `runtime/secrets/` (tunnel token, `restic.env`), host env files, pass-env
- **permissions** -- latchkey gateway, detent scope/permission schemas, per-agent
  `latchkey_permissions.json`, deny-all baselines
- **permission requests / approvals** -- request events in `events/requests/events.jsonl`,
  approve/deny dialogs, granted/denied/failed outcomes
- **credentials** -- latchkey-managed third-party service credentials, browser auth flows,
  set-credentials flows
- **AI providers** -- how the agent gets Anthropic credentials (imbue_cloud LiteLLM key, raw API
  key, subscription login)
- **accounts** -- imbue cloud accounts (used for LiteLLM keys, R2 buckets)
- **users / identity / sessions** -- one-time login codes, signed cookies, SuperTokens sessions,
  desktop-client API keys
- **sharing / global access** -- Cloudflare tunnels, global URLs per service, Cloudflare Access
  policies, the Share modal and per-service global toggle
- **onboarding / data preferences** -- the convenience/privacy/control choice and the local scan
- **labels** -- mngr labels (`workspace=<name>`, `is_primary`) used for discovery and protection

### Data and durability

- **backups** -- restic host backups (hourly, encrypted, R2), `runtime/` git backups
  (`mindsbackup/<agent-id>` orphan branch), backup providers, encryption/recovery keys
  (master password vs no password), backup status and export
- **snapshots** -- `mngr snapshot` of host state; restic snapshots
- **versions** -- git history of the workspace code; commits, branches, worktrees
- **upstream / parent** -- `parent.toml`, `update-self` (pull) and `submit-upstream-changes` (push)
- **events** -- append-only `events/<source>/events.jsonl` streams (service events, request
  events), `mngr event`
- **logs** -- agent logs, tmux pane output, service logs, push logs
- **memories** -- `runtime/memory/` (Claude auto-memory), backed up with runtime
- **runtime state** -- the `runtime/<feature>/` convention for all persistent feature state
- **files / file sharing** -- the WebDAV mount bridging the user's local filesystem and the
  workspace
- **specs / blueprints** -- the blueprint skill, `blueprint/` plan documents, `specs/` design docs
- **changelogs** -- per-project changelog entries and consolidated changelogs
- **purpose** -- PURPOSE.md, the agent-specific statement of what this mind is for

### Presentation

- **layout / tabs / panels** -- the dockview layout, `manage-layout` skill, `layout.py` (open,
  split, focus, rename, maximize...)
- **web views / UI services** -- per-service tabs served at `/service/<name>/`, the
  `build-web-service` skill
- **terminals** -- ttyd terminal tabs
- **tests** -- unit/integration/acceptance/release tests, ratchets, deployment tests, the e2e
  workspace runner

## Concepts we might want someday

Not first-class today (or only hinted at in guidelines, future_specs, or commented-out docs).

### External data and integration

- **connectors** -- configured syncs of remote data sources (Slack, Gmail, Google Drive, Notion,
  Linear, calendars), with sync state, cursors, and freshness
- **documents / knowledge base** -- curated reference material the mind maintains, distinct from
  raw memory
- **datasets / tables** -- structured data storage (the spreadsheet-like things a mind accumulates)
- **indexes / search** -- unified search across transcripts, memory, files, tickets, events
- **embeddings / vector stores** -- semantic retrieval over the above
- **caches** -- first-class cached derivations of remote data with invalidation
- **emails / calendar events** -- as native objects rather than connector payloads
- **feeds / watch targets** -- external state the mind watches (a repo, a dashboard, a mailbox)

### Work orchestration

- **tasks** -- the logically running bits of work as durable first-class objects (richer than
  tickets: state machine, owner, budget, artifacts, lineage)
- **schedules / crons** -- recurring agent runs (`mngr schedule` already exists as an mngr plugin
  but is not surfaced in minds)
- **triggers / automations** -- event-to-action rules (when X happens, do Y)
- **watchers / monitors** -- standing observations that alert or wake the mind on change
- **reminders** -- time-based nudges for the user or the mind
- **queues** -- ordered backlogs of pending work with retries and dead-lettering
- **workflows / pipelines** -- multi-step orchestrations spanning several agents/skills
- **goals / objectives** -- long-horizon intents that outlive individual tasks; what the mind works
  toward when idle
- **plans** -- durable plans (blueprints promoted to first-class, linked to goals and tasks)
- **background jobs vs interactive turns** -- explicit separation of attention
- **priorities / attention** -- what the mind chooses to do next and why

### Outputs and presentation

- **artifacts / outputs** -- generated deliverables (reports, files, PRs, images) with provenance
  back to the task and raw data that produced them
- **apps** -- user-facing mini-apps as installable/updatable/removable units (beyond ad-hoc web
  services)
- **dashboards / views / pages** -- saved renderings over data, with the raw records preserved and
  surfaced
- **reports / digests** -- recurring summaries (daily briefing, weekly review)
- **forms** -- structured input requests to the user
- **subscriptions** -- recurring deliverables the user has opted into

### Learning and quality

- **feedback** -- binary plus free-form signals on anything the mind produces, routed through judge
  pipelines (currently only a CLAUDE.md guideline)
- **evaluations / benchmarks** -- regression scenarios for skills and behaviors; skill quality
  scores
- **experiments** -- A/B variants of skills/prompts with outcome tracking
- **incidents** -- things that went wrong, with postmortems feeding heal-skill and memory
- **lessons / corrections** -- distilled user corrections as a typed memory category

### Trust, governance, and economics

- **approvals** -- generalized beyond latchkey: spending money, sending external messages, deleting
  data, deploying
- **policies / guardrails** -- declarative rules (network allowlists, data-handling, what requires
  approval), checked at runtime
- **audit log / activity timeline** -- a complete, queryable record of what the mind did and why
  (provenance)
- **budgets / quotas** -- token, compute, and API spend limits per task/agent/workspace
- **usage / cost metering** -- per-workspace and per-task cost attribution and billing
- **trust levels / quarantine** -- tainting of untrusted inbound data and restrictions on what it
  can influence
- **capabilities / entitlements** -- what features a given mind has unlocked or been granted
- **rate limits** -- first-class throttles on outbound calls and channels
- **wallets / payments** -- money the mind can spend, with budgets and receipts

### People and other minds

- **contacts / people** -- humans the mind knows, with per-person channels and permissions
- **guests / collaborators** -- sharing access to a service, view, or whole workspace with someone
  else
- **personas / identities** -- the mind's own outward identities (its email address, phone number,
  social accounts, signing keys)
- **devices** -- the user's phone/laptop as registered endpoints (push notifications, local
  access)
- **mind-to-mind communication** -- cross-workspace conversations, delegation, and federation
- **fleets / replicas** -- groups of minds managed together
- **marketplace** -- discovery and installation of skills, templates, connectors, and apps

### Platform and operations

- **models** -- model registry, per-task model selection, fallbacks, fast/slow tiers
- **tools / MCP servers** -- a registry of available tools beyond skills
- **releases / updates** -- versioned releases of the mind itself with rollback (beyond
  update-self)
- **restore points** -- user-facing snapshot/restore as a single concept unifying restic, git, and
  mngr snapshots
- **migrations** -- moving a mind between hosts, providers, or regions as a tracked operation
- **forks / lineage** -- cloning a mind and tracking ancestry between minds
- **archives / retirement** -- mothballing a mind cheaply with full export/import portability
- **sandboxes / staging** -- trying a change to the mind in a copy before applying it live
- **alerts / SLOs** -- health checks with user-visible alerting when something degrades
- **maintenance windows** -- scheduled self-maintenance (updates, gc, backup verification)
- **domains** -- custom domains for globally shared services
- **time / history** -- a browsable timeline of what the mind did each day (diary view over the
  audit log)
