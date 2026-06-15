# User-facing concepts (the primary taxonomy)

This is the top-down view, and the intended payoff of the whole analysis. Instead of
starting from code terms, it starts from **the things a user perceives, chooses, or acts
on** — and for each, names the lower-level technical terms and ambiguities that roll up
into it.

The premise (from the request): *if we can settle the canonical term and definition for
each user-facing concept, most of the technical-terminology ambiguities become solvable*,
because the technical sprawl is largely the symptom of an unnamed user-facing concept. The
clearest example: there is no canonical term for **"where does my mind run?"**, and as a
result the code has `provider backend`, `provider instance`, `launch mode`, `region`,
`host`, and `host pool` all partially covering it with no single owner.

## How each entry is structured

- **User's question** — what the user is actually asking/doing.
- **Canonical term** — ❌ none yet (with candidates) / ⚠ contested / ✅ settled.
- **Working definition** — the user-facing definition, as it'd appear in-app.
- **Rolls up** — the lower-level technical pieces (with current code term) that implement it.
- **Why it's hard** — the ambiguities: competing terms, overloads, blurry boundaries with
  neighboring concepts, doc/code gaps, behavior gaps.
- **To resolve** — the decision(s) that would settle it.

Cross-references: technical canonical terms in `README.md`; overloaded-word decisions in
`CROSS-CUTTING.md`; doc/code mismatches in `DOC-CODE-DIVERGENCES.md`; full per-concept
evidence in `groups/`.

---

# A. The mind & where it lives

## A1. The mind (the thing you have)

- **User's question:** "What is the thing I created and interact with?"
- **Canonical term:** ⚠⚠ **none settled** — candidates: **mind** (brand fit),
  **workspace** (code term), **assistant** / **agent** (mental model). This is the keystone
  decision; almost everything else inherits from it.
- **Working definition:** A persistent AI assistant of yours that lives in its own
  computer/container, remembers your work, and can act on your behalf.
- **Rolls up:**
  - `workspace` — the code-canonical term: a host + its hidden services agent, identified
    by that agent's `AgentId`, discovered by labels `workspace=<host_name>` + `is_primary`.
  - `mind` — appears only in UI copy and `MindLiveness`; **no `Mind` class exists**.
  - `agent` (the mngr primitive) — what a workspace is actually *made of* (a services agent
    + chat agent(s)).
- **Why it's hard:**
  - The product is "Minds" but the code is "workspace" — two names for the top thing.
  - A workspace is internally *several* agents; the user thinks of it as one thing.
  - Sub-decision: is the thing you open-and-chat-with the same as the mind, or a part of it?
    (Code supports many chat agents per workspace.) "Open your mind" vs "open a chat in your
    mind" are different products.
  - "agent" is so overloaded (see A-roles, G, etc.) that reusing it as the user word is
    risky.
- **To resolve:** (1) the top-level noun; (2) whether multiple chats per mind is a
  user-visible concept; (3) whether to expose the word "agent" to users at all.

## A2. Where my mind runs

- **User's question:** "Is my mind running on my own computer or in the cloud — and where?"
- **Canonical term:** ❌ **none** — there is no single term for this concept anywhere.
  Candidates: **location** / **runs on** / **environment**. (The control is currently
  called "launch mode," which is developer-facing.)
- **Working definition:** Whether your mind runs locally (on this computer) or in the cloud,
  and in which region — affecting cost, speed, and whether you can start/stop it yourself.
- **Rolls up:**
  - `provider backend` — stateless factory per backend type (local/docker/lima/modal/
    vultr/ovh/aws/gcp/imbue_cloud).
  - `provider instance` — the configured endpoint a mind was created with.
  - `launch mode` (`LaunchMode`: DOCKER/LIMA/VULTR/AWS/IMBUE_CLOUD) — the minds-level user
    choice that maps to a provider + templates.
  - `region` — provider-specific datacenter string.
  - `host` — the actual machine the mind runs on.
  - `host pool` / leased host — pre-baked machines imbue_cloud leases (a speed
    optimization).
- **Why it's hard:**
  - Five+ code terms, no shared user concept: "provider" used for both the factory and the
    configured instance; `VultrProvider` plays both roles; "AI provider" (a different thing,
    see G2) muddies the word further.
  - The user-facing control exposes raw enum labels (DOCKER/LIMA/VULTR/AWS/IMBUE_CLOUD) a
    non-technical user can't parse.
  - `region` has five incompatible formats across providers
    (`US-EAST-VA`/`ewr`/`us-east`/`us-east-1`/`us-west1-a` zone) and no shared type.
  - Boundary blur with **A3 (status)** — "where it runs" also determines *whether you can
    start/stop it* (only docker/lima are "shutdown-capable").
