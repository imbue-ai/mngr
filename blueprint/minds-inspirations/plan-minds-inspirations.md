# Plan: Minds Inspirations (publish & adapt)

## Overview

- Adds an "inspirations" concept: a way for a running mind to **publish** a reusable snapshot of the apps/features it built, and for another mind to **adapt** an existing inspiration into itself.
- All work lands in the **forever-claude-template (FCT)** repo (edited via a `.external_worktrees/forever-claude-template` worktree, per CLAUDE.md). There are **no `apps/minds` (desktop-client) changes** — the publish UI lives in the agent's in-container web UI (`system_interface`), not the minds request-inbox.
- Deliverables:
  - Agent-awareness: an "Inspirations" section in the FCT `CLAUDE.md` so the agent knows what inspirations are and **when to offer to publish one** (it already knows which skills exist — no need to enumerate them).
  - `/publish-inspiration` skill (FCT): assembles a clean, shareable repo from the current mind (via a `launch-task` sub-agent on an isolated worktree) and pushes it to GitHub.
  - `/adapt-inspiration` skill (FCT): merges an existing inspiration (by git URL) into the current mind and fills in its holes.
  - **system_interface publish popup** (FCT `apps/system_interface`): a small in-UI box with editable inputs (title, description, repo name, visibility, thumbnail) the user confirms before publishing — built directly into the system_interface, not as a minds request type.
  - **system_interface GitHub-login modal** (FCT `apps/system_interface`): a one-click GitHub login mirroring the existing Claude-login modal, for users without an in-VM `GH_TOKEN`.
- Key design decisions locked during planning:
  - The inspiration is assembled by a **`launch-task` sub-agent on its own git worktree** (`mngr/<slug>`), so the live mind is untouched during assembly. The worktree is reduced to a **clean FCT base** (the FCT upstream `main` via `parent.toml`) plus only the user-selected app/feature paths — no experimental cruft, no user data, no secrets.
  - The published repo keeps an **upstream link to FCT** so it can still pull template/runtime updates.
  - A single inspiration repo can **accumulate multiple inspirations** over time (one `inspiration-<name>.md` manifest per inspiration at the repo root); each inspiration can contain multiple apps.
  - The manifest is a **worksheet**: it records what the inspiration is, its holes/permissions (freeform prose), and how it was later adapted.
  - Scope is intentionally bounded: no catalog/discovery UI for browsing inspirations (separate, later effort).

## Expected behavior

### Agent-awareness (CLAUDE.md)

- The agent reads an "Inspirations" section in `CLAUDE.md` on startup and understands what an inspiration is and **when to offer to publish one**. The section does not enumerate the skills — the agent already knows which skills exist.
- When to offer (once the product is relatively finished and the user seems happy), two cues:
  - The agent can ask whether the user is happy or wants any more changes; if the user says no more changes, offer to publish an inspiration.
  - Or, when the user spontaneously shows excitement/joy about their app(s), offer then.
- The offer is a lightweight nudge over whatever chat channel the user is on (Telegram or minds chat), **not** the popup (the popup only appears once publishing is underway).

### Publishing (`/publish-inspiration`)

