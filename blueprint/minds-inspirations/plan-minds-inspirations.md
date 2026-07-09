# Plan: Minds Inspirations (publish & use)

## Overview

- Adds an "inspirations" concept: a way for a running mind to **publish** a reusable snapshot of the apps/features it built, and for another mind to **adapt** an existing inspiration into itself.
- All work lands in the **forever-claude-template (FCT)** repo (edited via a `.external_worktrees/forever-claude-template` worktree, per CLAUDE.md). There are **no `apps/minds` (desktop-client) changes** and, in the final design, **no `system_interface` UI changes either** — all publish interaction happens inline in chat (see the design revisions section).
- Deliverables:
  - Agent-awareness: a **one-sentence** mention in the FCT `CLAUDE.md` that inspirations exist (so the agent knows the concept). It does **not** push the agent to proactively offer publishing, and does not enumerate the skills (the agent already knows them).
  - `/publish-inspiration` skill (FCT): assembles a clean, shareable repo from the current mind (in an isolated local `git worktree` in the same container) and pushes it to GitHub.
  - `/use-inspiration` skill (FCT): merges an existing inspiration (by git URL) into the current mind and fills in its holes.
  - **Inline chat confirmation** (no popups): the agent presents the proposed title, description, repo name, visibility, and thumbnail in chat, and the user confirms or edits them there. (The originally-shipped system_interface publish popup and GitHub-login modal were removed — code deleted — after live testing; see the design revisions section.)
  - **Latchkey GitHub permissioning** (no `gh` CLI): the agent self-initiates the permission requests the user approves in the minds app — `github-rest-api` (`github-read-user` + `github-write-all`) for repo creation, description, and the `minds-inspiration` topic via `latchkey curl`, and `github-git` / `github-git-write` for the push itself, which runs through the latchkey gateway's native git smart-HTTP proxying. No GitHub token ever enters the container.
- Key design decisions locked during planning:
  - The inspiration is assembled by a **`launch-task` worker on its own isolated worktree** (`mngr/<slug>`), so the live mind is untouched during assembly. One worker cycle: the worker runs `build_inspiration.sh`, fleshes out the manifest's FILL-IN sections, and designs a bespoke thumbnail SVG. (This went worker, then local worktree, then back to worker across live testing — see the live-testing fixes and design revisions sections.) The worktree is reduced to a **clean base = the FCT version the mind was created from** (its own FCT base commit/ref — *not* a freshly fetched upstream), plus only the user-selected app/feature paths — no experimental cruft, no user data, no secrets.
  - **No merge-back invariant**: the lead pushes the published branch directly from the worker's worktree; nothing ever merges into or writes to the live mind's checkout after assembly starts.
  - The published repo records a **simple provenance link** to the FCT version it was based on — and does **nothing else** with upstream: no fetching, no pulling, no updating. Reusing whatever FCT version the mind already had keeps inspiration creation dead simple; because the base is a real FCT tree, the inspiration works as a proper template when later used.
  - A single inspiration repo can **accumulate multiple inspirations** over time (one `inspiration-<name>.md` manifest per inspiration at the repo root); each inspiration can contain multiple apps.
  - The manifest is a **worksheet**: it records what the inspiration is, its holes/permissions (freeform prose), and how it was later adapted.
  - Scope is intentionally bounded: no catalog/discovery UI for browsing inspirations (separate, later effort).

## Expected behavior

### Agent-awareness (CLAUDE.md)

- A single sentence in `CLAUDE.md` tells the agent that inspirations exist (a publishable/usable snapshot of what a mind built). That is all — it does **not** instruct the agent to proactively offer publishing, and does not enumerate the skills.
- Consequence: publishing is **user-initiated** for now (the user asks to publish, or invokes `/publish-inspiration`). Proactive "offer to publish" behavior is intentionally deferred.

### Publishing (`/publish-inspiration`)