- **To resolve:** name the concept; give the launch-mode options friendly location/plan
  names; decide whether region is exposed at all.

## A3. Is my mind awake / working?

- **User's question:** "Is my mind on? Asleep? Broken? Can I talk to it right now?"
- **Canonical term:** ⚠ **partly settled** — the badge is "status"; the *states* need
  user words (esp. asleep vs stopped).
- **Working definition:** Whether your mind is awake and responsive, asleep (idle, wakes on
  demand), off, or having a problem.
- **Rolls up (four distinct mechanisms answering subtly different questions):**
  - `HostState` — container/VM lifecycle (RUNNING/STOPPED/PAUSED/CRASHED/…).
  - `MindLiveness` — minds' RUNNING/STOPPED/UNKNOWN rollup of `HostState` (docker/lima
    only).
  - `AgentLifecycleState` — RUNNING/WAITING/DONE/… (incl. "the mind is waiting for *you*").
  - `system interface health` (`AgentHealth`: HEALTHY/STUCK/RESTARTING/RESTART_FAILED) +
    `recovery probe` (`DispatchTier`) — whether the in-container UI server responds and *why
    not*.
- **Why it's hard:**
  - Four overlapping "is it working" signals at different layers (container up? agent
    process alive? web server responding? why is it broken?), all of which collapse, from
    the user's view, into "is my mind working?"
  - **PAUSED vs STOPPED** is the confusing pair — both look "off" but one wakes instantly
    and one was deliberately turned off. No user-facing words distinguish them.
  - `WAITING` (mind needs your input) and `DONE` are user-relevant but buried in an
    internal enum.
  - Inconsistent code: `AgentHealth` uses a different enum base than `MindLiveness`;
    `_OFFLINE_HOST_STATES` is duplicated with two different types.
  - The recovery experience ("your mind isn't responding, recovering…") *is* user-facing but
    is assembled from internal probes.
- **To resolve:** the set of user-facing states and their words (esp. asleep/idle vs
  off/stopped vs error); whether "waiting for you" is its own surfaced state.

---

# B. Working with my mind

## B1. Talking to my mind (conversations)

- **User's question:** "How do I have a conversation with my mind, and where's the history?"
- **Canonical term:** ⚠⚠ **contested** — **chat / conversation** (a thread) vs
  **agent / assistant** (a persistent worker). Different products.
- **Working definition:** A conversation thread with your mind, with full history.
- **Rolls up:**
  - `chat agent` — an agent created `--template chat`; what the user talks to. **No runtime
    type marker** distinguishes it post-creation.
  - `transcript` — the parsed event sequence (`user_message`/`assistant_message`/
    `tool_result`).
  - `session` — one Claude `<id>.jsonl` file; a transcript spans several.
  - `conversation` — a *legacy* compat term (frontend `Conversation.ts`), inherited from
    llm-webchat.
  - `send message` (`mngr message`) — injecting text into the agent's stdin.
- **Why it's hard:**
  - The code is itself torn: a `Conversation` shim wraps the agent model. Is the left-rail
    item a disposable *thread* or a persistent *assistant*? "New Chat" currently spawns a
    whole agent.
  - "session" collides with **D5 (auth session)** — two unrelated meanings.
  - "message" is triple-overloaded (stdin injection / transcript event / notification body).
  - "transcript" is jargon; users would expect "conversation"/"history" — but that word is
    deprecated in code.
- **To resolve:** are users starting *conversations* or spawning *assistants*?; the
  user-facing word for the history (conversation/history, not transcript).

## B2. What my mind is doing right now (progress)

- **User's question:** "What is my mind working on this very moment?"
- **Canonical term:** ⚠⚠ **contested** — **plan** vs **steps / progress**. Also collides
  with build "plans" (F-blueprints).
- **Working definition:** A live checklist of the steps your mind is taking this turn, each
  with a title and a result.
- **Rolls up:**
  - `progress view` / `ProgressBlock` / "timeline" / "sections" — the code terms for the UI.
  - `step record` — a `step: true` ticket; the items in the timeline.
  - "plan" — used **only in docs**, never in code.
