# Plan: Minds Inspirations (publish & adapt)

## Overview

- Adds an "inspirations" concept to minds: a way for a running mind to **publish** a reusable snapshot of the apps/features it built, and for another mind to **adapt** an existing inspiration into itself.
- Three deliverables, all primarily in the **forever-claude-template (FCT)** repo (edited via a `.external_worktrees/forever-claude-template` worktree, per CLAUDE.md), plus one supporting change in the **minds** app:
  - Agent-awareness: an "Inspirations" section in the FCT `CLAUDE.md` so the agent knows what inspirations are, that a publish skill exists, and when to offer it.
  - `/publish-inspiration` skill (FCT): builds a clean, shareable repo from the current mind and pushes it to GitHub.
  - `/adapt-inspiration` skill (FCT): merges an existing inspiration (by git URL) into the current mind and fills in its holes.
- One **minds** desktop-client change: a new request-inbox type that renders an "Inspiration" confirmation popup (richer than the existing grant/deny dialogs) so the user can confirm/edit the title, description, thumbnail, and repo settings before publishing.
- Key design decisions locked during planning:
  - Inspirations build on a **clean FCT base** (clone the in-VM FCT at `main` as of the last remote sync) with only the user-selected app/feature paths layered on top — no experimental cruft, no user data, no secrets.
  - The published repo keeps an **upstream link to FCT** so it can still pull template/runtime updates.
  - A single inspiration repo can **accumulate multiple inspirations** over time (one `inspiration-<name>.md` manifest per inspiration at the repo root); each inspiration can contain multiple apps.
  - The manifest is a **worksheet**: it records what the inspiration is, its holes/permissions (freeform prose), and how it was later adapted.
  - Scope is intentionally bounded: no catalog/discovery UI for browsing inspirations (separate, later effort).

## Expected behavior

### Agent-awareness (CLAUDE.md)

- The agent reads an "Inspirations" section in `CLAUDE.md` on startup and understands: what an inspiration is, that `/publish-inspiration` and `/adapt-inspiration` exist, and when to use them.
- After the agent builds something meaningful that the user is happy with, it proactively offers to publish — as a lightweight nudge over whatever chat channel the user is on (Telegram or minds chat), **not** the popup.

### Publishing (`/publish-inspiration`)

- The agent asks the user a few setup questions in chat: what to call the inspiration, which apps/features to include, and whether any data should be included. It does **not** enumerate specific files to the user.
- By default, **no user data** is included — only app/UI/code — and data is included only when the user explicitly asks.
- The skill clones the in-VM FCT repo at `main` (last synced with remote) into a temp working dir as the clean base, preserving the upstream link to FCT.
- The agent diffs the live working tree against `main`, selects the file/paths backing the chosen apps/features, copies them onto the clean base, and makes a single commit. Secrets (`.env`, etc.) are stripped.
- The agent generates an `inspiration-<name>.md` manifest at the repo root: YAML front-matter (title, description, thumbnail) plus a markdown body explaining what it is, its holes, and the permissions it may need (freeform prose).
- The thumbnail is, for now, an **SVG icon the agent generates** (mock data only, never real user data); it can later be replaced with a real screenshot.
- The skill rewrites the FCT `/welcome` to be specific to the most-recently-published inspiration's adaptation flow.
- The agent raises an **Inspiration confirmation popup** in the minds UI (a new request-inbox type). The popup shows the proposed title, description, SVG thumbnail (with accept/redo), and repo settings (name, public/private), and lets the user confirm/edit them. The skill submits the request and waits for the user's response.
- On confirmation, `/publish-inspiration` always creates a **new** private (by default; public if the popup says so) GitHub repo via `gh repo create` under the user's account using the in-VM `GH_TOKEN`, and pushes.
- Before pushing, the agent **smoke-checks that the mind boots from the clean base**. The selected apps need not fully function — holes are expected.
- If repo creation fails (name taken, token missing/insufficient scope), the agent reports the error in chat and re-opens the popup for a new name, keeping the local commit intact.
- Publishing a mind that already holds accumulated inspirations carries **all** existing `inspiration-*.md` manifests and their apps into the new repo, alongside the newly-published one.

### Adapting (`/adapt-inspiration`)

- Two entry points:
  1. **Template path**: a new mind is created with an inspiration repo as its template. On startup, the rewritten `/welcome` drives the adaptation. The agent defaults to adapting the **latest** inspiration; older manifests are primarily reference (likely already adapted). The agent may ask the user what they want to do.
  2. **Merge path**: an existing mind (built from a different template) runs `/adapt-inspiration <git-url>`. The skill `git remote add`s the inspiration and merges/subtrees it in.
- After bringing in the inspiration, the agent reads the relevant `inspiration-<name>.md` manifest, asks the user in chat how they want to adapt it, and works through the manifest's holes interactively (e.g. swapping Slack for email).
- Merged-in `inspiration-<name>.md` manifests stay at the repo root and accumulate alongside existing ones.
- As it adapts, the agent appends a dated "how it was adapted" section to the relevant manifest so the file captures its own history (the worksheet behavior).