- The agent (the primary mind) asks the user a few setup questions in chat: what to call the inspiration, which apps/features to include, and whether any data should be included. It does **not** enumerate specific files to the user. Setup ends with a **hard confirmation gate before the worker is dispatched**: the agent echoes the proposed title, repo name, scope, and data inclusion and waits for the user's go-ahead. A rename arriving after dispatch is handled in place (pass the new slug to the script if it hasn't run yet, else `git mv` the slug-bearing files in the worker's worktree) — never by tearing down or relaunching the worker, whose name and branch are internal plumbing.
- By default, **no user data** is included — only app/UI/code — and data is included only when the user explicitly asks.
- The agent delegates the repo **assembly** to a **`launch-task` worker** on an isolated worktree (`mngr/<slug>`). One worker cycle: the worker runs `build_inspiration.sh` (a fast, deterministic script), fleshes out the manifest's FILL-IN sections, and designs a bespoke thumbnail SVG. The live mind keeps running untouched during assembly.
- In that worktree, the script establishes a **clean base = the FCT version the mind was created from** (resets to the mind's own FCT base commit/ref — no upstream fetch; the fallback is the *first-parent* root plus a bootable-template pre-check, since subtree merges create parallel near-empty roots), copies in only the file/paths backing the chosen apps/features, strips secrets, and makes a single commit. A simple provenance link to that FCT version is recorded; nothing is pulled or updated from upstream.
- The script generates an `inspiration-<name>.md` manifest at the repo root: YAML front-matter (title, description, thumbnail) plus a thorough markdown body — what it is, how it works, how to adapt it, its holes, and the permissions it may need — with clearly-marked FILL-IN sections the worker completes before reporting back.
- The thumbnail is always a **bespoke, app-relevant SVG designed by the worker** (mock data only, never real user data; the sanitization rules apply). A deterministic placeholder-marker gate blocks publishing the generic template SVG.
- The script overwrites the snapshot's `/welcome` skill with a generated, inspiration-specific one (the template's own welcome skill is untouched by the feature) and runs the **boot smoke-check** (the mind boots from the clean base; selected apps need not fully function — holes are expected). It communicates via exit codes; on success the worker reports back. **No merge-back**: the lead pushes directly from the worker's worktree; nothing ever merges into or writes to the live mind's checkout after assembly starts.
- The agent presents the proposed title, description, SVG thumbnail, and repo settings (name, private/public) **inline in chat** and waits for the user to confirm or edit them there — there is no popup.
- GitHub access goes through latchkey permissioning end-to-end (the `gh` CLI is banned from the flow): the agent probes access, self-initiates the `github-rest-api` (`github-read-user` + `github-write-all`) and `github-git` / `github-git-write` permission requests the user approves in the minds app, creates the repo (name + description + visibility) and sets the `minds-inspiration` topic via the REST API, and pushes with git through the latchkey gateway's proxy URL (the gateway natively proxies GitHub's git smart-HTTP endpoints and injects the credential server-side — no GitHub token ever enters the container). The chat confirmation embeds the thumbnail as a markdown image.
- On confirmation, `/publish-inspiration` always creates a **new** GitHub repo (private by default; public if the user says so) via the REST API through `latchkey curl` (name + description + visibility in one call), then pushes the assembled branch with git through the latchkey gateway.
- If repo creation fails (name taken, token insufficient), the agent reports it and asks in chat for a new name, keeping the assembled commit intact.
- Publishing a mind that already holds accumulated inspirations carries **all** existing `inspiration-*.md` manifests and their apps into the new repo, alongside the newly-published one.

### Using an inspiration (`/use-inspiration`)

- Two entry points:
  1. **Template path**: a new mind is created with an inspiration repo as its template. On startup the snapshot's generated `/welcome` takes over: the agent leads with a custom message naming the inspiration (not the generic template greeting), reads the manifest in the same turn, surfaces its **Prerequisites**, and asks whether to connect the user's own accounts now. **Activation-first**: if yes, the agent itself initiates each machine-readable `requires_permission:` line via a latchkey permission request, wires any `requires_secret:` values, and gets the app showing the user's own data — the definition of done for a data-backed app (not a service start or a 200) — before asking how they want to adapt it. It defaults to adapting the **latest** inspiration; older manifests are primarily reference (likely already adapted).
  2. **Merge path**: an existing mind (built from a different template) runs `/use-inspiration <git-url>`. The skill `git remote add`s the inspiration and merges/subtrees it in.
- After bringing in the inspiration, the agent reads the relevant `inspiration-<name>.md` manifest, activates its Prerequisites first when the user keeps the same connectors (initiating latchkey requests itself, done = the user sees their own data), then asks in chat how they want to adapt it and works through the manifest's holes interactively (e.g. swapping Slack for email).
- Merged-in `inspiration-<name>.md` manifests stay at the repo root and accumulate alongside existing ones.
- As it adapts, the agent appends a dated "how it was adapted" section to the relevant manifest so the file captures its own history (the worksheet behavior).