- **Why it's hard:**
  - The word "plan" reads well *before* work ("here's my plan") but oddly *after* (a "plan"
    of done things); the timeline is built live, not pre-committed, so "steps/progress" is
    more honest.
  - "step" is itself overloaded (a step record / a numbered step in a skill / the
    frontmatter field).
  - Title/summary of each step are literally user-facing copy, so the naming matters.
  - "plan" double-booked with design "blueprints/plans" (F).
- **To resolve:** "plan" vs "steps/progress" for the live timeline, and de-collide from
  build plans.

## B3. Tasks & to-dos

- **User's question:** "What work is queued/tracked for my mind (and by me)?"
- **Canonical term:** ⚠ **contested** — **tasks** / **to-dos** vs **tickets**; "task" is
  badly overloaded.
- **Working definition:** Trackable units of work your mind keeps a list of.
- **Rolls up:**
  - `ticket` (the `tk` CLI; `TicketState`) — the work-item record.
  - `step record` — a turn-bound subtype (see B2).
  - `task brief` (`task.md`) — instructions handed to a helper (see B4).
  - `type: task` — a `tk` ticket type.
- **Why it's hard:**
  - **"task" means five things**: a `tk` ticket type, the `task.md` brief, the `launch-task`
    skill slug, a worker's unit of work, and any bounded work.
  - `TicketState` (Python) parses only a subset of the `tk` frontmatter schema (`deps`,
    `links`, `type`, `priority`, `tags` are invisible) — so "ticket" means slightly
    different things to the writer (`tk`) and reader (Python).
  - "ticket" is support-desk flavored; a personal-assistant product might want "to-dos."
  - Overlaps B2 (steps are tickets) and B4 (task briefs).
- **To resolve:** whether tickets are user-visible at all and under what word; kill bare
  "task."

## B4. Background helpers (delegation)

- **User's question:** "Can my mind hand off work to run in the background while I keep
  going?"
- **Canonical term:** ⚠ **contested** — **helper** / **background task** / **worker** /
  **sub-agent** / **background agent**.
- **Working definition:** A short-lived helper your mind spins up to do one task in the
  background, reporting back when done.
- **Rolls up:**
  - `worker` — the launched delegate agent (`launch-task`); works on its own `mngr/<name>`
    branch.
  - `lead agent` — the dispatcher.
  - `task brief` (`task.md`) — the instructions.
  - merge gates / report file — how results come back.
- **Why it's hard:**
  - Three live terms for the same entity: the skill's own SKILL.md says "sub-agent" in its
    description but "worker" in its body; repo memory prefers "background agent"; "sub-agent"
    is also a Claude Code harness term (collision).
  - Boundary blur with **A1** (a helper is also an agent) and **B3** (it does a "task").
- **To resolve:** one user word for the helper; de-collide from Claude Code sub-agents and
  from "task."

---

# C. What my mind can do & knows

## C1. Abilities (skills & tools)

- **User's question:** "What can my mind actually do, and how do I add more?"
- **Canonical term:** ⚠ **contested** — **skill** vs **command** vs **recipe**;
  **tool** / **ability** for the built-ins.
- **Working definition:** The things your mind knows how to do — built-in abilities plus
  saved how-tos you (or it) add.
- **Rolls up:**
  - `skill` — a `SKILL.md` invoked as `/<name>`.
  - `tool` (Claude Code built-ins: Read/Edit/Bash/…) vs `MCP server`/`MCP tool` (external).
  - `mngr plugin` and `Claude Code plugin` — two different extension systems.
  - `hook` — four different mechanisms (Claude Code hook / pluggy hook / git hook /
    `LifecycleHook`).
- **Why it's hard:**
  - A skill is invoked like a `/command`, so "skill" vs "command" is a real fork (it shapes
    the "minds learn" story, see C2).
  - "plugin" names two entirely separate registries (pluggy Python vs npm), often in
    adjacent config files.
  - "hook" names four unrelated things; `LifecycleHook` isn't even a hook.
  - "tool" used for built-ins, MCP functions, and colloquially the mngr CLI.
  - "MCP" is pure jargon that must never reach users.
- **To resolve:** the user word for a saved how-to (skill/command/recipe); a single
  "add-ons/abilities" umbrella that hides the plugin/hook/MCP machinery.

## C2. Learning & improving over time

