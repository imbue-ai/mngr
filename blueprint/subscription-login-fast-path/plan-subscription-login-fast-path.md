# Subscription login fast path (browser sign-in primary, setup-token demoted)

## Refined prompt

> let's work through a small spec, to ensure we get these new changes right:
>
> this whole "setup token" flow is fine, but it's slower and more complex than what we had before.
>
> As you yourself said--the nice thing about the previous login flow was that it did *NOT* require restarting claude (ie, ends up being much faster)
>
> I'd like to move *back* to that being the *primary* way to for a user to log in.
>
> We can *keep* this new setup-token method, but let's demote it to being the 4th option for how to sign in (after "Use an API key"). Instead, the primary "Sign in with your Claude subscription" should work how it did before. That way, when users are first getting started, it will be faster--they'll just end up signed in, we won't need to restart their chat agent, etc.
>
> Later, when they *switch* authentication types, we *will* probably need to restart their chat agents, and possibly mutate the .claude.json or .credentials files.
>
> (note that changing the "sign in with imbue" flow to get it working is currently out of scope, we'll come back to that later)
>
> * The primary subscription path drives `claude auth login --claudeai` through the existing hardened PTY machinery (OSC-8 URL extraction, deferred-Enter code delivery, OAuth-error fail-fast); its credential lands in the shared config dir and running claudes re-read it on their next API call, so no restart is needed.
> * When a managed key/token is active and the user picks subscription sign-in (the switching case), the flow automatically completes the browser sign-in, then clears the managed env keys and runs the restart flow with the step checklist -- no upfront warning gate.
> * When the managed env is empty, `auth_mode` and the "currently signed in via" header fold in `claude auth status`: subscription oauth shows "Claude subscription", otherwise "Not signed in".
> * The subscription-credentials path runs no `claude -p` validation probe -- the OAuth exchange completing is the proof.
> * Switching away from subscription-credentials leaves `.credentials.json` in place (managed env keys outrank it, so it is functionally inert).
> * The demoted setup-token entry is the 4th option, after "Use an API key", labeled "Get a long-lived token", keeping its current URL + paste-code flow.
> * The primary flow reuses the identical two-step screen as setup-token (open the sign-in page; approve then paste the always-visible code input, with the silent background poll); only the success handling differs.
> * In the switching case, the applying checklist leads with a completed "Signed in with your subscription" step, then "Removing old credentials / Restarting agents / Resuming your agent".
> * The `/logout` interception dialog stays exactly as built.
> * Console OAuth (`claude auth login --console`) is restored as the 5th option; it writes into `.claude.json`, so it always uses the restart path.
> * The Console option is labeled "Anthropic Console (API billing)" with header copy "Currently signed in with your Anthropic Console account".
> * The `claude -p` validation probe is removed from ALL routes (the user will try the agent themselves; the probe is slow, needs a network round trip, and cannot run offline); with it go the `validating` phase, the settings restore-on-rejection, and the "Checking credentials" checklist step.
> * Clearing the managed env on a switch deletes all three managed keys including `ANTHROPIC_BASE_URL`.
> * The managed-env paths (API key, long-lived token, Imbue) use the checklist "Credentials saved (done) / Restarting agents / Resuming your agent".
> * Bad credentials surface post-restart through the existing transcript auth-error detection reopening the modal -- no extra machinery.
> * No migration is needed: existing token-authed workspaces keep working, the host-env migration script is untouched, and the change lands on the same coordinated dwt+mngr branch pair.
> * Success and header copy include the account email when `claude auth status` provides one ("Signed in as <email> with your Claude subscription"), falling back to generic copy.
> * The modal's load-time auto-open keys off the folded logged-in status (credentials OR managed env), so fresh workspaces auto-open as today and browser-signed-in workspaces do not.
> * A managed "Sign out" affordance is out of scope for this spec.

## Overview

- Browser sign-in (`claude auth login --claudeai`) becomes the primary "Sign in with your Claude subscription" again because its credential is re-read live by running claudes: fresh workspaces sign in with zero agent restarts and land straight in a working chat.
- The setup-token flow is kept but demoted to the 4th option ("Get a long-lived token"); Console OAuth returns as the 5th ("Anthropic Console (API billing)").
- The managed settings-env block remains the machinery for the key/token/Imbue modes; because managed keys outrank `.credentials.json`, choosing subscription while a managed key is active is a "switch": complete the browser sign-in, clear all managed keys, restart the claude agents.
- The `claude -p` validation probe is removed from every route (too slow, needs the network, and users try the agent immediately anyway); the transcript auth-error detection reopening the modal is the recovery net, as originally designed.
- All existing PTY hardening is reused verbatim for the two browser flows: OSC-8 URL extraction with frame-replay fallback, always-visible paste-code input, deferred-Enter code delivery, OAuth-error fail-fast.

## Expected behavior

### Provider selection

- Primary: "Sign in with your Claude subscription" (browser flow, no restart when the managed env is empty).
- "Other ways to sign in" (in order): Sign in with Imbue (still broken, out of scope), Use an API key, Get a long-lived token (the current setup-token flow), Anthropic Console (API billing).
- The muted header derives the current mode: managed env present -> imbue / api_key / subscription-token as today; managed env empty -> folded from `claude auth status` ("Currently signed in with your Claude subscription (<email>)" when available, Console copy for console accounts, otherwise "Not signed in").
- The modal's auto-open on workspace load keys off the folded logged-in status, so a browser-signed-in workspace does not pop the modal.

### Primary subscription flow (fresh workspace: managed env empty)

- Same two-step screen as setup-token: 1. Open the sign-in page, 2. Approve, then paste the code shown (input always visible; silent background poll finishes early if the CLI self-completes).
- On completion the CLI has written the subscription credential into the shared config dir; running claudes pick it up on their next API call.
- No restart, no checklist: straight to the success screen ("Signed in as <email> with your Claude subscription", generic fallback without email), and the welcome resend fires immediately.
- An OAuth error (bad/stale code) fails fast with the CLI's error text, exactly like setup-token today.

### Switching flows (managed key/token active, or Console chosen)

- Choosing subscription with a managed key active: the browser sign-in completes first, then the managed env keys (all three, including `ANTHROPIC_BASE_URL`) are cleared and the background restart runs.
- Checklist for that case: "Signed in with your subscription (done) / Removing old credentials / Restarting agents / Resuming your agent".
- Console OAuth always takes the restart path (its `primaryApiKey` lands in `.claude.json`, which claudes cache at process start) and also clears the managed env keys first.
- Managed-env paths (API key / long-lived token / Imbue blob) keep the async restart with checklist "Credentials saved (done) / Restarting agents / Resuming your agent".
- Previously-RUNNING agents still get the "please continue" message via the fused `mngr start --restart --resume-message` call; WAITING agents restart silently.

### Validation removal

- No route runs the `claude -p` probe; the `validating` phase, "Checking credentials" step, and settings restore-on-rejection are gone.
- The local guards stay: setup-token length floor and extraction-length logging.
- If a credential is bad, the agent hits an auth error on first use; the existing transcript auth-error detection reopens the modal.

### Unchanged

- `/logout` interception dialog; the strict env-lines parser; single-flight restart guard; restart coverage of `claude` + `worker` agent types; the host-env migration script; existing token-authed workspaces (their managed env keeps outranking everything, exactly as today).
- Switching away from subscription-credentials leaves `.credentials.json` in place.

## Changes

### default-workspace-template (system_interface)

- `claude_auth.py`: reintroduce the browser OAuth session driver (start / poll / submit-code / abort) parameterized by provider (`--claudeai` / `--console`), reusing the existing PTY machinery (spawner, OSC-8 + frame-replay URL extraction, drain, deferred-Enter code delivery, OAuth-error fail-fast); completion is the CLI exiting successfully (no token extraction for these flows).
  - `--claudeai` completion: if managed env is empty, no restart -- return folded status; if managed keys present, clear them (write empty managed env) and start the background restart.
  - `--console` completion: always clear managed keys and start the background restart.
  - Delete `_validate_written_credentials`, the `VALIDATING` phase, restore-on-rejection, and the validation timeout constant; `start_background_apply` becomes write-then-restart again.
  - Extend `RestartProgress` phases/details to carry the switch-flavored step copy (removing-old-credentials vs credentials-saved lead-ins).
  - `get_auth_status` / `derive_auth_mode`: when the managed env is empty, fold `claude auth status` output (auth method + provider + email) into `auth_mode` and the header fields; determine empirically on the pinned version how `auth status --json` distinguishes claudeai vs console accounts and pin that in a regression test.
- `claude_auth_endpoints.py` / `models.py`: session endpoints gain the provider dimension (or a parallel oauth-login set mirroring the old API); status response carries the folded mode + email.
- `ClaudeLoginModal.ts`: primary button drives the browser flow with the shared two-step screen; "Other ways" list reordered/extended (Imbue, API key, Get a long-lived token, Anthropic Console (API billing)); success copy with email; checklist step-copy variants; drop the "Checking credentials" step.
- `checkAuthStatusOnLoad` keeps keying off `logged_in`, which is now the folded value.
- Welcome resend: fires directly on the no-restart success path (as the old flow did) and stays on restart-completion for restart paths.

### Testing / docs (rides the standing deferred test pass)

- Fake-pexpect integration tests for the auth-login driver (URL, code submit, OAuth error, no-restart vs switch branching); pinned-version regression test for the `auth status --json` account-discrimination fields; modal test updates for the new option order and checklists; no real OAuth in CI.
- Update the module docstring, README, and the dwt changelog entry for the branch.

### mngr repo

- No mngr-side changes (the `--resume-message` flag and minds-side work are already on the branch).
