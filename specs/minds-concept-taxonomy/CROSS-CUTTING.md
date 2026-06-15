# Cross-cutting: overloaded terms and standardization decisions

This is the heart of the standardization effort. Each section below is a single English
word (or near-synonym cluster) that the codebase currently uses for **two or more
genuinely different concepts**, or **two or more terms used for one concept**. For each,
the decision is what the canonical term(s) should be and what has to change.

Ordering is roughly by blast radius.

---

## 1. `agent` — the most loaded word in the system

**Distinct concepts all called "agent":**

1. **mngr agent (primitive)** — an `AgentId`-identified process on a host. The base
   meaning. `libs/mngr/.../interfaces/agent.py:39`.
2. **Roles of an mngr agent**, distinguished only by creation template + labels, with no
   runtime type field:
   - **services agent** — `is_primary=true`, runs bootstrap/services, never runs Claude.
   - **chat agent** — `--template chat`, the conversational agent the user talks to.
   - **worker** — `--template worker`/`crystallize-worker`, a short-lived delegate.
   - **worktree agent** — `--template worktree`, an in-workspace editing agent.
3. **Claude Code subagent** — a `.claude/agents/` harness sub-agent definition. Synced by
   mngr_claude under the dir name `"agents"` (`_CLAUDE_HOME_SYNC_DIRS`), a direct
   collision inside the mngr-agent codebase.
4. **AI/coding "agent"** — colloquial for the coding assistant binary (claude/codex/agy),
   typed as `AgentTypeName`.

**Decision:**
- "**agent**" unqualified = the mngr agent primitive.
- Always qualify roles: "**services agent**", "**chat agent**", "**worker**",
  "**worktree agent**". Do not say "primary agent" (see §10) or "sub-agent" for a worker
  (see §6).
- "**Claude Code subagent**" for `.claude/agents/`. Rename the mngr_claude sync entry
  intent in comments/docs so the bare token `"agents"` is never read as mngr agents.
- "**agent type**" for `AgentTypeName` (claude/codex/antigravity/opencode/pi-coding), plus
  the name-resolution **aliases** (`agy`→antigravity, `pi`→pi-coding) registered via
  `register_agent_aliases`.

---

## 2. `provider` — factory vs instance vs LLM

**Distinct concepts:**

1. **provider backend** — stateless factory, one per backend type
   (`ProviderBackendInterface`).
2. **provider instance** — configured endpoint managing hosts (`ProviderInstanceInterface`).
3. **AI provider** (`AIProvider`) — the LLM auth/billing layer (how an agent obtains its
   Anthropic credentials). **Now a real minds enum** (`apps/minds/.../primitives.py:72`:
   `IMBUE_CLOUD`/`API_KEY`/`SUBSCRIPTION`), so the bare word `provider` is now genuinely
   overloaded between compute and LLM auth in code.

**Decision:**
- "**provider backend**" (factory) and "**provider instance**" (configured endpoint).
  Reserve bare "**provider**" for the instance in user-facing text.
- Rename `VultrProvider` → `VultrProviderInstance` (it currently reads like an instance but
  subclasses `VpsDockerProvider` and plays both roles). `vps_docker` is a shared base, not
  a usable provider — document that it registers no backend. The same base is now also
  subclassed by `aws`, `gcp`, and `ovh` instances.
- The code names the LLM-auth enum `AIProvider`, which collides with compute "provider"; in
  user-facing / cross-subsystem text prefer "**LLM auth mode**" or "AI connection" so the
  bare word "provider" keeps meaning compute (§5 of group 2).

---

## 3. `plugin` — two registries, same word, adjacent files

1. **mngr plugin** — pluggy Python extension. Disabled via `disable_plugin__extend` in
   `.mngr/settings.toml`.
2. **Claude Code plugin** — npm extension from a marketplace. Enabled via `enabledPlugins`
   in `.claude/settings.json`.

Both appear in FCT config, both change what an agent can do, and the `_CLAUDE_HOME_SYNC_DIRS`
even syncs a dir literally named `"plugins"` (the Claude Code one) from inside the
mngr-plugin package `mngr_claude`.

**Decision:** never write bare "plugin" in any cross-system context. Always
"**mngr plugin**" or "**Claude Code plugin**". "**plugin marketplace**" for
`extraKnownMarketplaces`.

---

## 4. `hook` — four unrelated mechanisms

1. **Claude Code hook** — shell command on a Claude lifecycle event (`.claude/settings.json`):
   `SessionStart`, `PreToolUse`, `Stop`, `PermissionRequest`, `Notification`, …
2. **mngr plugin hook** (pluggy) — Python `hookspec`/`hookimpl`
   (`libs/mngr/.../plugins/hookspecs.py`).