- **User's question:** "Does my mind get better at things it does repeatedly?"
- **Canonical term:** ⚠ mostly internal today, but the *story* is user-facing — candidates
  **learn / crystallize / save a skill**.
- **Working definition:** When your mind does something worth repeating, it can save it as a
  reusable skill — and fix or update skills that go wrong.
- **Rolls up:**
  - skill lifecycle ops: `do-something-new`, `crystallize-task`, `heal-skill`,
    `update-skill`.
  - `crystallized` vs `hand-authored` skill (`metadata.crystallized: true`).
  - scenario testing (ephemeral) vs fixture tests (on disk).
- **Why it's hard:**
  - "crystallize" is internal/evocative jargon; the user-facing verb is undecided
    (learn? save? remember?).
  - The crystallized/hand-authored split is a provenance detail with no runtime effect:
    `scripts/run.py` is optional even for crystallized skills (validated only if present),
    so the split gates nothing structural.
  - Overlaps C1 (a learned thing is a skill) and C3 (learning ≈ a kind of memory).
- **To resolve:** the user-facing verb/story for learning; whether crystallized-ness is ever
  shown.

## C3. Memory

- **User's question:** "What does my mind remember about me and my work?"
- **Canonical term:** ✅ **memory** (good, user-facing) — ⚠ one unified Memory vs two.
- **Working definition:** What your mind remembers across conversations about you and your
  work.
- **Rolls up:**
  - `agent memory` — `runtime/memory/` (Claude `autoMemoryDirectory`), per-mind.
  - `user memory` — `~/.claude/.../MEMORY.md`, cross-session, *not* backed up by the mind's
    backups.
- **Why it's hard:**
  - Two memories (per-mind vs your global) that users would expect to be one.
  - The on-disk format is controlled by Claude Code internals (opaque), and recovery comes
    from two different backup timelines (see F1).
- **To resolve:** present one "Memory" or two; clarify what's backed up.

---

# D. Control & trust

## D1. Approvals & requests (things my mind asks me)

- **User's question:** "When my mind wants to do something sensitive, how does it ask me?"
- **Canonical term:** ⚠ **contested** — the queue: **Inbox** vs **Approvals** vs
  **Requests**; the item: **request** vs **approval** vs **ask**.
- **Working definition:** A place where your mind's requests for permission wait, so you can
  allow or deny them.
- **Rolls up:**
  - `inbox` (`RequestInbox`) — event-sourced queue of pending requests.
  - `permission request` (`RequestEvent` + subtypes: permissions / latchkey / file-sharing).
  - outcome `RequestStatus`: GRANTED / DENIED.
  - "card" — the UI rendering of an item.
- **Why it's hard:**
  - "inbox" promises a general queue but holds *only* permission requests today.
  - The concepts doc and a docstring claim a "failed" outcome that **doesn't exist** in code
    (only GRANTED/DENIED) — see divergences.
  - "card" (UI) vs `RequestEvent` (model) for the same thing.
  - request flow recently moved from JSONL files to a gateway stream; a docstring is stale.
- **To resolve:** name the queue and the item; allow/deny wording; whether the inbox is
  approvals-only or a general inbox.

## D2. Notifications

- **User's question:** "How does my mind alert me when I'm not looking?"
- **Canonical term:** ✅ **notification** (settled).
- **Working definition:** An alert from your mind (desktop/OS), optionally clickable.
- **Rolls up:** `NotificationRequest` (message/title/urgency/url); Electron/macOS/tkinter
  channels.
- **Why it's hard:** mostly fine. Minor: "notification" is also the JSONL event type string
  to Electron; the click-`url` only works in the Electron channel. Boundary with D1 (a
  request can also notify).
- **To resolve:** essentially settled.

## D3. What my mind is allowed to do (permissions & autonomy)

- **User's question:** "What is my mind allowed to access, and how much can it do without
  asking me?"
- **Canonical term:** ⚠ **two concepts wrongly sharing one word**: enforced
  **permissions / access** vs **autonomy** (how freely it acts).
- **Working definition:** (a) Which services/data your mind may access; (b) how much freedom
  it has to act before asking you.
- **Rolls up:**
  - detent `permission` / `scope` / `permissions rule` / `permissions file` — machine-
    enforced access control at the latchkey gateway; deny-all baselines.
  - `permissions_preference` — a **free-text** onboarding answer written to the mind's
    *memory*; not enforced.