### Minds popup interaction (new behavior)

- The new Inspiration request appears in the existing inbox/requests panel (auto-open behavior consistent with other requests).
- Unlike existing grant/deny requests, this dialog collects **edited field values** (title, description, repo name, visibility, thumbnail accept/redo) and returns them to the agent.

## Implementation plan

> Files are grouped by repo. FCT files are edited in `.external_worktrees/forever-claude-template` on the same branch name as this repo's working branch (per CLAUDE.md), committed there. Minds files are in this monorepo under `apps/minds`.

### Minds app (`apps/minds/imbue/minds/desktop_client`)

- `request_events.py`
  - Add `RequestType.INSPIRATION_PUBLISH` (new enum member).
  - Add `InspirationPublishRequestEvent(RequestEvent)` carrying the agent's proposal: `proposed_title`, `proposed_description`, `proposed_repo_name`, `proposed_visibility` (`PRIVATE`/`PUBLIC`), `thumbnail_svg` (or a path/ref to it), `manifest_summary`/`rationale`, and the inspiration `name`/slug.
  - Extend `RequestResponseEvent` with **optional, defaulted** edited-value fields (`final_title`, `final_description`, `final_repo_name`, `final_visibility`, `thumbnail_action` = accept/redo). Keeping it the same response type leaves `get_pending_requests`/event-sourcing and the legacy-field stripping untouched; the new fields are simply absent on grant/deny responses for other request types.
  - Add a `create_inspiration_publish_request_event(...)` factory mirroring the existing `create_latchkey_*_request_event` factories.
  - Extend `parse_request_event` to dispatch `INSPIRATION_PUBLISH` to the new model.
- `latchkey/handlers/` (the per-request-type handler home; despite the package name, it is the canonical dispatch target the thin `app.py` route layer delegates to)
  - Add `inspiration.py`: renders the Inspiration confirmation dialog, validates the submitted form, writes the response event (with edited values), and notifies the waiting agent. Reuse `MngrMessageSender` (the same helper the predefined/file-sharing handlers use to notify the agent via `mngr message`). The notification **embeds the user's final field values in the `mngr message` payload**, which the `/publish-inspiration` skill parses to proceed — matching how permissions notify today, so no in-VM polling of response files is needed.
  - Add `inspiration_test.py` (unit tests for render + submit + response-event emission).
- `latchkey/handlers/templates.py`
  - Add `render_inspiration_publish_dialog(...)` producing the form (title/description inputs, repo name, public/private toggle, SVG thumbnail preview with accept/redo).
- `templates/pages/` and `templates.py`
  - Add an `Inspiration.jinja` dialog page (or extend the inbox card rendering) consistent with the existing `Latchkey*Permission.jinja` pages.
  - Wire `_build_inbox_cards` / `_build_requests_payload` to render the new request type (badge/accent like other requests).
- `app.py`
  - Add the thin route(s) for rendering and submitting the inspiration dialog, dispatching by request type to `handlers/inspiration.py` (mirror the existing predefined/file-sharing/workspace/accounts dispatch).
- `apps/minds/changelog/<branch>.md`
  - New changelog entry describing the Inspiration popup / request type.

### Forever-claude-template repo (FCT)

- `CLAUDE.md`
  - Add an "Inspirations" section: what inspirations are, that `/publish-inspiration` and `/adapt-inspiration` exist, when to offer publishing (after building something meaningful the user likes), and the proactive-nudge channel guidance.
- `skills/publish-inspiration/SKILL.md` (new)
  - Implements the publish flow: setup Q&A, clean-base clone of in-VM FCT at `main`, file/path-level selection + single commit, secret stripping, `inspiration-<name>.md` generation (front-matter + body + SVG thumbnail), `/welcome` rewrite, raise the minds Inspiration popup and wait for the edited values, boot smoke-check, `gh repo create` + push, failure handling (re-open popup), and accumulation handling (carry existing manifests/apps forward).
  - May include a small helper script (e.g. `skills/publish-inspiration/scripts/build_inspiration.sh`) for the git/clone/commit/push mechanics, kept self-contained in the FCT (the dev `create-new-mind-repo` justfile recipe is **not** available inside the VM).
- `skills/adapt-inspiration/SKILL.md` (new)
  - Implements the merge path: `git remote add` + **`git subtree`** of the inspiration URL (preserves provenance and coexists with the upstream-FCT link), manifest reading, interactive hole-filling Q&A, manifest worksheet append (dated "how it was adapted"), and accumulation (manifests stay at root).
  - Conflict handling: when a second inspiration collides with an existing app dir/file, the agent figures it out and resolves interactively, surfacing the collision to the user as a "hole" — always in non-technical language, asking the user only if it is unsure.
- `welcome` skill (existing in FCT)
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