### Publish confirmation and GitHub auth (in chat — no system_interface UI)

- There are **no popups** in the final design. The originally-shipped publish popup and GitHub-login modal were removed entirely (code deleted) after live testing; see the design revisions section for the rationale.
- **Publish confirmation** happens inline in chat: the agent presents the proposed fields (title, description, repo name, visibility, thumbnail) and the user confirms or edits them in the conversation.
- **GitHub access** goes through latchkey permissioning, like every other connector: the agent probes `latchkey curl https://api.github.com/user`, initiates the permission requests itself when needed (approved by the user in the minds app) — `github-rest-api` with `github-read-user` + `github-write-all` for the API operations (repo create, description, topic, all via `latchkey curl`; the narrower `github-write-repos` schema covers only `/repos/...` paths and would 403 the `POST /user/repos` creation call) and `github-git` / `github-git-write` for the push. A git push over HTTPS is just two smart-HTTP calls (`GET info/refs?service=git-receive-pack` + `POST git-receive-pack`), and the latchkey gateway proxies them natively: git pushes to `$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git` with the gateway's two auth headers passed as one-shot `-c http.extraHeader` options, and the GitHub credential is injected server-side (it never enters the container). This depends on the `mngr_latchkey` gateway-spawn change raising `--max-body-size` (upstream default 10 MiB; a full-history push packfile is ~30 MiB today). The `gh` CLI is banned from the flow.

## Implementation plan

> All files are in the **forever-claude-template (FCT)** repo, edited via a `.external_worktrees/forever-claude-template` worktree on the same branch name as this repo's working branch (per CLAUDE.md), and committed there. There are **no `apps/minds` changes**.

### system_interface — no changes in the final design (popups removed)

The publish popup and GitHub-login modal (backend endpoints, `inspiration.py`/`github_auth.py` logic, Pydantic models, and the Mithril `InspirationPublishModal`/`GitHubLoginModal` frontends) were implemented in the feature round and then **removed entirely — code deleted — in the design revisions of 2026-07-03**. Publish confirmation is inline in chat; GitHub auth was first the chat-surfaced `gh` device flow, then (2026-07-06) replaced by latchkey permissioning with the `gh` CLI banned entirely.

### Forever-claude-template skills + docs (FCT)

- `CLAUDE.md`
  - Add a **single sentence** noting that inspirations exist (a publishable/usable snapshot of what a mind built). Do **not** enumerate the skills and do **not** tell the agent to proactively offer publishing — that nudge is deferred for now.
- `.agents/skills/publish-inspiration/SKILL.md` (new)
  - Implements the publish flow: setup Q&A; **delegate assembly to a `launch-task` worker** (write the task file, `create_worker.py launch --template worker`, background `await` for the report, proxy `question` gates — **no merge-back**: the lead pushes directly from the worker's worktree); the worker runs `build_inspiration.sh` to establish the clean base by resetting to the FCT version the mind was created from (the mind's own FCT base commit/ref — no upstream fetch), does file/path-level selection + single commit, secret stripping, `inspiration-<name>.md` generation, writing an inspiration-specific `/welcome` into the snapshot, and the boot smoke-check, then fleshes out the manifest's FILL-IN sections and designs a bespoke thumbnail SVG — one worker cycle; then present the proposed fields **inline in chat** for confirmation; ensure GitHub access via latchkey permissioning (self-initiated `github-rest-api` and `github-git` requests when the probes fail); REST-API repo create via `latchkey curl` + gateway-proxied git push from the worker's worktree; failure handling (ask in chat for a new name); and accumulation (carry existing manifests/apps forward).
  - May include a helper script (e.g. `.agents/skills/publish-inspiration/scripts/build_inspiration.sh`) for the git assembly the worker runs, kept self-contained in the FCT (the dev `create-new-mind-repo` recipe is **not** available inside the VM).
- `.agents/skills/use-inspiration/SKILL.md` (new)
  - Implements the merge path: `git remote add` + `git fetch` + **`git merge --allow-unrelated-histories`** of the inspiration's branch (`git subtree` cannot target the repo root as its prefix, so the skill forbids it; the plain merge preserves both trees at the root and coexists with the provenance link), manifest reading, interactive hole-filling Q&A, manifest worksheet append (dated "how it was adapted"), and accumulation (manifests stay at root).
  - Conflict handling: when a second inspiration collides with an existing app dir/file, the agent figures it out and resolves interactively, surfacing the collision to the user as a "hole" — always in non-technical language, asking the user only if it is unsure.