- **Why it's hard:**
  - "permissions" names both the enforced access rules and the free-text autonomy preference,
    side by side in code.
  - "detent" (the underlying framework) and its double-`any` wildcard are unexplained.
  - **Doc/code divergence:** permissions are keyed by `host_id`, not `agent_id` as docs say,
    so agents on one host *share* a permissions file (possible isolation gap) — see
    divergences.
- **To resolve:** split the words — "permissions/access" (enforced) vs "autonomy" (the
  preference); decide whether per-host sharing is intended.

## D4. Connected services (credentials)

- **User's question:** "How do I let my mind use my Slack/Google/GitHub on my behalf?"
- **Canonical term:** ⚠ **contested** — **connection** / **connected account** /
  **integration** vs **credential** (a security word).
- **Working definition:** Third-party accounts you connect so your mind can act in them for
  you.
- **Rolls up:**
  - `service credential` (latchkey; `CredentialStatus`: missing/valid/invalid/unknown).
  - browser-auth flow vs set-credentials flow.
- **Why it's hard:**
  - "credential" is overloaded: latchkey service creds vs Claude's own auth vs SuperTokens
    session vs "credentials" = email+password at sign-in.
  - Collides with **D5** ("account" = your login *and* your connected accounts).
  - "credential" is a security word, not a consumer word.
- **To resolve:** the user word ("connection"/"connected account"); separate it cleanly from
  your own account.

## D5. My account & sign-in

- **User's question:** "What's my Minds account and how do I sign in?"
- **Canonical term:** ✅ **account** (user-facing) — but ⚠ collides with "connected
  accounts" (D4) and the misnamed `AccountSession`.
- **Working definition:** Your Minds identity (email/login), which your minds are associated
  with.
- **Rolls up:**
  - `account` / `AccountSession` (identity + workspace associations — *not* a session).
  - SuperTokens session tokens (the real auth `session`), `minds_session` cookie.
  - imbue cloud account = LiteLLM key + R2 bucket.
- **Why it's hard:**
  - "session" overloaded four ways (auth session / `AccountSession` / cookie / Claude
    transcript session in B1).
  - `AccountSession`/`MultiAccountSessionStore` hold identity, not sessions — misnamed.
  - "account" = your login *and* third-party connected accounts (D4).
- **To resolve:** reserve "account" for the user's login; rename the misleading
  `*Session` types.

## D6. Sharing & file access

- **User's question:** "How do I let other people use my mind's apps, and how do I let my
  mind into my files?"
- **Canonical term:** ⚠ **two opposite concepts sharing the word "share"** —
  **Share** (out: give others a link) vs **File access** (in: let the mind read your files).
- **Working definition:** (a) Give someone a link to one of your mind's apps; (b) grant your
  mind access to specific files on your computer.
- **Rolls up:**
  - `workspace sharing` — Cloudflare tunnel + Access (email allowlist); the Share modal;
    global URLs.
  - `file sharing` / file access grant — WebDAV (`/api/v1/files`); `FileSharingGrantHandler`.
- **Why it's hard:**
  - Both are "sharing" but point opposite directions (out vs in) with completely different
    mechanisms and UX.
  - An **empty** email allowlist silently means *public* (no Access policy) — undocumented
    behavior.
  - Overlaps F4 (files) and A2 (global URL = how others reach a cloud mind).
- **To resolve:** "Share" only for the outward link; a distinct name for inward file access;
  surface the public-when-empty behavior.

---

# E. The workspace surface (what I see and open)

## E1. My mind's apps (running services)

- **User's question:** "When my mind builds or starts something I can open, what is that?"
- **Canonical term:** ⚠ **contested** — **app** (natural) vs **service** / **application**
  (dev words) vs **web view** (meaningless to users).
- **Working definition:** An app your mind is running that you can open in a tab — like a
  website it built.
- **Rolls up:**
  - `service` (services.toml process) vs `forwarded service`/`application`
    (`applications.toml`, `ApplicationEntry`) — the same thing, split by layer.
  - URL `/service/<name>/`, ref `service:<name>`, `ServiceName`.
  - port forwarding (`forward_port.py`), `app-watcher`, service events.
- **Why it's hard:**
  - The single most pervasive code split: URL says "service," registry says "application,"
    events say "service_registered" for application registrations. Even the two analyses
    disagreed on the fix.
  - "web view" is doc-only colloquial; no `WebView` type exists.
  - Boundary with E2 (an app is shown in a *tab/panel*) and A2 (a *forwarded* service can be
    shared globally, D6).