- **Phase 1 — Minds request type + popup (testable in isolation)**
  - Add `INSPIRATION_PUBLISH` request type, request/response models, factory, and parsing in `request_events.py`.
  - Add `handlers/inspiration.py`, `render_inspiration_publish_dialog`, `Inspiration.jinja`, route wiring in `app.py`, and inbox-card rendering.
  - Result: a hand-written request event JSONL file renders as an Inspiration popup; submitting it writes a response event with edited values and notifies via `mngr message`. Unit + integration tested. Changelog added.

- **Phase 2 — FCT `/publish-inspiration` skill (happy path)**
  - Write the skill + helper script: setup Q&A, clean-base clone, file/path selection + commit, secret stripping, manifest + SVG thumbnail generation, `/welcome` rewrite, `gh repo create` + push.
  - Defer the popup round-trip to a simple chat confirmation initially; swap in the Phase-1 popup once both are present.
  - Result: a mind can publish a single-app inspiration to a fresh private repo. Manually verified end-to-end.

- **Phase 3 — Wire publish to the popup + robustness**
  - Connect `/publish-inspiration` to the Phase-1 popup (raise request, wait for edited values), thumbnail accept/redo, boot smoke-check, repo-create failure handling, and accumulation (carry existing manifests forward).
  - Add the CLAUDE.md "Inspirations" section and the proactive-publish nudge guidance.

- **Phase 4 — FCT `/adapt-inspiration` skill (both paths)**
  - Merge path: `git remote add` + merge/subtree, manifest read, interactive hole-filling, worksheet append.
  - Template path: rewrite `/welcome` output so a new mind built from an inspiration repo adapts the latest inspiration on startup; surface older manifests as reference.
  - Result: a mind can adapt an existing inspiration both by URL and by being created from an inspiration repo.

## Testing strategy

### Minds (Python — unit + integration, `apps/minds`)

- Unit tests for `request_events.py`: round-trip serialize/parse of `InspirationPublishRequestEvent`; the extended response carrying edited values; `get_pending_requests` still resolves the new type; legacy-field stripping unaffected.
- Unit tests for `handlers/inspiration.py` + `render_inspiration_publish_dialog`: dialog renders proposed fields; submit with edited values produces the correct response event; invalid/empty submissions are rejected; `mngr message` notification is emitted (using the existing message-sender test seam, not monkeypatch).
- Integration test: write a request event to a temp `events/requests/events.jsonl`, drive the route to render + submit, assert the response event is appended and the inbox no longer lists it as pending. Reuse shared fixtures (`temp_host_dir`, desktop-client conftest) rather than new ones.
- Edge cases: redelivered request idempotency (same `event_id`); public vs private selection; thumbnail redo path; cancel/deny.

### FCT (markdown skills — manual verification)

- Skills are markdown; verify manually by exercising the flow as a real user inside a running mind (per minds-dev-workflow), not via pytest:
  - Publish a single-app inspiration; confirm the new repo is clean (no `.env`/user data), boots from the clean base, has a valid `inspiration-<name>.md` + SVG thumbnail, and a rewritten `/welcome`.
  - Publish from a mind with an existing accumulated inspiration; confirm both manifests/apps are carried forward.
  - Adapt by URL into a different mind; confirm merge, hole-filling, and the dated worksheet append.
  - Create a new mind from an inspiration repo; confirm `/welcome` drives adaptation of the latest inspiration.
  - Failure: occupied repo name re-opens the popup; missing `GH_TOKEN` reports a clear chat error and leaves the local commit intact.

### Edge cases to cover explicitly

- No diff vs `main` (nothing to publish) — clear message, no empty repo.
- Selected apps include secret-bearing files — stripped, with a note to the user.
- Boot smoke-check fails outright (base doesn't boot) — abort before creating the repo.

## Open questions

- **`/welcome` structure.** The publish skill must rewrite `/welcome` deterministically. Need to confirm the current FCT `/welcome` skill's structure (the template isn't checked out locally) so the rewrite targets a stable region rather than regenerating the whole file. Resolve by inspecting the FCT `/welcome` skill in a `.external_worktrees/forever-claude-template` worktree before Phase 3.

### Resolved during planning

- **Edited-values delivery.** The handler embeds the user's final field values in the `mngr message` payload the `/publish-inspiration` skill parses (matches how permissions notify today); no in-VM response-file polling.
- **Response schema.** Extend `RequestResponseEvent` with optional, defaulted edited-value fields rather than a dedicated response type, keeping event-sourcing/pending logic untouched.
- **Merge mechanics.** `/adapt-inspiration` uses `git subtree`; collisions between accumulated inspirations are surfaced to the user as holes and resolved interactively, in non-technical language.
- **Upstream link.** Recorded both as a `parent.toml`-style pointer and a git remote.
- **Secret denylist.** Baseline is the repo's existing `.gitignore` set (`.env*`, `.runtime/`, `memory/`, etc.); the agent additionally reasons about any other secrets in the selected changes and excludes them.
- **Thumbnail storage.** `inspiration-<name>.svg` next to the manifest, referenced by relative path in the front-matter `thumbnail` key.

✓ Explore  ✓ Plan  ● Write  ○ Refine