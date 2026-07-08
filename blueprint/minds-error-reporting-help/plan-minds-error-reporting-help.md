# Minds error reporting & "get help" flow

## Overview

- Replace the env-var gating of Sentry (`MINDS_SENTRY_ENABLED`, `MINDS_SENTRY_S3_UPLOADS`) with user-facing consent: a standalone consent screen on first launch and persistent settings toggles ("Report unexpected errors", "Include logs"), both defaulting OFF and stored globally per-machine in `MindsConfig` (`~/.minds/config.toml`).
- Add a permanent "get help" button (question-mark-in-circle) to the top bar, with the same unconditional visibility as the inbox button. It opens a modal offering "have an agent help" (default) and "report a bug to imbue", plus a free-text description box.
- Add a "report a bug" flow: a form (most controls hidden under "Advanced") that gathers app/workspace state and submits a manually-tagged Sentry report. All Sentry submission is owned by the outer minds app, fronted by a single shared collector behind both the local form and a new authenticated `/api/v1` route.
- Add a "have an agent help" flow: when in a loaded workspace, spawn a visible `/assist` chat that diagnoses, fixes what it can, and escalates built-in-code issues to imbue. The agent never sends to Sentry itself; it asks the desktop app to pop the report-a-bug modal pre-filled with its diagnosis, so the human reviews, chooses what to attach, and submits through the same path as a manual report.
- Build and test in four phases — (1) consent + settings, (2) report-a-bug + API, (3) in-workspace agent help, (4) out-of-workspace ("launch a new mind to investigate") agent help — but the whole design is planned now.

## Expected behavior

### Consent & settings

- On the very first app launch, before welcome/login, the user sees a standalone consent screen explaining error reporting. "Report unexpected errors" defaults OFF; "Include logs" appears only once "Report unexpected errors" is enabled, and also defaults OFF.
- The same two toggles live permanently in app settings. Changing them takes effect immediately, with no app restart.
- With "Report unexpected errors" OFF, no automatic unexpected errors are sent to Sentry. With it ON but "Include logs" OFF, errors are sent without log/traceback attachments. With both ON, behavior matches today's fully-enabled path (logs/tracebacks attached, subject to the existing prod/staging-only S3 rule).
- Consent state and toggles are global to the machine, independent of which imbue cloud accounts are logged in. The env vars no longer gate this functionality.

### Get help button & modal

- A question-mark button sits in the top bar wherever the inbox button shows (i.e. always). Clicking it opens a modal: "Seems like you're running into a problem — we can help." with two choices and a description input.
- "Have an agent help fix the problem" is selected by default; the alternative is "Report a bug to imbue".

### Report a bug

- Manual reports always work, even when "Report unexpected errors" is OFF (an explicit user action).
- The form shows the user's description plus an "Advanced" disclosure. Always-included basics: minds + mngr version/release, OS. When "Include logs" is OFF, a top-level "Include logs" checkbox appears (separate from Advanced); when ON, logs are included without the extra checkbox.
- Advanced controls what app/workspace state is attached. Minds-app state (system info, resource usage, latchkey status/version, logged-in accounts, config, known workspaces + states, running mngr/minds processes) is available everywhere. Workspace state (running agents + states, service states, resource usage) is offered only when the window is loaded into a workspace.
- Heavy/sensitive items default OFF and must be opted into under Advanced: full FCT code, chat history (options "all" / "current chat" / "none", default "all" only once enabled), and a "remote access" consent flag. "Remote access" is just a recorded flag in the report — no access is provisioned in this PR. Recovery diagnostics are offered when the window is on a workspace's recovery page.
- Submitting synthesizes a Sentry event (not tied to an exception) titled from the description, tagged `manually_submitted`, with the selected state attached/contextualized via the existing S3-attachment mechanism.

### Have an agent help — in a loaded workspace

- Choosing "have an agent help" while the window is in a loaded (non-recovery) workspace creates a new chat there and sends `/assist <description>`. The chat is visible and auto-opens as a tab.
- The `/assist` skill gathers logs and explores the repo, then classifies the issue along two axes: user-created vs built-in code (escalation decision), and forever-claude-template vs `vendor/mngr` code (fixability decision).
- The agent fixes the issue when it lies in user code, in template built-in code (e.g. `system_interface`), or in `vendor/mngr` code in a way that affects how it runs in the container. It gives up (cannot fix) when the fix would require a new outer-app version — i.e. issues in the installed desktop app (`apps/minds`), its bundled plugins (`mngr_forward`, `mngr_latchkey`), or the outer app's vendored mngr.
- Any built-in-code issue is reported to imbue even if the agent fixed it; purely user-created issues are not reported. Built-in vs user classification is by git history (initial template commit, `/update-self` merges, anything under `vendor/`).
- To report, the agent does not submit directly. It POSTs its diagnosis (as a description) to the authenticated minds report route. The effect of that POST is to open the report-a-bug modal in the workspace's window, pre-filled with the description and with "report a bug" pre-selected. The user reviews, chooses what to attach (logs / diagnostics / remote-access flag — same controls and defaults as a manual report), and submits via the normal `/help/report` path. So the human, not the agent, gates every Sentry send, and there is a single submission path.

### Have an agent help — not in a workspace (incl. recovery)

- Choosing "have an agent help" when no workspace is loaded (home/landing or recovery) launches a new mind to investigate, then runs the same `/assist` flow seeded with context describing the broken workspace.
- The new mind helps enrich and clarify the report and submit it (via the same report-modal path: it POSTs its diagnosis to the report route, which pops the pre-filled modal for the user to review and submit), but does not attempt to reach into the other workspace — cross-mind interaction is out of scope for this PR.

## Changes

### Settings, consent, and Sentry gating

