# Minds error reporting & "get help" flow

## Overview

- Replace the env-var gating of Sentry (`MINDS_SENTRY_ENABLED`, `MINDS_SENTRY_S3_UPLOADS`) with user-facing consent: a standalone consent screen on first launch and persistent settings toggles ("Report unexpected errors", "Include logs"), both defaulting OFF and stored globally per-machine in `MindsConfig` (`~/.minds/config.toml`).
- Add a permanent "get help" button (question-mark-in-circle) to the top bar, with the same unconditional visibility as the inbox button. It opens a modal offering "have an agent help" (default) and "report a bug to imbue", plus a free-text description box.
- Add a "report a bug" flow: a form (most controls hidden under "Advanced") that gathers app/workspace state and submits a manually-tagged Sentry report. All Sentry submission is owned by the outer minds app, fronted by a single shared collector behind both the local form and a new authenticated `/api/v1` route.
- Add a "have an agent help" flow: when in a loaded workspace, spawn a visible `/assist` chat that diagnoses, fixes what it can, and escalates built-in-code issues to imbue — surfacing the escalation as a permission request in the existing inbox ("agent X is requesting to submit a bug report to imbue") that the user approves.
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
- To report, the agent does not submit directly. It raises a permission request that appears in the user's inbox — "agent X is requesting to submit a bug report to imbue". On approval, the outer app gathers state (conservative defaults: no full code / "all" chat / remote access unless the user opted in) and sends the Sentry report.

### Have an agent help — not in a workspace (incl. recovery)

- Choosing "have an agent help" when no workspace is loaded (home/landing or recovery) launches a new mind to investigate, then runs the same `/assist` flow seeded with context describing the broken workspace.
- The new mind helps enrich and clarify the report and submit it (via the same inbox-approval path), but does not attempt to reach into the other workspace — cross-mind interaction is out of scope for this PR.

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

- Add a single shared "report collector + submitter" in the minds app that gathers the basics and the selected app/workspace state (reusing existing sources: build info, `psutil`, latchkey status, session store, config loader, backend resolver / list_agents, recovery probe) and emits a `manually_submitted`-tagged Sentry event with attachments.
- Add a new authenticated `/api/v1` route (reachable by in-workspace agents via the latchkey `minds-api-proxy`) that calls the same collector/submitter; the app owns all Sentry sends.
- Determine the "current chat" and workspace context for the window that opened the help modal so the form can scope workspace fields and chat-history options correctly.

### Agent-help flow

- Add an `/assist` skill to the forever-claude-template that performs the gather/classify/fix logic and the built-in-vs-user and template-vs-vendor decisions, with the explicit give-up set (outer `apps/minds`, `mngr_forward`, `mngr_latchkey`, outer app's vendored mngr).
- Add a recognizable merge-commit message convention to the `/update-self` skill so the agent can identify built-in code arriving via template updates from git history.
- When the app initiates agent help in a loaded workspace, create the `/assist` chat (reusing an existing mngr utility/label so it auto-opens as a tab) and send the `/assist <description>` message.
- Route the agent's escalation through the existing requests/inbox permission mechanism, framed as a bug-report submission request; on approval, invoke the shared collector/submitter.
- For the not-in-workspace case, trigger creation of a new mind and run the same `/assist` flow with context about the original workspace, without cross-mind access.