- The agent (the primary mind) asks the user a few setup questions in chat: what to call the inspiration, which apps/features to include, and whether any data should be included. It does **not** enumerate specific files to the user.
- By default, **no user data** is included — only app/UI/code — and data is included only when the user explicitly asks.
- The agent delegates the repo **assembly** to a sub-agent via the existing **`launch-task`** skill, which `mngr create`s a worker on an isolated worktree (`mngr/<slug>`). The live mind keeps running untouched during assembly.
- In its worktree, the worker establishes a **clean FCT base** (checks out the FCT upstream `main` recorded in `parent.toml`), copies in only the file/paths backing the chosen apps/features, strips secrets, and makes a single commit. The upstream link to FCT is preserved.
- The worker generates an `inspiration-<name>.md` manifest at the repo root: YAML front-matter (title, description, thumbnail) plus a markdown body explaining what it is, its holes, and the permissions it may need (freeform prose).
- The thumbnail is, for now, an **SVG icon the agent generates** (mock data only, never real user data); it can later be replaced with a real screenshot.
- The worker rewrites the FCT `/welcome` stable region to be specific to the most-recently-published inspiration, runs the **boot smoke-check** (the mind boots from the clean base; selected apps need not fully function — holes are expected), and reports back. The lead proxies any mid-flight `question` gates to the user and merges the worker's branch back on `done`.
- The agent raises the **publish popup directly in the system_interface web UI** (not a minds request type): a small box pre-filled with the proposed title, description, SVG thumbnail, and repo settings (name, private/public), with editable inputs and a Publish button. The skill waits for the user's submitted values.
- Before pushing, if `gh auth status` fails (no in-VM GitHub credential), the agent surfaces the **GitHub-login modal** in the system_interface so the user can log in with one click. The login configures `gh`'s credential store + git credential helper in place, so the **already-running agent can push immediately — no agent restart** (the token is only needed at push time, not in the process env at startup).
- On confirmation, `/publish-inspiration` always creates a **new** GitHub repo (private by default; public if the popup says so) via `gh repo create` under the user's account using `GH_TOKEN`, and pushes the assembled branch.
- If repo creation fails (name taken, token insufficient), the agent reports it and re-opens the publish popup for a new name, keeping the assembled commit intact.
- Publishing a mind that already holds accumulated inspirations carries **all** existing `inspiration-*.md` manifests and their apps into the new repo, alongside the newly-published one.

### Adapting (`/adapt-inspiration`)

- Two entry points:
  1. **Template path**: a new mind is created with an inspiration repo as its template. On startup, the rewritten `/welcome` drives the adaptation. The agent defaults to adapting the **latest** inspiration; older manifests are primarily reference (likely already adapted). The agent may ask the user what they want to do.
  2. **Merge path**: an existing mind (built from a different template) runs `/adapt-inspiration <git-url>`. The skill `git remote add`s the inspiration and merges/subtrees it in.
- After bringing in the inspiration, the agent reads the relevant `inspiration-<name>.md` manifest, asks the user in chat how they want to adapt it, and works through the manifest's holes interactively (e.g. swapping Slack for email).
- Merged-in `inspiration-<name>.md` manifests stay at the repo root and accumulate alongside existing ones.
- As it adapts, the agent appends a dated "how it was adapted" section to the relevant manifest so the file captures its own history (the worksheet behavior).

### system_interface UI interaction (new behavior)