- `.agents/skills/welcome/SKILL.md` (existing in FCT)
  - Updated by `/publish-inspiration` at publish time to reflect the latest inspiration. The plan adds the rewrite logic to the publish skill; the welcome skill itself needs a stable, templated structure the publish skill can target.
- Manifest convention
  - Define the `inspiration-<name>.md` format (front-matter keys: `title`, `description`, `thumbnail`; body sections: What it is, How it works, How to adapt it, Holes, Permissions it may need, Adaptation history).
  - The thumbnail is stored as `inspiration-<name>.svg` next to the manifest at the repo root; the front-matter `thumbnail` key holds its relative path.
- Provenance link (no upstream updating)
  - The inspiration records **which FCT version it was based on** (reuse the `parent.toml` pointer the mind already carries). This is provenance only: the published repo does **not** fetch, pull, or update from upstream. The clean base simply *is* whatever FCT version the mind started from, so the tree is already a proper FCT tree and works as a template when used. Keeping it link-only is what makes inspiration creation simple.
- FCT changelog
  - New entry per FCT changelog conventions describing the two skills + CLAUDE.md awareness.

### Cross-cutting

- Naming: the repo name and `inspiration-<name>.md` slug both derive from a slug of the user's title; the user can override the repo name in the chat confirmation.
- Secrets: start from the repo's existing reasonable defaults (the `.gitignore` set — `.env*`, `.runtime/`, `memory/`, etc.) as the baseline denylist, and have the agent actively reason about whether any other secrets are present in the selected changes (it should always be thinking about all changes), excluding anything it identifies.

## Implementation phases

> Phases 1 and 2 were implemented and later **removed** (popups deleted) in the design revisions of 2026-07-03; they are kept below as history.

- **Phase 1 — system_interface publish popup (testable in isolation)** *(implemented, later removed)*
  - Backend: `inspiration_endpoints.py` + `inspiration.py` + `models.py` + `server.py` wiring (publish-request/confirm/abort/status + the response-file handshake).
  - Frontend: `InspirationPublishModal.ts`, `models/InspirationPublish.ts`, `App.ts` + `StreamingMessage.ts` wiring.
  - Result: posting a publish-request opens the box pre-filled; submitting writes the response file with edited values. Backend pytest + manual UI check.

- **Phase 2 — system_interface GitHub-login modal** *(implemented, later removed)*
  - Backend `github_auth_*` + frontend `GitHubLoginModal.ts` / `models/GitHubAuth.ts`, mirroring the Claude-auth modules; persist the credential via `gh` (store + git credential helper), **no agent restart**.
  - Result: a user without `GH_TOKEN` can log in from the UI and the token reaches the agent.

- **Phase 3 — FCT `/publish-inspiration` skill (happy path, launch-task)**
  - Write the skill + helper script: setup Q&A, launch-task delegation, clean-base assembly, manifest + thumbnail, `/welcome` rewrite, boot smoke-check, REST-API repo create + git push.
  - Confirmation and GitHub auth are inline in chat in the final design (originally wired to the Phase-1/2 popups, since removed). Add the one-sentence `CLAUDE.md` mention that inspirations exist (no proactive-nudge guidance).
  - Result: a mind can publish a single-app inspiration to a fresh private repo. Manually verified end-to-end.

- **Phase 4 — FCT `/use-inspiration` skill (both paths)**
  - Merge path: `git remote add` + `git fetch` + `git merge --allow-unrelated-histories`, manifest read, interactive hole-filling, worksheet append.
  - Template path: rewrite `/welcome` so a new mind built from an inspiration repo adapts the latest inspiration on startup; surface older manifests as reference.
  - Result: a mind can adapt an existing inspiration both by URL and by being created from an inspiration repo.

## Testing strategy

### system_interface (removed)

- The backend and frontend test suites written for the popup/modal code were deleted along with that code in the design revisions of 2026-07-03. No system_interface tests remain for this feature.

### FCT skills (markdown — manual verification)