- **To resolve:** the user word ("app"); unify service/application in code (rename registry
  + model + events).

## E2. Tabs, layout, terminals, browser (the window)

- **User's question:** "What are these tabs, and how do I arrange them / open a terminal /
  open a web page?"
- **Canonical term:** ✅ **tab** (settled, user-facing); **layout** (mostly settled);
  **terminal** (⚠ vs "command line"); **browser** (⚠ aspirational).
- **Working definition:** Your mind's window is a set of arrangeable tabs — chats, apps,
  terminals, and web pages.
- **Rolls up:**
  - `panel` (atomic unit) / `group` (pane with a tab bar) / `tab` (the visual entry) /
    `layout` (the saved tree) — dockview.
  - `terminal` — an iframe panel loading a `ttyd` shell.
  - `browser` — *not implemented*; closest is an ad-hoc `url:<hash>` iframe.
- **Why it's hard:**
  - "panel/tab/view/window/group" — five terms; "view" collides with Electron
    `WebContentsView`, "window" with `BaseWindow`. (Users only need "tab.")
  - `layout_inspect.active_panel` actually returns a *group* id — naming bug (divergence).
  - **"browser" is listed as existing but isn't built** (divergence) — don't promise it.
  - Two parallel ttyd provisioning paths (random vs fixed port).
- **To resolve:** keep "tab" for users; fix the `active_panel` misnomer; mark browser as
  future; "command line" vs "terminal" for non-technical users.

---

# F. Durability & history

## F1. Backups & restore

- **User's question:** "Is my mind backed up, and can I restore it?"
- **Canonical term:** ⚠ **one user concept, three mechanisms** — should be a single
  **Backup**; the mechanisms must be hidden.
- **Working definition:** A saved copy of your mind you can restore from.
- **Rolls up:**
  - `workspace backup` (host_backup) — encrypted restic backup of the whole host, hourly, to
    R2/S3.
  - `runtime checkpoint` (runtime_backup) — git commits of `runtime/` every 60s to an orphan
    branch.
  - `host snapshot` (`mngr snapshot`) — provider VM snapshot.
  - `restic backup artifact` ("restic snapshot") + `consistency capture` (host_backup
    internal "snapshot") + backup export.
- **Why it's hard:**
  - **"backup" and "snapshot" are each overloaded** (backup = restic *or* git; snapshot =
    restic artifact *or* VM snapshot *or* internal filesystem capture). Five durability words
    for what users should see as one "Backup."
  - "runtime backup" overstates its guarantees (it's git checkpointing).
  - Master backup password is shared across all workspaces (undocumented design decision).
- **To resolve:** define what the user's "Backup"/"Restore" maps to (almost certainly the
  encrypted backup); hide checkpoint/snapshot; rename internal `Snapshot*` → `Capture*` and
  `runtime_backup` → `runtime_checkpoint`.

## F2. Versions & history of my mind's work

- **User's question:** "Can I see and roll back the changes my mind made?"
- **Canonical term:** ⚠ **history / versions** — no user surface exists today.
- **Working definition:** Every past version of your mind's code/work, viewable and
  restorable.
- **Rolls up:** git history of the workspace repo (`agent branch`, commits, worktrees,
  tags); runtime checkpoints (orphan branch).
- **Why it's hard:**
  - No `mngr version`/`history` command or UI exists — the concept is in `Minds_concepts.md`
    but unimplemented as a user feature.
  - Two version timelines (code branch vs runtime orphan branch) with different granularity.
  - Overlaps F1 (restore) and F3 (activity).
- **To resolve:** decide whether to surface workspace history at all, and under what word.

## F3. Activity log (what my mind did)

- **User's question:** "What has my mind been doing, and why?"
- **Canonical term:** ⚠ **Activity / History** (best) — not "logs"/"events" (jargon). This
  is also the seed of the future audit-log/diary concept.
- **Working definition:** A record of what your mind did and when.
- **Rolls up:**
  - `event log` (on-disk `events/<source>/events.jsonl`, `mngr event`) vs `event stream`
    (in-memory SSE) — two unconnected systems both called "events."
  - service logs (`/tmp/*.log`), tmux output, transcripts.