- **Publish popup**: the skill posts the proposed fields to the system_interface backend, which pushes an event over the existing SSE/WebSocket channel; the frontend opens the publish box pre-filled. The user edits the fields and clicks Publish; the frontend POSTs the edited values, the backend writes them to a response file, and the skill polls that file for the result (mirroring how `launch-task` polls a worker's report file).
- **GitHub-login modal**: mirrors the Claude-login modal's UI/endpoints, but **not** its restart step. The user logs in via `gh auth login --web` (or pastes a PAT); the backend completes the `gh` login so the credential persists to gh's store + git credential helper. The running agent's next `gh repo create` / `git push` uses it directly — **no agent restart** (unlike the Claude API-key flow, the publish skill only needs the credential at push time, not in its process env at startup).

## Implementation plan

> All files are in the **forever-claude-template (FCT)** repo, edited via a `.external_worktrees/forever-claude-template` worktree on the same branch name as this repo's working branch (per CLAUDE.md), and committed there. There are **no `apps/minds` changes**.

### system_interface — publish popup (`apps/system_interface`)

Backend (`apps/system_interface/imbue/system_interface/`):
- `inspiration_endpoints.py` (new): HTTP routes mirroring `claude_auth_endpoints.py`:
  - `POST /api/inspiration/publish-request` — the skill posts the proposed fields (title, description, repo name, visibility, thumbnail SVG, inspiration slug); the backend records the pending request and emits an event so the frontend opens the box.
  - `POST /api/inspiration/publish-confirm` — the frontend posts the user's edited values; the backend persists them to the response file the skill polls.
  - `POST /api/inspiration/abort`, `GET /api/inspiration/status`.
- `inspiration.py` (new): business logic — holds the pending request, writes the response to `runtime/inspiration/publish-response.json` (the skill's poll target), mirroring the request/response file pattern used elsewhere.
- `models.py`: add Pydantic request/response models for the publish payloads.
- `server.py`: register the new routes.

Frontend (`apps/system_interface/frontend/src/`):
- `views/InspirationPublishModal.ts` (new): a Mithril modal modeled on `CreateAgentModal.ts` — "a nice box" with a title input, description textarea, repo-name input, private/public toggle, an SVG thumbnail preview, and a Publish button. Submitting POSTs to `/api/inspiration/publish-confirm`.
- `models/InspirationPublish.ts` (new): module-level open/close state + the proposed fields, mirroring `models/ClaudeAuth.ts`.
- `models/StreamingMessage.ts`: open the publish box when the inspiration-publish event arrives over SSE (mirror `openLoginModalIfAuthError`).
- `views/App.ts`: render the modal conditionally (mirror the `ClaudeLoginModal` line).

### system_interface — GitHub-login modal (`apps/system_interface`)

Backend:
- `github_auth_endpoints.py` (new), `github_auth.py` (new), `models.py`: mirror the Claude-auth modules. Endpoints: `POST /api/github-auth/start` (spawn `gh auth login --web` via pexpect; return the device code + verification URL), `POST /api/github-auth/submit-code` (device-flow completion) and/or `POST /api/github-auth/submit-raw-token` (paste a PAT), `POST /api/github-auth/abort`, `GET /api/github-auth/status` (`gh auth status`).
- On success: persist the credential via `gh` (`gh auth login --with-token` for a pasted PAT, or the completed web/device flow), which configures gh's store + git credential helper. **No agent restart** — the publish skill's `gh repo create` / `git push` in the already-running agent picks the credential up at push time. (Optionally also write `GH_TOKEN` to `$MNGR_HOST_DIR/env` for future agents, but that is not required for the current publish.)

Frontend:
- `views/GitHubLoginModal.ts` (new): mirror `ClaudeLoginModal.ts` — a single-provider GitHub login with two paths (web/device login, or paste a token).
- `models/GitHubAuth.ts` (new): mirror `models/ClaudeAuth.ts`.
- `views/App.ts`: render the GitHub modal alongside the Claude one.
- Trigger: the publish flow opens it when `GH_TOKEN` is missing (via an `inspiration_endpoints` event, or a `github_auth_required` marker added to the `claude_auth_patterns.py`-style transcript detection).

### system_interface changelog
- New entry in `apps/system_interface/changelog/` describing the publish popup + GitHub-login modal.

### Forever-claude-template skills + docs (FCT)

- `CLAUDE.md`
  - Add an "Inspirations" section: what inspirations are and **when to offer to publish** — do not list the skills (the agent already knows them). Offer when the product is relatively finished and the user is happy: either after asking "are you happy, or want any more changes?" and the user wants none, or when the user spontaneously shows excitement about their app(s). The offer is a lightweight nudge over the user's current chat channel, not the popup.
- `.agents/skills/publish-inspiration/SKILL.md` (new)
  - Implements the publish flow: setup Q&A; **delegate assembly to a `launch-task` worker** (write the task file, `create_worker.py launch --template worker`, background `await` for the report, proxy `question` gates, merge the worker's `mngr/<slug>` branch back); the worker establishes the clean FCT base (upstream `main` from `parent.toml`), does file/path-level selection + single commit, secret stripping, `inspiration-<name>.md` + SVG thumbnail generation, `/welcome` stable-region rewrite, and the boot smoke-check; then raise the **system_interface publish popup** and wait for the edited values; ensure `GH_TOKEN` (raising the **GitHub-login modal** if missing); `gh repo create` + push; failure handling (re-open popup); and accumulation (carry existing manifests/apps forward).
  - May include a helper script (e.g. `.agents/skills/publish-inspiration/scripts/build_inspiration.sh`) for the git assembly the worker runs, kept self-contained in the FCT (the dev `create-new-mind-repo` recipe is **not** available inside the VM).
- `.agents/skills/adapt-inspiration/SKILL.md` (new)
  - Implements the merge path: `git remote add` + **`git subtree`** of the inspiration URL (preserves provenance and coexists with the upstream-FCT link), manifest reading, interactive hole-filling Q&A, manifest worksheet append (dated "how it was adapted"), and accumulation (manifests stay at root).
  - Conflict handling: when a second inspiration collides with an existing app dir/file, the agent figures it out and resolves interactively, surfacing the collision to the user as a "hole" — always in non-technical language, asking the user only if it is unsure.
- `.agents/skills/welcome/SKILL.md` (existing in FCT)
  - Updated by `/publish-inspiration` at publish time to reflect the latest inspiration. The plan adds the rewrite logic to the publish skill; the welcome skill itself needs a stable, templated structure the publish skill can target.
- Manifest convention
  - Define the `inspiration-<name>.md` format (front-matter keys: `title`, `description`, `thumbnail`; body sections: What it is, Apps included, Holes, Permissions it may need, Adaptation history).
  - The thumbnail is stored as `inspiration-<name>.svg` next to the manifest at the repo root; the front-matter `thumbnail` key holds its relative path.
- Upstream link
  - The published repo records its FCT upstream **both** as a `parent.toml`-style pointer (per FCT v2's upstream convention) **and** as a git remote, so template-path minds and `/adapt-inspiration` can pull FCT template/runtime updates.
- FCT changelog
  - New entry per FCT changelog conventions describing the two skills + CLAUDE.md awareness.

### Cross-cutting

- Naming: the repo name and `inspiration-<name>.md` slug both derive from a slug of the user's title; the popup can override the repo name.
- Secrets: start from the repo's existing reasonable defaults (the `.gitignore` set — `.env*`, `.runtime/`, `memory/`, etc.) as the baseline denylist, and have the agent actively reason about whether any other secrets are present in the selected changes (it should always be thinking about all changes), excluding anything it identifies.

## Implementation phases

- **Phase 1 — system_interface publish popup (testable in isolation)**
  - Backend: `inspiration_endpoints.py` + `inspiration.py` + `models.py` + `server.py` wiring (publish-request/confirm/abort/status + the response-file handshake).
  - Frontend: `InspirationPublishModal.ts`, `models/InspirationPublish.ts`, `App.ts` + `StreamingMessage.ts` wiring.
  - Result: posting a publish-request opens the box pre-filled; submitting writes the response file with edited values. Backend pytest + manual UI check.

- **Phase 2 — system_interface GitHub-login modal**
  - Backend `github_auth_*` + frontend `GitHubLoginModal.ts` / `models/GitHubAuth.ts`, mirroring the Claude-auth modules; persist the credential via `gh` (store + git credential helper), **no agent restart**.
  - Result: a user without `GH_TOKEN` can log in from the UI and the token reaches the agent.

- **Phase 3 — FCT `/publish-inspiration` skill (happy path, launch-task)**
  - Write the skill + helper script: setup Q&A, launch-task delegation, clean-base assembly, manifest + thumbnail, `/welcome` rewrite, boot smoke-check, `gh repo create` + push.
  - Wire to the Phase-1 popup and the Phase-2 GitHub-login modal. Add the `CLAUDE.md` "Inspirations" section + proactive-nudge guidance.
  - Result: a mind can publish a single-app inspiration to a fresh private repo. Manually verified end-to-end.

- **Phase 4 — FCT `/adapt-inspiration` skill (both paths)**
  - Merge path: `git remote add` + `git subtree`, manifest read, interactive hole-filling, worksheet append.
  - Template path: rewrite `/welcome` so a new mind built from an inspiration repo adapts the latest inspiration on startup; surface older manifests as reference.
  - Result: a mind can adapt an existing inspiration both by URL and by being created from an inspiration repo.

## Testing strategy

### system_interface backend (Python — pytest)

- Unit/integration tests for `inspiration_endpoints.py` + `inspiration.py` (mirror the existing `claude_auth` tests): publish-request records the pending request and emits the event; publish-confirm writes the response file with the edited values; abort/status behave; malformed payloads are rejected.
- Unit/integration tests for `github_auth_endpoints.py` + `github_auth.py`: start spawns the login subprocess (mock pexpect, as the Claude-auth tests do); submit-token / submit-raw-token completes the `gh` login (persists the credential) **without restarting agents**; status reflects `gh auth status`. Reuse the Claude-auth tests' subprocess/DI seams; do not monkeypatch.

### system_interface frontend (Mithril/TS)

- If the frontend has a test setup, add component tests for `InspirationPublishModal` (renders proposed fields, POSTs edited values) and `GitHubLoginModal` (mirrors the Claude modal). Otherwise verify manually in a running mind.

### FCT skills (markdown — manual verification)

- Verify manually by exercising the flow inside a running mind (per minds-dev-workflow), not via pytest:
  - Publish a single-app inspiration via the launch-task worker; confirm the worktree is isolated, the new repo is clean (no `.env`/user data), boots from the clean base, has a valid `inspiration-<name>.md` + SVG thumbnail and a rewritten `/welcome`, and the publish popup round-trips edited values.
  - Publish from a mind without `GH_TOKEN`; confirm the GitHub-login modal lets the user log in and the push then succeeds.
  - Publish from a mind with an existing accumulated inspiration; confirm both manifests/apps are carried forward.
  - Adapt by URL and via the template path; confirm merge, hole-filling, and the dated worksheet append.

### Edge cases to cover explicitly

- No diff vs `main` (nothing to publish) — clear message, no empty repo.
- Selected apps include secret-bearing files — stripped, with a note to the user.
- Boot smoke-check fails outright (base doesn't boot) — abort before creating the repo.

## Open questions

- **Lead vs. worker division for popup + push.** Whether the `launch-task` worker raises the publish popup and pushes, or only assembles + smoke-checks in isolation while the lead (which owns the user conversation) raises the popup, handles GitHub login, and pushes. Leaning toward the latter; confirm.
- **Publish-popup transport.** The exact channel skill → system_interface (a `POST /api/inspiration/publish-request` vs. a watched `runtime/inspiration/*.json` file) and back (response-file polling vs. an SSE-driven `mngr message`). Mirror whatever the system_interface already standardizes on (the Claude-auth endpoints + SSE event + a polled response file).
- **GitHub login flow.** `gh auth login --web` device flow (no PAT needed, but requires a device-code paste-back UI) vs. a raw-PAT paste (simplest, mirrors the Claude API-key path). Likely support both, default to web/device.
- **Clean-base mechanism in the worker.** How the worker reduces its worktree to the FCT upstream `main` baseline (fresh/orphan branch from `parent.toml`'s upstream vs. a fresh clone) and how the selected app paths are conveyed to it (launch-task `source_artifacts_dir`).
- **`/welcome` rewrite target.** Confirm the exact stable region/markers in `.agents/skills/welcome/SKILL.md` the publish skill rewrites (now that FCT is available to inspect).

### Resolved during planning

- **Publish UI location.** Built directly into the FCT `system_interface` web UI (a new modal + endpoints), **not** a minds desktop-client request type. No `apps/minds` changes.
- **Clone/assembly mechanism.** Delegated to a `launch-task` sub-agent on an isolated worktree (`mngr/<slug>`), rather than a hand-rolled temp clone in the publish skill.
- **GitHub auth.** A new system_interface GitHub-login modal mirroring the Claude-login modal's UI/endpoints; persists the credential via `gh` (store + git credential helper) so the running agent can push immediately — **no agent restart** (the credential is only needed at `git push` time, not in the process env at startup).
- **Merge mechanics.** `/adapt-inspiration` uses `git subtree`; collisions between accumulated inspirations are surfaced to the user as holes and resolved interactively, in non-technical language.
- **Upstream link.** Recorded both as a `parent.toml`-style pointer and a git remote.
- **Secret denylist.** Baseline is the repo's existing `.gitignore` set (`.env*`, `.runtime/`, `memory/`, etc.); the agent additionally reasons about any other secrets in the selected changes and excludes them.
- **Thumbnail storage.** `inspiration-<name>.svg` next to the manifest, referenced by relative path in the front-matter `thumbnail` key.