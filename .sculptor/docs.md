# Docs

Specs in this repo describe what the system should do and how it will
be built. They commonly live alongside an implementation plan and are
used to drive concrete code changes.

## Spec Location

- **Path pattern:** `specs/<slug>/<kind>.md`
- **Kinds in use:**
  - `spec.md` — full design spec for new features (e.g. `specs/vps-docker-provider/spec.md`)
  - `concise.md` — short, bulleted summary of a feature (e.g. `specs/vps-docker-provider/concise.md`)
  - `plan.md` — step-by-step plan for a focused, procedural change with concrete deliverables and measurements (e.g. `specs/offload-v0.9.0-history/plan.md`)

Use `plan.md` when the work is primarily a sequence of well-defined
steps (an upgrade, a fix, a refactor) and the user already understands
the "why" — the document should drive iteration. Use `spec.md` when
introducing new architecture or substantial new behavior. Use
`concise.md` as a companion summary.

## Spec Structure

For `plan.md`-style specs, prefer this structure:

- **Goal** — the problem and what success looks like
- **Current State** — what exists today, including the broken behavior
- **Plan** — numbered steps with concrete deliverables; each step
  identifies the files to touch and the verification check for that
  step
- **Test invocation** — concrete commands the user (or agent) can run
  to validate each step
- **Constraints** — what must remain true throughout (e.g. caching
  behavior, supported platforms)
- **Open Questions** — unresolved decisions

For `spec.md`-style specs, prefer this structure:

- **Motivation / Overview** — the problem and the motivation
- **Expected Behavior** — what the system should do, end-to-end
- **Architecture** — relevant diagrams or structural notes
- **Implementation Plan** — file-by-file changes
- **Non-Goals** — what is explicitly out of scope
- **Open Questions** — unresolved decisions or ambiguities

## Conventions

- No emojis anywhere in spec files (see project CLAUDE.md).
- Use bullet points and short sentences; avoid long paragraphs.
- When code paths are referenced, use `path:line` format where
  applicable.
- Specs are committed to the repo and updated as work progresses.

## Reference Docs

- `specs/offload-v0.9.0-history/plan.md` — exemplar plan for an
  offload upgrade with measurement-driven verification
- `specs/vps-docker-provider/spec.md` — exemplar full spec for a new
  feature
- `specs/faster-leased-agent-setup/spec.md` — exemplar spec for a
  cross-cutting refactor with a single-PR implementation plan

## UI Reference

<!--
  No UI work in this repo's typical specs. Frontend work primarily
  lives in apps/minds (Electron desktop client). Update if you start
  spec'ing visual features.
-->

## Mock Conventions

- Show multiple scenarios/states in a single HTML file, each clearly
  labeled with a short description of what state it demonstrates.
- Render mocks inside a realistic app window/chrome.
- Match the app's existing visual style (Electron desktop client).
- Use realistic sample data.