- Add persistent global fields to `MindsConfig` for consent-given, "report unexpected errors", and "include logs", plus getters/setters following the existing config pattern.
- Add a standalone consent screen shown on first launch ahead of welcome/login, gated on the consent-given flag; wire it into the startup routing.
- Add the two toggles to the settings UI, with "include logs" revealed only when "report errors" is on.
- Replace the `MINDS_SENTRY_ENABLED` / `MINDS_SENTRY_S3_UPLOADS` env-var checks with reads of the new config, evaluated live so changes apply without restart (Sentry always initializes, but sending and log-attachment are gated on the current setting at send time).

### Get help UI

- Add a question-mark icon and a "get help" titlebar button beside the inbox button in the top bar, with matching unconditional visibility.
- Add the help modal (two radio options + description box) reusing the existing modal component, and the report form with an "Advanced" disclosure and the conditional top-level "include logs" checkbox.

### Report collection & submission

- Add a single shared "report collector + submitter" in the minds app that gathers the basics and the selected app/workspace state (reusing existing sources: build info, `psutil`, latchkey status, session store, config loader, backend resolver / list_agents, recovery probe) and emits a `manually_submitted`-tagged Sentry event with attachments. (Done in phase 2.)
- The authenticated `/api/v1/agents/<id>/report` route (reachable by in-workspace agents via the latchkey `minds-api-proxy`) no longer submits to Sentry directly. Instead it asks the running desktop app to open the report-a-bug modal pre-filled with the agent's description, scoped to that agent's workspace. The human then submits via the existing `/help/report` path, so the app still owns all Sentry sends but a human gates each one. (Phase 2 wired the direct-submit version; phase 3 repurposes it to the modal-open behavior.)
  - Mechanism: an app-level broker (mirroring the existing `SystemInterfaceHealthTracker` on-change-callback pattern) fans the request out to all live chrome-events SSE connections as a new `open_help` event; the Electron shell handles it by opening the help modal (via the existing `openHelp`) in the window currently showing that workspace, carrying the description; the `/help` page pre-fills the description and pre-selects "report a bug" from new query params.
  - Interim latchkey change: the report route is allowed unconditionally for any in-workspace agent (a baseline rule in `mngr_latchkey`), bypassing per-agent permission gating while keeping the bearer-key auth. This is a stopgap pending the broader minds-API-surface latchkey work.
- Determine the "current chat" and workspace context for the window that opened the help modal so the form can scope workspace fields and chat-history options correctly.

### Agent-help flow

- Add an `/assist` skill to the forever-claude-template that performs the gather/classify/fix logic and the built-in-vs-user and template-vs-vendor decisions, with the explicit give-up set (outer `apps/minds`, `mngr_forward`, `mngr_latchkey`, outer app's vendored mngr). The agent can read all of these under `vendor/mngr/` (the template vendors the whole mngr monorepo, including `apps/minds`), so it can inspect outer-app code even though it cannot change the installed desktop app.
- Add a recognizable merge-commit message convention to the `/update-self` skill so the agent can identify built-in code arriving via template updates from git history: force a non-fast-forward merge with a stable subject, `git pull --no-ff --no-edit upstream "$BRANCH" -m "update-self: merge upstream template ($BRANCH)"`. `/assist` then treats a file as built-in if it is under `vendor/`, introduced by the initial template (root) commit, or last touched by a commit reachable from the second parent of an `update-self:` merge.
- When the app initiates agent help in a loaded workspace, the outer desktop app spawns the `/assist` chat by running `mngr create` against the workspace's container host (`--template chat --transfer none`, inheriting `workspace=`/`project=` labels, plus a special auto-open label), with `/assist <description>` as the initial message. Coupling stays at the mngr API/interface level — the app does not call system_interface's HTTP API.
- Update `apps/system_interface` (in the template) to recognize the special auto-open label when it discovers the new agent and auto-open its tab via the existing layout-broadcast `open` op.
- The agent's escalation reuses the report route's new modal-open behavior (above): the `/assist` agent POSTs its diagnosis to the report route, which pops the pre-filled modal for the user to review and submit.
- For the not-in-workspace case, trigger creation of a new mind and run the same `/assist` flow with context about the original workspace, without cross-mind access. (Phase 4.)

## Phase 3 acceptance criteria (in-workspace agent help)

- A POST to `/api/v1/agents/<id>/report` with a description no longer calls the Sentry submitter; it publishes an `open_help` request carrying that description scoped to the agent's workspace. (Unit test asserts the broker is notified and Sentry is not called.)
- The chrome-events SSE stream emits an `open_help` event when the broker is notified, and the Electron shell opens the help modal — pre-filled with the description and with "report a bug" pre-selected — in the window showing that workspace. (Unit test on the SSE payload; the Electron wiring is verified manually.)
- The `/help` page pre-fills the description and pre-selects the report option when the new query params are present. (Unit test rendering the page.)
- An in-workspace agent can reach the report route without a prior per-agent permission grant (latchkey baseline allows it). (Unit test on the baseline permissions.)
- Choosing "have an agent help" in a loaded workspace runs `mngr create` against that workspace's container host with the chat template, the auto-open label, and `/assist <description>` as the initial message. (Unit test on argv assembly + host targeting.)
- `apps/system_interface` auto-opens a tab for a discovered agent carrying the auto-open label. (system_interface unit test.)
- End-to-end (manual, not crystallized): picking "have an agent help" spawns a visible `/assist` tab seeded with the description; when that agent POSTs its diagnosis to the report route, the pre-filled report modal pops in the right window. The LLM-judgment parts of `/assist` (classification, fix-vs-give-up) are verified by manual exercise against a running workspace, not asserted in CI.