- **Why it's hard:**
  - "event" = persistent log *and* live SSE stream, with no bridge; only the on-disk one is
    in the style guide.
  - "log" spans structured (jsonl) and unstructured (`*.log`); some logs are ephemeral, some
    backed up — undocumented which.
  - Overlaps B1 (transcripts), F2 (history), and the future audit-log concept.
- **To resolve:** pick "Activity" as the user word now (it scales to the audit-log future);
  document the two event systems distinctly.

## F4. Files

- **User's question:** "How do I get files in and out of my mind?"
- **Canonical term:** ✅ **files / file sharing** (clear) — but see D6 (name must not
  collide with "Share").
- **Working definition:** Read/write access to files between your computer and your mind.
- **Rolls up:** WebDAV service at `/api/v1/files` (home + `/tmp`); the `file-sharing` skill.
- **Why it's hard:** the URL mirrors the OS filesystem path (unusual); served roots are
  container-relative; "file sharing" collides with workspace "sharing" (D6).
- **To resolve:** mostly fine; resolve the name overlap with D6.

---

# G. AI & cost

## G1. Which AI brain (model)

- **User's question:** "Which AI is my mind using, and can I make it smarter/faster?"
- **Canonical term:** ⚠ **model** — but expose **tiers** (capable/balanced/fast), not raw
  IDs.
- **Working definition:** The AI model your mind thinks with — smarter ones are slower and
  cost more.
- **Rolls up:** `model alias` (`opus[1m]`) → `concrete model ID` (`claude-opus-4-8`); model
  tiers (opus/sonnet/haiku); `fastMode`; the LiteLLM proxy as de-facto registry.
- **Why it's hard:**
  - `opus[1m]` and `claude-opus-4-8` are jargon; users need tiers.
  - No typed model registry; the proxy YAML must be hand-synced with Modal (no enforcement).
  - Boundary with G2 (which model vs how you pay/connect).
- **To resolve:** tier-based labels vs named models; whether per-task model choice is
  exposed.

## G2. AI connection & billing

- **User's question:** "How does my mind connect to and pay for its AI?"
- **Canonical term:** ⚠ **AI connection / billing** — never "AI provider."
- **Working definition:** How your mind authenticates to and pays for the AI — included
  credits, your own API key, or your Claude subscription.
- **Rolls up:** the three `LLM auth mode`s (LiteLLM virtual key / raw `ANTHROPIC_API_KEY` /
  OAuth subscription); imbue cloud account (LiteLLM key, R2).
- **Why it's hard:**
  - **Now typed in minds, still env-driven downstream**: the mode is the `AIProvider` enum
    (`IMBUE_CLOUD`/`API_KEY`/`SUBSCRIPTION`, `primitives.py:72`), but it reaches the mngr
    provisioning layer as env vars rather than a typed field.
  - "provider" used to mean only compute (A2); the code now *also* has an `AIProvider` enum,
    so the bare word is genuinely overloaded — "AI provider" is no longer just a naming trap
    but a real, colliding code name.
  - Overlaps D5 (account holds the LiteLLM key) and G1 (model).
- **To resolve:** settle the user-facing name; give the three options friendly names. (The
  mode is already typed in minds; the open work is threading the enum, not the env vars,
  through to mngr.)

---

# H. Setup

## H1. Onboarding & data preferences

- **User's question:** "What do I set up when I first create a mind, and how much does it
  learn about me?"
- **Canonical term:** ⚠ the data-preference labels are unsettled **and** partly
  unimplemented.
- **Working definition:** A short setup where you choose how much your mind learns about you
  and give it a first task.
- **Rolls up:** onboarding Q1 `data_preference` (`UserDataPreference`:
  CONVENIENCE/PRIVACY/CONTROL + local scan); Q2 `initial_problem`; Q3 `permissions_preference`
  (the autonomy text, D3).
- **Why it's hard:**
  - **CONVENIENCE and PRIVACY currently behave identically** (only CONTROL differs) — the
    promised distinction isn't implemented (divergence). Labels can't be finalized until the
    behavior is.
  - Q3 `permissions_preference` collides with detent permissions (D3).
  - "onboarding" also means Claude Code's own first-launch flow (different thing).
- **To resolve:** make the three data preferences actually differ (or drop one); name them;
  separate Q3 from "permissions."

---

# I. Mostly developer-facing concepts

These exist today and are in scope, but their primary audience is developers/power users.
They still have naming ambiguity worth noting.

## I1. Updates from the template (upstream)