- Verify manually by exercising the flow inside a running mind (per minds-dev-workflow), not via pytest:
  - Publish a single-app inspiration via the launch-task worker; confirm the worktree is isolated (and nothing merges back into the live mind's checkout), the new repo is clean (no `.env`/user data), boots from the clean base, has a valid `inspiration-<name>.md` with completed FILL-IN sections + a bespoke (non-placeholder) SVG thumbnail and a generated inspiration `/welcome`, and the inline chat confirmation round-trips edited values.
  - Publish from a mind without GitHub permission; confirm the agent initiates both latchkey requests (`github-rest-api` and `github-git`), the user approves them in minds, the API calls succeed, and the push works through the gateway (or the agent stops with a clear message when approval never comes).
  - Publish from a mind with an existing accumulated inspiration; confirm both manifests/apps are carried forward.
  - Adapt by URL and via the template path; confirm merge, hole-filling, and the dated worksheet append.

### Edge cases to cover explicitly

- No diff vs `main` (nothing to publish) — clear message, no empty repo.
- Selected apps include secret-bearing files — stripped, with a note to the user.
- Boot smoke-check fails outright (base doesn't boot) — abort before creating the repo.

## Open questions

- ~~**Lead vs. worker division for popup + push.**~~ Resolved (twice) by live testing: assembly is delegated to a `launch-task` worker (one cycle: script + manifest FILL-INs + bespoke thumbnail); the lead owns the chat confirmation, GitHub auth, and the push, done directly from the worker's worktree — no merge-back (see the design revisions section).
- ~~**Publish-popup transport.**~~ Moot — the popup was removed; confirmation is inline in chat (see the design revisions section).
- ~~**GitHub login flow.**~~ Resolved (final): latchkey GitHub permissioning; the `gh` CLI and the login modal are both gone. (Interim iterations used a system_interface modal, then the `gh` device flow in chat.)
- ~~**Clean-base mechanism in the worker.**~~ Resolved in the shipped `build_inspiration.sh`: the worker stages the selected paths out of its own checkout (its worktree starts from the mind's HEAD), then resets to the base with `git read-tree -u --reset <BASE_REF>` + `git clean -fdxq` (drops tracked-but-not-in-base files AND gitignored cruft; never `git checkout <ref> -- .`), then overlays the staged paths back with a root-to-root `rsync`. The selected paths are conveyed as `--include` arguments baked into the launch-task task file (no `source_artifacts_dir` needed).
- ~~**`/welcome` rewrite target.**~~ Resolved (final design): the template's welcome skill is not modified at all — no marker region. The build script overwrites the SNAPSHOT's `.agents/skills/welcome/SKILL.md` with a generated inspiration-specific welcome, so any bootable base works (including ones predating the inspirations feature), and the base pre-check needs only `pyproject.toml` + `supervisord.conf`.

### Resolved during planning

- **Publish UI location.** Built directly into the FCT `system_interface` web UI (a new modal + endpoints), **not** a minds desktop-client request type. **No `apps/minds` changes are needed** — the minds desktop client already proxies the system_interface web UI as the workspace, and forwards its HTTP/WebSocket traffic generically, so the new routes/SSE events and modals appear with no minds-side awareness. Everything ships by updating FCT. *(Superseded 2026-07-03: the popups were removed entirely; all interaction is inline in chat — see the design revisions section.)*
- **Clone/assembly mechanism.** Delegated to a `launch-task` sub-agent on an isolated worktree (`mngr/<slug>`), rather than a hand-rolled temp clone in the publish skill. *(Briefly replaced by a local worktree in round 1, then reinstated in the 2026-07-03 design revisions.)*
- **GitHub auth.** A new system_interface GitHub-login modal mirroring the Claude-login modal's UI/endpoints; persists the credential via `gh` (store + git credential helper) so the running agent can push immediately — **no agent restart** (the credential is only needed at `git push` time, not in the process env at startup). *(Superseded 2026-07-03: the modal was removed; the `gh` device flow is surfaced in chat, keeping the store persistence and no-restart property.)*
- **Merge mechanics.** `/use-inspiration` merges the inspiration at the repo root; collisions between accumulated inspirations are surfaced to the user as holes and resolved interactively, in non-technical language. *(Implementation note: the originally-planned `git subtree` cannot target the repo root as its prefix, so the shipped skill forbids it and uses `git fetch` + `git merge --allow-unrelated-histories` instead.)*
- **Upstream handling.** Provenance link only — the inspiration records which FCT version it was based on (the `parent.toml` pointer the mind already has) and does nothing else: no fetching, pulling, or updating from upstream. The clean base is whatever FCT version the mind started from.
- **Agent-awareness.** `CLAUDE.md` gets a single sentence noting inspirations exist; no proactive "offer to publish" behavior (deferred). Publishing is user-initiated.
- **Skill name.** The use/adapt skill is `/use-inspiration` (formerly `/adapt-inspiration`).
- **Secret denylist.** Baseline is the repo's existing `.gitignore` set (`.env*`, `.runtime/`, `memory/`, etc.); the agent additionally reasons about any other secrets in the selected changes and excludes them.
- **Thumbnail storage.** `inspiration-<name>.svg` next to the manifest, referenced by relative path in the front-matter `thumbnail` key.
## Live-testing fixes (implemented, 2026-07-02)

The feature was implemented (FCT commit `fc4e0c46`) and exercised end-to-end by a real mind (`yo-inspo`, env `dev-inspiration`). The publish worked but surfaced real defects — several diagnosed from the publishing agent's own retrospective, each root-caused with evidence and fixed in the FCT. Two fix commits: `ee8443d7` and `72b7160b`.

### Round 1 (`ee8443d7`)

- **GitHub-login modal could not persist a credential (`GH_TOKEN` shadowing).** `gh` gives `GH_TOKEN`/`GITHUB_TOKEN` absolute priority over its credential store, and the system_interface process inherits `GH_TOKEN` — so `gh auth login` refused to store and `gh auth status` reported the env token. Fixed by scrubbing the token env vars from every `gh` child invocation in the auth backend (parent env untouched); the skill scrubs them for its own status probe and the final push (`env -u GH_TOKEN -u GITHUB_TOKEN`).
- **Assembly was minutes instead of seconds.** Timestamps showed the ~20-minute publish was dominated by agent-turn latency around a `launch-task` sub-agent (worker boot/read/report/poll cycles, plus a forced retry), not by the assembly script (~0.2s measured). Replaced the sub-agent with a local throwaway `git worktree` in the same container — identical isolation, none of the latency. *(Later reversed: the 2026-07-03 design revisions returned assembly to a `launch-task` worker, by user decision.)*
- **Secret scan false positives.** The scan covered the whole assembled tree including the trusted public FCT base, whose own test fixtures hold placeholder tokens (`sk-ant-test`) — blocking every publish. Now scans only the overlaid paths, with token patterns requiring realistic key lengths.
- **Boot smoke-check via `uv run`** rebuilt the whole project env just to parse `supervisord.conf` (slow, spurious failures). Now uses the interpreter behind the installed `supervisord` binary.

### Round 2 (`72b7160b`)

- **Popups never appeared (root cause of most of the thrash).** Popup events were fire-and-forget over a transient WebSocket: with no live client connected at broadcast time, the POST returned 200 but the popup went into the void, and the skill blind-polled for minutes and re-triggered serially. Backend fanout itself was proven correct by a live WS experiment. Fixed both sides: the backend now retains the pending publish request and any unresolved GitHub-auth prompt and **replays them to every newly-connecting client**, and the trigger endpoints return **`ws_client_count`** so the skill skips waiting when nobody is listening. The skill now waits one bounded ~90s window at most, then falls back to inline chat confirmation (publish) or a chat-surfaced `gh` device flow (auth) — one mechanism at a time, no serial thrash. *(Superseded: the 2026-07-03 design revisions removed the popups entirely; the fallbacks became the only mechanism.)*
- **Wrong base commit on multi-root repos.** The BASE_REF fallback used a bare root-commit lookup; subtree merges give a mind repo several parallel near-empty roots (the real repo had 4), so assembly built on a wrong root and burned a full round-trip. The fallback is now the **first-parent root**, governed by a mandatory seconds-cheap pre-check that the base tree is a bootable template (`pyproject.toml` + `supervisord.conf`), walking the first-parent chain forward when needed; `build_inspiration.sh` re-validates and exits 5 as a backstop.
- **Welcome never took over.** A mind created from an inspiration repo led with the generic template greeting and never started adapting. Final design: the published snapshot ships its own **generated inspiration-specific `/welcome` skill** (the template's welcome is untouched): custom message naming the inspiration, same-turn manifest read, and an immediate "how do you want to adapt it?" conversation.
- **Manifest was too thin.** The generated `inspiration-<slug>.md` is now a thorough, self-sufficient explainer — What it is / How it works / How to adapt it / Holes / Permissions it may need / Adaptation history — with clearly-marked FILL-IN blocks the publishing agent completes before confirmation. *(In the final design the worker completes the FILL-INs — see the design revisions section.)*
- **`gh` device flow + scopes.** The web login's expect logic was rebuilt against gh 2.95's real PTY transcript (it previously timed out waiting for the one-time code), and it now requests the `workflow` scope up front (the template ships CI workflows, so the first push needs it); auth status surfaces token scopes with a warning when `workflow` is missing.
- **Thumbnail ordering.** The confirmed thumbnail/manifest edits are committed before the push (previously the placeholder was pushed first and re-pushed), with a clean-`git status` pre-push check.

### Known-good verification state

- system_interface backend: 43 tests passed across the inspiration + github-auth suites; frontend build + 378 vitest tests passed (from the feature round; those suites were deleted along with the popup code in the 2026-07-03 design revisions).
- The secret-leak/mis-nest safety of the clean-base reset was verified empirically against synthetic repos (tracked secrets dropped, apps land at correct paths, token-in-selected-path hard-fails before commit), as were the exit-5 base validation and the welcome-region takeover rewrite.

### Design revisions from live testing (2026-07-03)

The user redirected the design after further live testing; the following is the final design and supersedes anything above that conflicts with it.

- **No popups anywhere.** The system_interface publish popup and GitHub-login modal are **removed — code deleted** (backend endpoints, business logic, models, and the Mithril modals, along with their test suites). Live testing hit three separate popup-delivery bugs — the fire-and-forget WebSocket broadcast, a connect race, and a mithril keyed-child render fatal — on top of general popup UX friction; chat is the mind's native, reliable channel. Publish confirmation is now **inline in chat**; GitHub auth moved to the `gh` device flow in chat, and then (2026-07-06) to **latchkey permissioning** with the `gh` CLI banned from the flow entirely.
- **Assembly is delegated to a `launch-task` worker again** (user decision, reversing the round-1 local-worktree change). One worker cycle: the worker runs `build_inspiration.sh` on its isolated worktree, fleshes out the manifest's FILL-IN sections, and designs a bespoke thumbnail SVG. The **no-merge-back invariant is unchanged**: the lead pushes directly from the worker's worktree; nothing ever merges into or writes to the live mind's checkout after assembly starts.
- **The thumbnail is always a bespoke, app-relevant SVG designed by the worker** (mock data only, never real user data; the sanitization rules are preserved). A **deterministic placeholder-marker gate** blocks publishing the generic template SVG.

### Design revision: git push through the latchkey gateway (2026-07-07)

The interim design authenticated the git push with the mind's `GH_TOKEN` on the assumption that a push was "not an HTTP call latchkey can inject into". That assumption was wrong: git push over HTTPS is two smart-HTTP calls (`GET info/refs?service=git-receive-pack` + `POST git-receive-pack`), and the upstream latchkey gateway has proxied GitHub's git endpoints as first-class URLs since 2.14.0, converting the stored token to the `x-access-token` basic auth git expects, server-side. Detent ships matching built-in schemas under the `github-git` scope (`github-git-read` for clone/fetch, `github-git-write` for push), already present in `mngr_latchkey`'s services catalog.

- **The push is now fully latchkey**: git pushes to `$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git` with the gateway's two auth headers as one-shot `-c http.extraHeader` options; the credential is injected server-side and no GitHub token ever enters the container. `GH_TOKEN` is out of the publish flow entirely (the skill explicitly forbids falling back to it).
- **Permission-request bodies fixed**: the publish skill's request used a top-level `{scope, permissions, rationale}` body; latchkey requires the four-field `{agent_id, type, payload, rationale}` form. Both requests (`github-rest-api`, `github-git`) now use it.
- **One mngr-side change** (`libs/mngr_latchkey`): both gateway spawn sites (desktop + VPS) now pass `--max-body-size` raised to 512 MiB — the upstream 10 MiB default rejects any full-history push packfile (the template's history is ~30 MiB), and the flag is the only knob (no env var).
- **`/use-inspiration`** fetches private inspiration repos the same way (`github-git-read`), fetching the gateway URL directly instead of persisting a gateway-URL remote; the FCT `latchkey` skill documents the general git-over-gateway pattern.
- **Adversarial-review fixes (same day)**: the `github-rest-api` request asks for `github-read-user` + `github-write-all` (the narrower `github-write-repos` schema does not cover `POST /user/repos`, so the previously-requested grant would 403 the flow's own repo-creation call even after user approval); the API probes pass `-f` (`latchkey curl` exits with curl's code, and the gateway's 403 denial is otherwise exit 0, making the probe vacuous); the push-permission probe handles `"any"` catch-all grants. Known caveat: already-running gateways keep the old 10 MiB body cap until they restart (the desktop gateway restarts with the minds app; a VPS gateway persists until its process exits).

### Design revision: the post-assembly confirmation is a hard gate (2026-07-08)

A live publish ran the §1 scope gate correctly, then — after the worker finished — verified the deterministic gates itself, announced "everything checks out," and pushed in the same turn; the user never saw the final title, description, or thumbnail before the repo existed on their account. §6's old wording ("present the proposal once… then proceed") permitted present-then-proceed. It is now a hard gate mirroring §1: present the final details with the thumbnail embedded, END THE TURN, and run repo-creation + push only on an explicit reply to that message. No earlier approval counts (the scope confirmation, a pre-assembly "go ahead and publish," or approving the GitHub permission requests in the minds app — the final artifacts did not exist yet), and the agent's own gate checks are verification, never confirmation.

### Design revision: scope gate, published-version modifications, and history privacy (2026-07-07)

A live publish laid out its proposal and dispatched the assembly worker in the same turn, declaring the include set "confirmed" without any user reply. Three changes:

- **The scope gate.** §1 now ends with one plain-language message covering what WILL be included, what will NOT be (data, other apps, secrets/config), any published-version modifications, and the proposed (adjustable-later) title/repo — and requires an explicit user reply to that message before any assembly work or worker dispatch. Confirmation is something the user gives, never something the agent declares; the original "publish this" request does not count. Naming intentionally does not block the gate (renames are handled in place).
- **Published-version modifications.** The user can ask for files to be changed, generalized, or stripped in the published snapshot only (secret-cleaned copies, removed personal preferences). They are confirmed at the scope gate, carried in the worker task file, applied by the worker in its isolated worktree (the live mind's files and history stay untouched), re-scanned with the assembly script's secret-token patterns, and recapped in the final chat confirmation.
- **History privacy.** `build_inspiration.sh` used to parent the snapshot commit on the mind's HEAD, publishing the mind's entire commit history — including anything ever committed and later removed, which would have made a "secret-cleaned" file unsafe (the dirty original stayed retrievable from history). The snapshot commit is now parented on `BASE_REF` via `git commit-tree`, so the published history is exactly the public template's history plus the snapshot commits. Verified on a synthetic repo: a committed-then-removed secret in the mind is unreachable from the pushed branch.

### Design revision: confirm the name before dispatch; renames never restart assembly (2026-07-07)

A live publish derived the title itself, launched the assembly worker, and then tore it down and relaunched it when the user renamed the inspiration ("Zenbox"). Two fixes. First, §1 of the publish skill now ends with a hard gate: echo the proposed title, derived repo name, scope, and data inclusion in one message and WAIT for the user's go-ahead before dispatching (§3 repeats the guard), keeping §6 a review rather than a rename. Second, the relaunch itself was unnecessary and the skill now says so explicitly: the worker's name and branch are internal plumbing that appear nowhere in the published repo (the push refspec sends any branch as `main`), so a post-dispatch rename is handled in place — pass the new slug/title to `build_inspiration.sh` if it hasn't run yet, otherwise `git mv` the manifest/thumbnail and fix the front-matter and welcome references in `$WT` (which preserves completed FILL-IN prose and the bespoke SVG; re-running the script under a new slug would instead carry the old-slug files forward as a phantom accumulated inspiration) — never by tearing down the worker.

### Design revision: deterministic `BASE_REF` (2026-07-07)

A live publish from a fresh mind exposed a judgment call in the base-detection rule: with no `update-self:` commits, the documented fallback (first-parent root + bootable walk) resolves to an ancient template commit unrelated to the mind, and the publishing agent correctly diverged by hand to the workspace's creation snapshot, flagging the divergence. The rule is now deterministic: `BASE_REF` = the newest first-parent commit whose subject is a template-state marker — `update-self: ...` (mind updated after creation) or bootstrap's `Initial workspace commit` (always present: `libs/bootstrap` creates it `--allow-empty` at first boot, snapshotting exactly what the workspace started from, including any uncommitted source state a dev-flow clone carried). The first-parent root + bootable pre-check walk remains only as a last resort for repos with no marker (hand-made / pre-bootstrap). Verified against the live mind: the one-liner resolves precisely the commit the agent had chosen by hand.
