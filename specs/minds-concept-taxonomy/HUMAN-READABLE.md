# Human-readable definitions (app & docs voice)

The glossary in `README.md` is the precise/technical definition of each concept. This file
is the **plain-language** companion: for each concept, how you'd describe it to a
non-technical user in the app UI, a tooltip, or end-user documentation.

Two things to read for:

- **Plain language** — the one- or two-sentence version a normal person understands. Where
  a concept is pure internal plumbing a user never sees, it says so (no user-facing term is
  needed, and inventing one would be noise).
- **⚠ Naming decision** — flagged wherever the *user-facing* word is genuinely unsettled or
  has multiple defensible candidates. **This is the part to actually resolve.** A concept
  can have a clean technical canonical term (in `README.md`) and still have an unresolved
  user-facing name; those are different decisions.

A rough audience tag per concept: **[user]** = users see and act on it; **[power]** =
only technical/power users meet it; **[internal]** = never surfaced, plumbing only.

---

## The identity question that everything else hangs on

Before the per-concept list, the single biggest unresolved user-facing naming decision,
because it cascades into a dozen others:

### ⚠ What do we call "the thing the user has one or more of"? — **mind vs workspace vs assistant vs agent**

The product is called **Minds**. The code calls the unit a **workspace**. The thing the
user actually talks to is a **chat agent**. Internally a workspace is really *a container +
a hidden services agent + one or more chat agents*. Users don't think in any of those
terms.