3. **git hook** — `scripts/git_hooks/post-commit`.
4. **`LifecycleHook` enum** — mngr provisioning *stages* (`INITIALIZE`, `ON_CREATE`,
   `POST_START`, …) in `primitives.py`. Worst offender: it reads like a Claude Code hook
   but is a mngr concept.

**Decision:** "**Claude Code hook**", "**mngr plugin hook**" (or "pluggy hook"),
"**git hook**". Rename the `LifecycleHook` enum to **`LifecycleStage`** ("mngr lifecycle
stage") to remove the false association with hooks.

---

## 5. `service` vs `application` — the most pervasive runtime naming split

One real concept, two terms, split by layer:

| Layer | Term used |
|---|---|
| `services.toml` entry / tmux window `svc-<name>` | **service** |
| URL path `/service/<name>/`, layout ref `service:<name>`, `ServiceName` type | **service** |
| `runtime/applications.toml`, `ApplicationEntry` model, WS event `applications_updated` | **application** |
| `app_watcher` event types `service_registered`/`service_deregistered` | **service** (for application registrations!) |

Note the two analyses (groups 3 and 8) reached *different* recommendations, which itself
shows the confusion: group 3 wanted to keep "service" (process) vs "application"
(port-registered) as a real distinction; group 8 wanted "service" everywhere.

**Decision (reconciled):** keep one underlying concept and standardize the *word* on
**service**, but keep a precise sub-distinction by adjective rather than a different noun:

- "**service**" = any `services.toml`-managed process (tmux `svc-<name>`).
- "**forwarded service**" = a service that registered a URL via `forward_port.py` (the
  subset that becomes UI-addressable). This replaces the noun "application".
- Rename for consistency (the registry is the only place "application" survives):
  `runtime/applications.toml` → `runtime/forwarded_services.toml` (or document the legacy
  name), `ApplicationEntry` → `ServiceEntry`, WS event `applications_updated` →
  `services_updated`. The `app_watcher` event types are already "service_*", so they
  become correct under this scheme.
- "**web view**" is doc-only colloquial — drop it from code vocabulary; the panel that
  hosts a forwarded service is an "**iframe panel**" (§9).

This is a larger rename; if it is not done, at minimum **document** that
`applications.toml`/`ApplicationEntry` and `service:`/`ServiceName` name the same thing.

---

## 6. `task` / `ticket` / `step` / `worker` — the work-unit cluster

"task" alone means at least five things: a `tk` ticket of `type: task`; the `task.md`
brief; the `launch-task` skill slug; a worker's unit of work; and any bounded work.

**Decision:**
- "**ticket**" = a `tk` record. "**step record**" = a `step: true` ticket (never "step
  ticket"; "step" alone OK in prose). "**regular ticket**" = non-step.
- "**worker**" = the launched delegate agent (not "sub-agent", not "background agent" in
  this context — see below). "**lead agent**" = the dispatcher. "**task brief**" =
  `task.md`. "**task dispatch**" = the act of launching a worker.
- Avoid bare "**task**" as a noun for the dispatch. `launch-task` skill slug stays
  (established) but its prose should say "worker"/"task brief".
- Terminology nuance with §1: the repo memory prefers "background agent" over "sub-agent"
  generally (because "sub-agent" is a Claude Code harness term). Within `launch-task`,
  "**worker**" is the most specific and should win; "sub-agent" in the skill's description
  frontmatter is the one concrete thing to fix.

---

## 7. `message` / `notification` / `request` / `event` / `card` — comms cluster

- "**send message**" / "**mngr message**" = inject text into agent stdin (`agent.send_message`).
- "**transcript event**" (specifically `user_message` / `assistant_message`) = a parsed
  JSONL entry. Not "message".
- "**notification**" = OS-level desktop alert (`NotificationRequest`). Also (confusingly)
  the JSONL event type string emitted to Electron — keep that as the wire type but know it
  is the same concept.
- "**permission request**" = a `RequestEvent` inbox item, resolved **GRANTED/DENIED**
  (there is no FAILED — see divergences).
- "**event**" splits into "**event log**" (on-disk JSONL, `mngr event`) vs
  "**event stream**" (in-memory SSE in `system_interface`) — see §11.
- "**card**" = UI-only rendering of an inbox item; the model is a `RequestEvent`. Call them
  "**request cards**" in UI copy only.

**Decision:** never use bare "message" in an API/field/doc without one of the qualifiers
above. "notification" stays for the OS alert.

---

## 8. `conversation` / `transcript` / `session` — and the second `session`

- "**transcript**" = the parsed event sequence. "**session**" = one Claude `<id>.jsonl`
  file. **Retire "conversation"** — it survives only in FCT frontend compat shims
  (`Conversation.ts`, `ConversationEventQueues` comment), inherited from llm-webchat.
- Second collision: **auth session**. `AccountSession` / `MultiAccountSessionStore` /
  `minds_session` cookie / SuperTokens session all say "session" but mean account auth,
  not transcript files.

**Decision:** "**transcript**" + "**session**" (Claude JSONL) on the conversation side;
on the auth side rename `AccountSession` → **`AccountProfile`/`AccountRecord`** and
`MultiAccountSessionStore` → **`AccountStore`** (they hold identity + workspace
associations, not tokens — the real tokens live in the plugin). Reserve "session" for an
actual token-bearing auth session.

---

## 9. `panel` / `tab` / `view` / `window` / `group` — layout cluster

- "**panel**" = atomic content unit (programmatic). "**group**" = pane holding panels with
  one tab bar. "**tab**" = the visual tab-bar entry (UI copy only).
- **Avoid "view"** for layout — it is already Electron's `WebContentsView`; in dockview
  JSON `views`/`activeView` is internal.
- **Avoid "window"** for layout — it is Electron's `BaseWindow`.
- "**iframe panel**" = the panel type hosting a forwarded service or URL.

**Decision:** panel / group / tab as above. Fix the `active_panel` field that actually
returns a *group* id (see divergences).

---

## 10. `primary` — opposite agents in adjacent docs

`is_primary=true` labels the **services agent** (hidden, never runs Claude). But "the
primary agent" in UX prose usually means **the main chat agent the user talks to** — the
exact opposite agent in the same workspace.

**Decision:** call the `is_primary` agent the "**services agent**" everywhere (matches the
error strings in `system_interface/server.py`). Keep the *label* `is_primary` as the wire
contract, but never narrate it as "the primary agent". If a term is needed for the user's
main chat agent, say "**the workspace's main chat agent**".

---

## 11. `event` — on-disk log vs in-memory SSE

Two unconnected systems both called "events":

- **event log** (System A): append-only `events/<source>/events.jsonl`, persistent, read
  by `mngr event`. Documented in `style_guide.md`.
- **event stream** (System B): in-memory `AgentEventQueues` SSE for live UI, with
  `BufferBehavior` replay. **Not** in the style guide; no bridge to the log.

**Decision:** "**event log**" vs "**event stream**". Document System B in the style guide.
(Also: the on-disk backup `SNAPSHOT_CREATED`/`SNAPSHOT_DELETED` event types refer to
filesystem captures, not restic snapshots — see §12.)

---

## 12. `backup` / `snapshot` — durability cluster (a 5-way tangle)

- "**workspace backup**" (today: `host_backup`) — restic, encrypted, full host_dir,
  hourly, to R2/S3.
- "**runtime checkpoint**" (today: `runtime_backup`) — git commits of `runtime/` to orphan
  branch every 60s. Calling this a "backup" overstates its guarantees.
- "**restic backup artifact**" (today: "restic snapshot") — a point-in-time artifact in the
  restic repo.
- "**host snapshot**" (today: "mngr snapshot") — provider-level VM/disk snapshot. Keep
  "snapshot" here (standard cloud term).
- "**consistency capture**" (today: host_backup internal "snapshot") — the temporary
  filesystem view fed to restic (`SnapshotTakerInterface`, `SnapshotMethod`).

**Decision:** rename the library `runtime_backup` → **`runtime_checkpoint`**; rename
host_backup's internal `Snapshot*` types → **`Capture*`** and the `SNAPSHOT_CREATED/DELETED`
event types → **`CAPTURE_CREATED/DELETED`**. Keep "host snapshot" for `mngr snapshot`. This
removes the triple "snapshot" collision and the double "backup" collision.

---

## 13. `secret` / `credential` / `token` / `key` — auth-material cluster

Four storage systems + a type wrapper all say "secret/credential/token/key":

- "**runtime secret**" = `runtime/secrets/*.env` in a container.
- "**Modal Secret**" = cloud KV bundle for deploy infra (Modal's own term).
- "**Vault secret**" = operator KV in HashiCorp Vault.
- "**service credential**" = latchkey third-party auth.
- "**account credential**" = SuperTokens session JWTs.
- "**API key**" = reserve for genuine key-based auth (Anthropic inference, Cloudflare API
  token, SuperTokens admin). `api_key: SecretStr` currently names three different things.
- "**signing key**" = cookie HMAC key (already consistent).
- `SecretStr` = a Pydantic in-memory wrapper, not a storage location.

**Decision:** adopt the qualified terms above. The most actionable fix is to stop using a
bare `api_key` field name for three semantically different keys — give each a specific name
(`cloudflare_api_token`, `supertokens_admin_key`, `anthropic_api_key`).

---

## 14. `permission` / `scope` — detent vs onboarding preference

- **detent permission / scope** = machine-enforced access control at the latchkey gateway
  (`LatchkeyPermissionsConfig`, `scope → [permission]` rules).
- **`permissions_preference`** = a free-text Q3 onboarding answer written to Claude's
  *memory* (`runtime/memory/permissions_preferences.md`). Not detent. Not enforced.

**Decision:** keep "**permission**"/"**scope**"/"**permissions rule**"/"**permissions
file**" for detent. **Rename** `permissions_preference` → `agent_autonomy_preference` (or
`agent_instruction_preference`) to kill the collision.

---

## 15. `sharing` — Cloudflare vs WebDAV

- "**workspace sharing**" = expose a service to external users via Cloudflare tunnel +
  Access (the Share modal).
- "**file sharing**" / better "**file access grant**" = WebDAV per-path local file access
  (`FileSharingGrantHandler`, `RequestType.FILE_SHARING_PERMISSION`).

Entirely different mechanisms and UX.

**Decision:** "**workspace sharing**" vs "**file access grant**". Also explicitly document
that an empty `emails` list in `enable_sharing_via_cloudflare` means *no Access policy* =
public (currently a silent behavior).

---

## 16. `remote` — git remote vs remote host

In the *same file* (`libs/mngr/.../api/git.py`): `RemoteGitContext` runs git on a **remote
host** (SSH compute), while `git push origin` targets a **git remote** (named URL).

**Decision:** never bare "remote". "**git remote**" for the VCS endpoint, "**remote host**"
for the compute machine. (Bonus: mngr's SSH push passes the URL positionally and stores no
named remote in `.git/config` — document this so operators aren't confused.)

---

## 17. `template` — create preset vs template repo

- "**create template**" = `[create_templates.<name>]` CLI preset, applied via `--template`.
- "**template repository**" = the FCT git repo cloned into a workspace.
- Sub-collision: `create_templates.main` (a preset) vs `agent_types.main` (an agent type) —
  "main" names both.

**Decision:** "**create template**" vs "**template repository**". Consider renaming the
`main` create-template (e.g. `base`/`shared`) to stop it colliding with the `main` agent
type.

---

## 18. `upstream` / `parent`

`parent.toml` says "parent"; the git remote is `upstream`; skills are `update-self` /
`submit-upstream-changes`. Three names, one relationship.

**Decision:** standardize on "**upstream**". Rename `parent.toml` → `upstream.toml` (keys
`url`, `branch`) or at least rename keys to `upstream_url`/`upstream_branch`. Keep the git
remote name `upstream`.

---

## 19. `state` / `runtime`

- "**runtime state**" = `runtime/<feature>/` persistent filesystem state.
- "**service state**" = in-memory Python (`app.state`, `BackupState`).
- plus several `*State` enums (`BackupStatusState`, etc.).

**Decision:** "**runtime state**" (filesystem) vs "**service state**" (in-memory). Note the
exceptions (`runtime/secrets/` is not user "state"; `runtime/last-restic-prune` is a file,
not a dir; `events/` lives under runtime state but is undocumented).

---

## 20. `spec` / `blueprint` / `plan`

- "**spec**" = freeform human-authored design doc in `specs/`.
- "**blueprint**" = structured generated plan in `blueprint/` (from the `blueprint` +
  `blueprint-generate` skills).
- "plan" is used as a third synonym in `blueprint-generate`'s prose, and *separately* as
  the docs-only word for the progress view (§7/§9).

**Decision:** "**spec**" vs "**blueprint**"; make `blueprint-generate` call its output a
"blueprint" consistently. Document the spec/blueprint distinction in FCT CLAUDE.md.
Keep "plan" out of code entirely (progress view is "**progress view**").

---

## Summary of recommended renames (highest value first)

| Current | Recommended | Why |
|---|---|---|
| `permissions_preference` | `agent_autonomy_preference` | collides with detent permissions |
| `runtime_backup` (lib) | `runtime_checkpoint` | it is git checkpointing, not backup |
| host_backup `Snapshot*` / `SNAPSHOT_*` | `Capture*` / `CAPTURE_*` | 3-way "snapshot" collision |
| `AccountSession` / `MultiAccountSessionStore` | `AccountProfile` / `AccountStore` | hold identity, not sessions |
| `LifecycleHook` enum | `LifecycleStage` | not a hook |
| `applications.toml` / `ApplicationEntry` / `applications_updated` | `forwarded_services.toml` / `ServiceEntry` / `services_updated` | unify service vs application |
| `active_panel` (layout_inspect) | `active_group` | it returns a group id |
| `parent.toml` | `upstream.toml` | standardize on "upstream" |
| `VultrProvider` | `VultrProviderInstance` | backend/instance naming |
| bare `api_key` fields (×3) | `cloudflare_api_token` / `supertokens_admin_key` / `anthropic_api_key` | three different keys |
| `create_templates.main` | `create_templates.base` | collides with `agent_types.main` |

Each of these is independently shippable; none requires the others.