- **Canonical term:** ⚠ **template** (user-facing) vs **upstream** vs **parent** (code).
- **Definition:** The template your mind was created from; you can pull its improvements
  (`update-self`) or contribute back (`submit-upstream-changes`).
- **Why it's hard:** three names for one relationship (`parent.toml` says "parent," the git
  remote is "upstream," skills say "self"/"upstream"); `parent.toml` isn't formally parsed
  (skill-read only). Overlaps A1/templates (create template vs template repository — "main"
  is both a create-template and an agent-type).

## I2. Specs, blueprints & plans (design artifacts)

- **Canonical term:** ⚠ **spec** vs **blueprint** vs **plan** (and "plan" collides with B2).
- **Definition:** Design docs (specs) and step-by-step build plans (blueprints) for features.
- **Why it's hard:** spec/blueprint distinction is undocumented; `blueprint-generate` calls
  its output a "plan"; "plan" is also the B2 progress-view candidate. For users, collapse to
  one "plan" and keep "progress/steps" for the live timeline.

## I3. Tests & quality gates

- **Canonical term:** ⚠ taxonomy gaps. **review** is overloaded.
- **Definition:** Automated checks on the code your mind writes (unit/integration/acceptance/
  release/ratchets) and review gates.
- **Why it's hard:** `style_guide.md` omits deployment tests and e2e as categories (they
  exist); "review" means the `.reviewer` automated gate *and* the worker approval gates *and*
  human reading. The worker **approval** gate is the only user-facing one (see D1).

## I4. Changelogs

- **Canonical term:** ✅ **changelog** (settled).
- **Definition:** What changed in each update (per-PR entries fanned into CHANGELOG.md +
  UNABRIDGED_CHANGELOG.md).
- **Why it's hard:** mostly fine; CI enforcement is opaque (no visible test).

---

# J. Purely internal (NOT user-facing — the inverse list)

These are real concepts but have **no** user-facing manifestation; they should never appear
in user copy and don't need a user-facing name. Listed so the boundary is explicit (and so
"is X user-facing?" has an answer).

- **services agent** (`is_primary` agent) — hidden by design; guarded from destroy/interrupt.
- **lead agent** internals; worker branches / merge gates plumbing.
- **provider backend vs instance** *as code types* (the user concept is A2; these are the
  implementation).
- **host pool / leased host** — a speed optimization behind A2.
- **runtime secret** (`runtime/secrets/*.env`), Modal Secrets, Vault secrets — plumbing
  behind D4/F1/D6.
- **runtime state** (`runtime/<feature>/`) — plumbing behind C3/F1.
- **session** (Claude JSONL) and the in-memory **event stream / SSE** — plumbing behind
  B1/F3.
- **panel / group** (dockview internals) — users see "tab" (E2).
- **skills lock**, the **plugin/hook systems**, `LifecycleHook` — plumbing behind C1.
- **mngr `LifecycleStage`**, provisioning hooks, signal checks — provisioning plumbing.
- **system interface health / recovery probe** *internals* — the user-facing surface is the
  recovery experience in A3, but the probes themselves are internal.

---

# The shortlist: user-facing concepts with NO canonical term yet

If the goal is to unblock the technical cleanup by naming the user-facing concepts first,
these are the ones with no settled name, roughly in priority order:

1. **The mind itself** (A1) — mind / workspace / assistant. *Keystone.*
2. **Where my mind runs** (A2) — no term at all; owns provider/launch-mode/host/region.
3. **Talking to my mind** (B1) — chat/conversation vs agent/assistant.
4. **What my mind is doing now** (B2) — plan vs steps/progress.
5. **My mind's apps** (E1) — app vs service vs application.
6. **Background helpers** (B4) — helper vs worker vs background task.
7. **Approvals queue + items** (D1) — inbox vs approvals; request vs approval.
8. **Connected services** (D4) — connection vs credential.
9. **Permissions vs autonomy** (D3) — one word doing two jobs.
10. **Backups** (F1) — one concept, three mechanisms to hide.
11. **Two kinds of sharing** (D6) — out (link) vs in (file access).
12. **AI connection/billing** (G2) and **model tiers** (G1).
13. **Tasks/to-dos** (B3) — and de-collide "task."
14. **Abilities** (C1) — skill vs command; "add-ons" umbrella.

Settling these names is the lever; the technical renames in `CROSS-CUTTING.md` mostly fall
out of them.