Candidates for the top-level user-facing noun:
- **"a mind"** — matches the product name; warm, distinctive; but vague ("what *is* a
  mind?") and easy to confuse with the chat itself.
- **"a workspace"** — matches the code; neutral; but generic and sounds like a folder/IDE
  workspace, not an assistant.
- **"an assistant" / "an agent"** — matches user mental model ("my assistant"); but "agent"
  is overloaded to death internally (see below) and "assistant" may undersell it.

And a second layer: is the thing you open and chat with **the same** as the top-level
thing, or a part of it? I.e. is it "open your mind" (one mind, one chat) or "open a chat in
your mind" (one mind, many chats)? The code supports many chat agents per workspace, so the
product has to decide whether to expose that.

**Recommendation to resolve first:** pick the top-level noun (lean **"mind"** for brand
fit, with "workspace" reserved for technical/settings surfaces), and decide whether
multiple chats per mind is a user-visible concept or hidden. Everything below inherits from
this.

---

## Compute substrate

- **provider backend / provider instance** — **[user]** (concept), **[internal]** (word).
  The concept answers a user-facing question: *"Where and how does my mind run — on this
  computer, or in the cloud?"* Users choose it (via *launch mode*) and it determines whether
  they can Start/Stop locally, cost, and latency. Users never see the word "provider," but
  the concept is not internal. See *launch mode* for the control.

- **region** — **[power]** Plain language: *"Where in the world your mind's computer lives."*
  ⚠ Minor: call it **"region"** (familiar from other cloud tools) or **"location"** (softer
  for non-technical users). Lean "location" in consumer UI.

- **host** — **[user]** (as an action target), **[internal]** (word). This is the machine a
  user actually **Starts and Stops** and whose status badge they read. Plain language:
  *"your mind's computer."* Users shouldn't see the word "host," but they perceive and act
  on the thing — so it is not internal.

- **host pool / leased host** — **[internal]** An optimization (pre-warmed machines) so
  creating a mind is fast. Never user-facing; at most surfaces as "your mind is ready
  faster."

- **agent (the primitive)** — **[internal]** See the identity question above and *chat
  agent* / *worker* below for the user-facing roles. ⚠ The bare word **"agent"** is so
  overloaded internally that it's risky to also use it as the user-facing word for "the
  thing you talk to." Strongly consider **not** showing users the word "agent" at all and
  using "assistant"/"chat"/"mind" instead.

- **launch mode** (DOCKER / LIMA / CLOUD / IMBUE_CLOUD) — **[user]** Plain language:
  *"Where your mind runs — on this computer, or in the cloud."*
  ⚠ Naming decision: today the creation form exposes raw enum-ish names (DOCKER, LIMA,
  CLOUD, IMBUE_CLOUD). Non-technical users won't know Docker from Lima. Candidates for the
  user-facing framing:
  - by *location*: "On this Mac" / "In the cloud" (hide the engine entirely).
  - by *plan*: "Local (free, uses your computer)" / "Hosted (we run it for you)."
  The technical mode can stay as an advanced detail. **This needs a real decision** — the
  current labels are developer-facing.

- **host state / lifecycle** — **[user]** This is the status badge users see. Plain
  language per state: Running = *"Awake"* / *"Running"*; Stopped = *"Stopped"*; Paused =
  *"Asleep"* (idle, resumes on demand); Starting/Building = *"Starting…"*; Crashed/Failed =
  *"Something went wrong."*
  ⚠ Naming decision: **PAUSED vs STOPPED** is the confusing one for users. "Paused" (idle
  auto-suspend, cheap to resume) and "Stopped" (deliberately off) feel the same to a user
  but mean different things for cost/latency. Candidate framings: "Asleep" vs "Off", or
  "Idle" vs "Stopped." Pick words that signal *"asleep = will wake instantly"* vs *"off =
  you turned it off."*

---

## Coding-agent infrastructure

- **agent branch / commits / git remote / tags** — **[power]** Plain language for the whole
  cluster: *"A full history of every change your mind makes to its own code — you can see
  it, and roll back."* Most users won't touch git directly. ⚠ If surfaced, call it
  **"history"** or **"versions"**, not "commits/branches."

- **LLM auth mode** (LiteLLM key / raw API key / subscription) — **[power]** Plain
  language: *"How your mind pays for and connects to its AI."* ⚠ Naming decision: users
  care about the *choice* ("use my Claude subscription" vs "use an API key" vs "use Minds'
  included credits"), so the user-facing framing should be **"AI connection"** or
  **"AI billing,"** not "auth mode." The three options need friendly names.

- **model / model alias** (`opus[1m]`) — **[user]** Plain language: *"Which AI brain your
  mind uses — smarter ones are slower and cost more."* ⚠ The raw string `opus[1m]` is
  jargon. User-facing should be tiers like "Most capable / Balanced / Fastest," with the
  exact model an advanced detail. **Decision: do users pick a named model, or a tier?**

- **tools / MCP servers** — **[power]** Plain language: *"Extra abilities your mind can use,
  like searching the web or controlling a browser."* ⚠ "MCP" is pure jargon — never show
  it. "Tools" or "abilities" for users.

- **skill** — **[user]** Plain language: *"A saved how-to your mind can follow — like a
  reusable recipe for a task it's done before."*
  ⚠ Naming decision: skills are invoked as `/commands`, so to a user a skill looks like a
  **command**. Is the user-facing word **"skill"** (matches the "minds learn" story) or
  **"command"** (matches how it's invoked) or **"recipe"/"playbook"** (matches what it is)?
  This is a real fork — it shapes how the whole "your mind gets better over time" narrative
  reads.

- **skills lock** — **[internal]** Versioning detail. Never user-facing.

- **hooks / plugins (both kinds)** — **[internal]/[power]** Plain language if ever surfaced:
  *"Add-ons that extend what your mind can do."* ⚠ Don't expose the four-way "hook" / two-way
  "plugin" mess to users at all; if there's an extensions UI, pick one word ("add-ons" or
  "extensions") and hide the underlying system.

---

## What the user opens and looks at

- **workspace / mind** — see the identity question at the top.

- **chat agent** — **[user]** Plain language: *"A conversation with your mind"* OR *"an
  assistant working for you."*
  ⚠ Naming decision (important): the code is itself torn here — there's a legacy
  `Conversation` shim over the agent model. Is the thing in the left rail a **"chat"/
  "conversation"** (a thread you talk in) or an **"agent"/"assistant"** (a worker with its
  own identity)? These imply different products: chats are disposable threads; agents are
  persistent helpers. The "New Chat" button currently creates a whole agent. **Decide
  whether users are starting conversations or spawning assistants.**

- **transcript / session** — **[user]** Plain language: *"The history of your conversation."*
  ⚠ Call it **"conversation"** or **"history"** to users — never "transcript" (jargon) or
  "session" (collides with login session). The fact that one conversation can span several
  underlying "sessions" should stay hidden.

- **progress view (steps / "plan")** — **[user]** Plain language: *"A live checklist of what
  your mind is doing right now, step by step."*
  ⚠ Naming decision (important, and you flagged this exact tension): the code calls it
  "progress view"; the docs call it a "plan"; the items are "steps." For users:
  - **"Plan"** reads well *before* work ("here's my plan") but oddly *after* ("plan" of
    things already done).
  - **"Progress" / "Steps" / "Activity"** read well *during/after* but not as a
    forward-looking commitment.
  Since the timeline is built live from steps as they happen (not pre-committed), **"Steps"**
  or **"Progress"** is the more honest user-facing word; reserve "plan" for cases where the
  mind genuinely commits to a plan up front. **This needs a decision** because it also
  collides with *blueprint/spec* "plans" below.

- **tab / panel / group / layout** — **[user]** Plain language: **"tab"** for the clickable
  thing, **"layout"** for the arrangement. ⚠ Settled: users say "tab"; never expose "panel,"
  "group," or "view."

- **terminal** — **[user/power]** Plain language: *"A command line into your mind's
  computer."* ⚠ "Terminal" is fine for technical users; for general users *"command line"*
  or *"console."*

- **service / application / "web view"** — **[user]** Plain language: *"An app your mind is
  running that you can open in a tab — like a website it built."*
  ⚠ Naming decision: this is the user-facing twin of the big internal service-vs-application
  split. When a tab shows a running app, is it **"an app,"** **"a service,"** **"a page,"**
  or **"a view"**? For users, **"app"** is by far the most natural ("the app your mind
  built/started"); "service" and "application" are developer words and "web view" is
  meaningless to users. **Recommend "app" for users**, even if the code standardizes on
  "service."

- **browser** — **[user, future]** Plain language (once it exists): *"A web browser your
  mind can see and click for you."* ⚠ Not built yet — don't promise it in UI/docs until the
  `url:` iframe grows real browser control. Flagged in the divergence register as
  aspirational.

---

## Work the mind does

- **worker / sub-agent / background agent** — **[user]** Plain language: *"A helper your
  mind spins up to work on a task in the background while you keep going."*
  ⚠ Naming decision (you flagged this cluster): internally it's called worker / sub-agent /
  background agent inconsistently. For users the candidates are **"helper,"** **"background
  task,"** **"teammate,"** or **"worker."** "Sub-agent" is jargon; "background agent"
  re-introduces "agent." Lean **"helper"** or **"background task"** for users. **Decide.**

- **task brief / "task"** — **[user]** Plain language: *"The instructions you give a helper."*
  ⚠ Avoid the bare word "task" in UI because it collides with tickets/steps; "instructions"
  or "the brief" is clearer.

- **ticket / step record** — **[power/user]** Plain language: ticket = *"A unit of work,
  like a to-do item your mind tracks"*; step record = *"One step in what the mind is doing
  this turn"* (the same things shown in the progress view).
  ⚠ Naming decision: do users ever see "tickets"? If there's a task/to-do view, is it
  **"tasks,"** **"to-dos,"** **"tickets,"** or **"work items"**? "Ticket" is developer/
  support-desk flavored. For a personal-assistant product, **"to-dos"** or **"tasks"** reads
  better — but "tasks" collides with the helper "task brief." This is genuinely tangled at
  the user layer and worth resolving alongside the worker naming.

- **code review gate / approval gate** — **[power]** Plain language: *"An automatic quality
  check on the code your mind writes before it's accepted."* ⚠ Users likely never see the
  internal `.reviewer` gate; the worker "approval gate" *is* user-facing ("approve this
  plan?") — call that an **"approval"** to users, distinct from the automated "review."

- **crystallized vs hand-authored skill** — **[internal]** A provenance detail. Plain
  language if surfaced: *"learned automatically"* vs *"written by hand."* Most users don't
  need the distinction.

- **services agent ("primary" / system-services)** — **[internal]** Deliberately hidden from
  users. No user-facing term — and that's correct. ⚠ Just make sure UI copy never
  accidentally says "primary agent," which a user would assume means *their* main chat.

---

## Communication & approvals

- **send message** — **[user]** Plain language: just *"message"* / *"send a message."*
  Settled.

- **notification** — **[user]** Plain language: *"An alert from your mind."* Settled.

- **inbox** — **[user]** Plain language: *"Where your mind's requests for you wait for a
  response."*
  ⚠ Naming decision: today the inbox holds *only* permission requests, but the word "inbox"
  promises a general queue (messages, approvals, notifications). Decide whether "inbox" is
  (a) specifically the **approvals queue** — then maybe call it **"Approvals"** or
  **"Requests"** — or (b) a general inbox you intend to grow into. Calling a
  permissions-only drawer "Inbox" sets an expectation the product may or may not want to
  meet.

- **permission request / approval** — **[user]** Plain language: *"Your mind is asking
  permission to do something — you can allow or deny it."*
  ⚠ Naming decision: is each item an **"approval,"** a **"request,"** a **"permission,"** or
  an **"ask"**? And the outcomes — users see allow/deny; the code says GRANTED/DENIED (and
  the concepts doc wrongly adds "failed," see divergences). Recommend **"request"** for the
  item and **"Allow / Deny"** (or "Approve / Decline") for the actions. Keep it consistent
  with whatever the inbox is called.

---

## Identity, security & access

- **runtime secret** — **[internal]** Plumbing. Never user-facing.

- **service credential** (Slack/GitHub/etc. via latchkey) — **[user]** Plain language:
  *"A connected account your mind can use on your behalf — like your Slack or Google."*
  ⚠ Naming decision: when a user connects Slack, is that a **"connection,"** an
  **"integration,"** a **"connected account,"** or a **"credential"**? "Credential" is a
  security word, not a consumer word. Recommend **"connection"** or **"connected account"**
  for users; reserve "credential" for technical surfaces.

- **account credential** (the user's own Minds login) — **[user]** Plain language: *"Your
  Minds account / login."* ⚠ Note the collision with *connected accounts* above — "account"
  means both the user's own login and the third-party services they connect. Keep "your
  account" for the login and "connections" for the third-party ones.

- **permission / scope (detent)** vs **permissions_preference** — **[user]/[power]** Plain
  language (detent): *"What your mind is allowed to access."* ⚠ Naming decision: the word
  "permissions" is used for both the enforced access rules *and* a free-text onboarding
  preference ("how much freedom should your mind have?"). To users these must be clearly
  separated: enforced rules = **"permissions"** / **"access,"** the preference =
  **"autonomy"** or **"how much your mind can do without asking."** Don't show both as
  "permissions."

- **account** — see above (collision with connected accounts).

- **workspace sharing vs file access grant** — **[user]** Plain language: workspace sharing =
  *"Let someone else open one of your mind's apps via a link"*; file access grant = *"Let
  your mind read/write specific files on your computer."*
  ⚠ Naming decision: both are currently "sharing," but they're opposite directions —
  *sharing out* (giving others a link) vs *granting in* (giving the mind file access). Using
  "Share" for both will confuse users. Recommend **"Share"** only for the outward link, and
  **"File access"** / **"Allow file access"** for the inward grant.

- **onboarding / data preference** (CONVENIENCE / PRIVACY / CONTROL) — **[user]** Plain
  language: the setup question *"How much should your mind learn about you?"*
  ⚠ Naming decision (and note the divergence: CONVENIENCE and PRIVACY currently behave
  identically in code). The three options need real user-facing labels and need to actually
  differ. Candidate framing: **"Personalized"** (learn from my computer) / **"Private"**
  (keep data local, minimal) / **"Manual"** (ask me / learn nothing). As written, "Privacy"
  promising less data collection than "Convenience" is a promise the code doesn't keep yet —
  resolve the behavior before shipping the labels.

---

## Data & durability

- **runtime state** — **[internal]** Plumbing.

- **agent memory** vs **user memory** — **[user]** Plain language: *"What your mind
  remembers about you and your work."*
  ⚠ Naming decision: "memory" is good and user-facing. The wrinkle is there are two
  memories (per-mind workspace memory vs your cross-session user memory). Decide whether
  users see one unified "Memory" or two. Most users will expect one.

- **backup vs runtime checkpoint vs snapshot** — **[user]** Plain language: *"A saved copy
  of your mind you can restore from."*
  ⚠ Naming decision (important): there are *three* durability mechanisms (encrypted remote
  backup, frequent git checkpoints of state, provider VM snapshots) and they're all called
  "backup"/"snapshot" somewhere. To a user there should probably be **one** concept:
  **"Backups."** Decide what the user's "Backup" / "Restore" button actually maps to (almost
  certainly the encrypted remote backup), and hide the checkpoint/snapshot machinery. Don't
  expose three durability words to users.

- **file sharing** — see *file access grant* above (same feature, user-facing name needs to
  not collide with "Share").

- **versions / history** — **[power]** Plain language: *"Every past version of your mind's
  work, restorable."* ⚠ If exposed, "history" or "versions"; there's no user surface for
  this today (flagged in divergences).

- **logs / events / activity** — **[power]** Plain language: *"A record of everything your
  mind did and why."*
  ⚠ Naming decision: for users this is best framed as **"Activity"** or **"History,"** not
  "logs" or "events" (both jargon). This is also the future "audit log / diary" concept —
  worth picking "Activity" now so it scales.

- **spec / blueprint / plan** — **[power]** Plain language: spec = *"a description of what to
  build"*; blueprint = *"a step-by-step plan for building it."*
  ⚠ Naming decision: three words (spec, blueprint, plan) for design artifacts, **plus**
  "plan" is also the progress-view candidate above. If any of this is user-facing, collapse
  it: pick **"plan"** for the build plan and use **"progress"/"steps"** for the live
  timeline (so "plan" isn't used for two different things). "Spec" vs "blueprint" is a
  developer distinction users don't need.

- **changelog** — **[user]** Plain language: *"What changed in each update."* Settled.

- **upstream / parent / template** — **[power]** Plain language: *"The template your mind was
  created from — you can pull in its improvements."*
  ⚠ Naming decision: internally it's parent/upstream/template. For users, **"template"** is
  the friendly word ("update from template"); "upstream"/"parent" are git jargon. Pick
  "template" for users even though the technical canonical term is "upstream."

---

## Cross-references

- The technical canonical terms are in `README.md`.
- The internal overloaded-term decisions are in `CROSS-CUTTING.md`. Several of the
  ⚠ flags above are the *user-facing half* of an internal collision (service/application,
  backup/snapshot, sharing, permissions, agent) — resolve them together with their
  technical counterparts.
- Two ⚠ flags are also `DOC-CODE-DIVERGENCES.md` entries (the permission "failed" outcome
  that doesn't exist; CONVENIENCE/PRIVACY behaving identically) — the user-facing copy
  can't be finalized until the underlying behavior is.

## The decisions that actually need a human

If you only resolve a handful, these are the ones blocking the most copy:

1. **Top-level noun**: mind vs workspace vs assistant (and one-chat-per-mind vs many).
2. **The thing you talk to**: chat/conversation vs agent/assistant.
3. **Live timeline**: "plan" vs "steps/progress" (and de-collide from build "plans").
4. **A running app in a tab**: "app" vs "service" vs "view."
5. **Background helper**: helper vs background task vs worker (and de-collide "task").
6. **Durability**: one user-facing "Backup," hiding checkpoint/snapshot.
7. **Two kinds of sharing**: "Share" (out) vs "file access" (in).
8. **Approvals queue**: "Inbox" vs "Approvals/Requests," and allow/deny wording.
9. **Connected services**: "connection" vs "credential" vs "integration."
10. **Launch mode + model**: location-based and tier-based friendly labels, not enum names.
