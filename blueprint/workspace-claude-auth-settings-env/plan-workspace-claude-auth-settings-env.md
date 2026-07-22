# Workspace Claude auth via shared settings.json env

## Refined prompt

> Move workspace (minds/default-workspace-template) Claude auth off mngr host env vars and into the shared CLAUDE_CONFIG_DIR settings.json env block; subscription auth via claude setup-token (CLAUDE_CODE_OAUTH_TOKEN); remove AI provider from the workspace create route; add "sign in with imbue" litellm-key paste flow in the workspace login modal; migration script for existing workspaces; restart-with-continue logic for running agents
>
> * Only the auth trio moves (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`); all other host_env vars stay where they are.
> * `ANTHROPIC_AUTH_TOKEN` is not managed by the writer.
> * The settings env auth keys are fully controlled by one writer (system_interface): presence and absence enforced per auth mode. Pinned-version (Claude Code 2.1.207) regression tests cover the empirically verified behaviors: settings env overrides shell env, API key outranks OAuth token, env maps deep-merge across settings scopes, and setup-token completes via polling without a code paste.
> * The modal offers three paths, ordered: Claude subscription (default, drives `claude setup-token` via the existing PTY machinery — URL shown, poll for completion, paste-code fallback — and writes `CLAUDE_CODE_OAUTH_TOKEN`), Sign in with Imbue, API key. Console OAuth is dropped.
> * Sign in with Imbue: the modal links to a new dedicated main-app mint page keyed by workspace id (e.g. `/settings/ai-keys?workspace=<host_id>`); the page resolves the workspace's owning account from the workspace-record store and errors with "associate an account on the settings page" when there is none.
> * Clicking the mint-page link while accessing the workspace remotely pops an alert saying to do it from the desktop client.
> * The mint page mints a LiteLLM key with the workspace identity as a fixed, non-editable alias/metadata field (budget defaults 100/1d) and copies an env-var-style blob (`ANTHROPIC_BASE_URL=...` / `ANTHROPIC_API_KEY=...`) to the clipboard; the user pastes it into a textarea in the modal ("paste your credentials here after you get them from the settings screen").
> * The mint page is mint-only: fixed workspace alias, copy-blob output; revocation stays a CLI concern.
> * Restart logic covers every claude-binary agent type (`claude` and `worker`, not just `type == "claude"`; `main` excluded): snapshot states via `mngr list` before stopping; previously-RUNNING agents (chat agents included) get an auth-aware message after restart ("Your Claude credentials were just updated and your session was restarted. Please continue what you were working on."); WAITING agents get nothing.
> * The restart-then-status-check flow is kept (no pre-validation API call before restarting).
> * No proactive token-expiry handling: rely on the existing auth-error detection to pop the modal when the token dies.
> * A persistent "Agent auth" entry below the chat (near "open agent terminal") opens the modal any time; clicking goes straight to provider selection.
> * A single muted header line above the provider options shows the current mode, derived from settings content (token = subscription; key + base URL = imbue/litellm; key alone = direct Anthropic), with a masked key suffix.
> * No sidecar metadata: auth mode display is derived purely from settings content at read time.
> * The API-key field and the imbue textarea submit to one shared backend endpoint accepting env-var-style lines (the key field wraps its value as `ANTHROPIC_API_KEY=...`).
> * The env-lines parser is strict: pastes containing any unmanaged key are rejected with a clear error, and mixed-mode pastes are rejected (allowed: key alone, key + base URL, or token alone).
> * A pasted `CLAUDE_CODE_OAUTH_TOKEN` line is accepted as a third managed key, surfaced only as a very subtle affordance (muted "Already have a token? Paste it instead" link on the subscription waiting screen expanding an inline field).
> * Subscription success copy: "Signed in with your Claude subscription" plus a muted "token valid for about a year" note.
> * AI provider comes off the create route; the server silently ignores a stale `ai_provider` field from old clients.
> * Cross-repo landing: one coordinated pair of PRs (paired same-name branches in dwt and mngr) cut over together; land on main and let the next scheduled minds release pick it up (no dedicated release for this change).
> * Manual verification gate: DOCKER mode only (create, modal sign-in, chat) — the auth surfaces are identical across launch modes.
> * Manual migration script in default-workspace-template: copies a working key/base-URL from `/mngr/env` into the settings env block, scrubs them from host env, restarts claude agents; subscription-based workspaces need no migration (existing `.credentials.json` keeps working at lower precedence until expiry, then the modal offers setup-token). A dwt changelog entry tells upgrading workspaces which script to run.
> * The migration script runs its restart step detached (nohup-style) so an agent invoking it on itself still completes the migration; the invoking agent comes back with the standard "please continue" message.
> * Services: the keyed (litellm/direct-HTTP) path reads credentials from the shared settings.json instead of `os.environ` (key wins over token, mirroring claude); the keyless `claude -p` path is unchanged and picks up new auth per spawn.
> * `claude_auth_patterns.py` gains litellm budget/auth rejection patterns so a budget-exhausted workspace pops the modal, with copy noting it may be budget rather than credentials; the patterns are captured empirically (mint a tiny-budget key against dev litellm, exhaust it, snapshot the real transcript error shapes).
> * `test_snapshot_resume` drives the modal UI via Playwright: create with no provider, login modal auto-appears, fill API key, chat round-trip + welcome assertion.
> * `test_litellm_via_workspace` is implemented for real as part of this work (workspace-create driver + Neon spend-query helper included): it drives the actual mint page + modal paste UI end-to-end and asserts spend lands in Neon; e2e coverage also exists for the API-key and OAuth-token login methods.
> * The setup-token PTY leg is covered by integration tests with an injected fake pexpect spawner (the existing `ClaudeAuthService` DI pattern); e2e exercises the flow as far as the URL/poll stage; no real OAuth tokens in CI — the token paste path is e2e-tested with a dummy token asserting the settings write + restart.
> * No workspace-tile auth badges in the main app for v1.

## Overview

- Claude auth for workspaces moves out of the mngr host env file (`/mngr/env`) and into the `env` block of the shared `CLAUDE_CONFIG_DIR/settings.json` — the config dir every claude in the workspace already inherits. Changing auth becomes "edit one file + restart claude agents"; the services agent, supervisord, and every background service are never touched.
- The in-workspace login modal becomes the *sole* auth surface. The AI-provider choice (and key entry) disappears from the minds create flow entirely; every workspace boots unauthenticated and the existing modal/welcome-resend machinery turns first sign-in into the designed onboarding step.
- Subscription auth switches from fragile synced `.credentials.json` logins to a 1-year token from `claude setup-token`, stored as `CLAUDE_CODE_OAUTH_TOKEN` in the settings env block. A new "Sign in with Imbue" path mints a LiteLLM key from a dedicated desktop-app page (keyed to the workspace, fixed alias) and pastes it into the modal as an env-style blob.
- The auth-recovery restart is fixed and upgraded: it now covers `worker`-type agents (the current `type == "claude"` filter misses them), snapshots agent states first, and sends previously-RUNNING agents an auth-aware "please continue" message so unattended workers resume instead of silently dying.
- All load-bearing Claude Code behaviors were verified empirically on the pinned 2.1.207 (settings env drives requests and overrides shell env; key outranks token; env maps deep-merge across scopes; setup-token completes by polling; oat tokens support `opus[1m]` + fast mode) and get pinned-version regression tests, since two of them contradict the official docs.

## Expected behavior

### Creating a workspace

- The create form and `POST /api/v1/workspaces` no longer offer an AI provider or API-key field; presets only choose launch mode and backup. A stale client sending `ai_provider` is silently ignored.
- The desktop client no longer mints LiteLLM keys or injects `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` at create time; the dwt templates no longer forward them via `pass_host_env`.
- On first boot the chat agent comes up unauthenticated; the initial `/welcome` does not land. Opening the workspace pops the login modal automatically (existing load-time detection). After any successful sign-in, the existing WelcomeResender re-sends `/welcome`.

### The login modal

- Three options, in order: **Claude subscription** (default), **Sign in with Imbue**, **API key**. Console OAuth is gone.
- A muted header line shows the current mode, derived from the settings env content: "Currently signed in via Claude subscription" / "…via Imbue (…a4f2)" / "…via API key (…a4f2)" / "Not signed in".
- **Subscription**: drives `claude setup-token` through the PTY machinery; the modal shows the OAuth URL and polls — the flow normally completes on browser approval without any code paste (verified), with the paste-code field as fallback. On success, writes `CLAUDE_CODE_OAUTH_TOKEN` into the settings env block. Success copy: "Signed in with your Claude subscription" + muted "token valid for about a year". The waiting screen carries a muted "Already have a token? Paste it instead" link expanding an inline field.
- **Sign in with Imbue**: instructional text + a link to the desktop app's mint page + a textarea. Clicking the link while the workspace is accessed remotely (non-`.localhost` origin) pops an alert saying to do it from the desktop client. The user pastes the copied env-style blob into the textarea.
- **API key**: a single `sk-ant-...` field (wrapped internally as an `ANTHROPIC_API_KEY=...` line).
- All three paste paths hit one shared endpoint that parses env-var-style lines. Managed keys: `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`. Unmanaged keys are rejected with a clear error; mixed-mode pastes (token alongside key/base-URL) are rejected; allowed shapes are key alone, key + base URL, or token alone.
- The writer fully controls the managed keys: switching modes deletes the other mode's keys from the env block (presence *and* absence enforced). `ANTHROPIC_AUTH_TOKEN` is not managed. No sidecar metadata is written.
- After a successful settings write, the existing restart-then-status-check flow runs (no pre-validation call). No proactive token-expiry tracking — an expired/revoked token surfaces through the existing auth-error detection.
- A persistent **"Agent auth"** entry sits below the chat near "open agent terminal" and opens the modal (straight to provider selection) at any time, so users can switch modes while auth still works.

### Restarting agents after an auth change

- The restart covers every claude-binary agent type — `claude` *and* `worker` — excluding the `main` services agent. Before stopping, agent states are snapshotted via `mngr list`.
- Stop-all → prepare shared Claude config (dialog dismissals, key approval) → start-all (`--no-resume`), as today.
- Agents that were RUNNING (chats included) then receive: "Your Claude credentials were just updated and your session was restarted. Please continue what you were working on." WAITING agents receive nothing — the next user message starts them with fresh env anyway.

### The Imbue mint page (desktop app)

- New dedicated page, e.g. `/settings/ai-keys?workspace=<host_id>`, reachable only at the desktop-client origin.
- Resolves the workspace's owning account via the workspace-record store (association is record existence). With no associated account it errors, telling the user to associate one on the settings page.
- Mint-only v1: one mint action with the workspace identity as a fixed, non-editable alias/metadata field, LiteLLM budget defaults 100/1d, and a copy-to-clipboard env-style blob (`ANTHROPIC_BASE_URL=...` newline `ANTHROPIC_API_KEY=...`) using the base URL returned with the minted key. Revocation stays in the CLI. No workspace-tile auth badges.

### Services and error detection

- The keyed (litellm/direct-HTTP) service path reads credentials from the shared settings.json at call time instead of `os.environ`, with key-over-token precedence mirroring claude. The keyless `claude -p` path is unchanged — every spawn reads the shared settings and picks up new auth with zero restarts.
- The transcript auth-error patterns gain litellm budget/auth rejection shapes (captured empirically from a real exhausted tiny-budget key), so a budget-exhausted workspace pops the modal with copy noting it may be budget rather than credentials.

### Migration of existing workspaces

- A manual script in dwt: copies a working `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` from `/mngr/env` into the settings env block, scrubs them from the host env file, and restarts claude agents. The restart step runs detached, so an agent running the script on itself still completes; that agent returns to a "please continue" message.
- Subscription-based workspaces need no migration: their `.credentials.json` keeps working at lower precedence until it expires, at which point the modal offers setup-token.
- A dwt changelog entry names the script so upgrading workspaces know to run it. Idempotent: re-running on a migrated workspace is a no-op.

### Rollout

- One coordinated pair of PRs on paired same-name branches (dwt + mngr), landed together on main; the next scheduled minds release picks it up. Old clients degrade gracefully (workspaces boot unauthenticated; the modal recovers them).
- Manual verification gate before landing: DOCKER mode end-to-end (create, each modal sign-in path, chat).

## Changes

### default-workspace-template

- `.mngr/settings.toml`: drop `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` from every template's `pass_host_env__extend` (imbue_cloud keeps `MNGR_PREFIX`).
- `system_interface` backend (`claude_auth.py` + endpoints): replace the `--claudeai`/`--console` OAuth flows with a `claude setup-token` PTY driver (URL extraction, poll-completion, paste fallback); add the settings-env writer owning the three managed keys with strict env-lines parsing (unmanaged/mixed rejection); one shared submit endpoint for all paste paths; current-mode derivation for the modal header; restart upgraded to cover `claude` + `worker` types with pre-stop state snapshot and post-restart "please continue" messaging to previously-RUNNING agents; drop the host-env API-key write path.
- `system_interface` frontend (`ClaudeLoginModal`, chat panel): three reordered provider options, current-mode header, subscription URL + poll screen with subtle token-paste affordance, imbue section (instructions + desktop-origin mint-page link with remote-access alert + textarea), persistent "Agent auth" entry below the chat near "open agent terminal".
- `claude_auth_patterns.py`: add empirically captured litellm budget/auth rejection patterns; modal copy for the budget case.
- `scripts/`: the manual migration script (copy keys into settings env, scrub host env, detached restart, idempotent).
- Services keyed path (`use-ai-integration` claude_p helper): read credentials from the shared settings.json instead of `os.environ`; update `billing-and-credentialing.md` accordingly.
- New pinned-version regression tests for the load-bearing Claude Code behaviors (settings-env-beats-shell, key-outranks-token, cross-scope env deep-merge, setup-token poll-completion); fake-pexpect integration tests for the setup-token driver; dummy-token paste test asserting settings write + restart.
- Docs: README/design notes describing the settings-env auth contract and the single-writer ownership.

### minds (mngr repo)

- Remove `AIProvider`/`anthropic_api_key` from the create API, form, presets, and `agent_creator` (including per-create LiteLLM minting and subprocess-env injection); server ignores a stale `ai_provider` field.
- New mint-only page + endpoint keyed by workspace host id: account resolution via the workspace-record store (error when unassociated), fixed workspace alias/metadata on the minted key, 100/1d budgets, env-blob clipboard output.
- `test_snapshot_resume`: rework the manual-key leg to drive the modal via Playwright (bare create, auto-appearing modal, API-key fill, chat + welcome assertions).
- `test_litellm_via_workspace`: implement for real — workspace-create driver, mint page + modal paste UI driven end-to-end, Neon spend assertion.
- Update remaining test/fixture surfaces that passed auth through the create route (e2e workspace runner, api_v1/templates/desktop-client tests); tmr mapper prose.
- Docs: overview/design updates for the removed create-time credential flow.

### Rollout

- Paired same-name branches, one coordinated landing on main; no dedicated release (next scheduled minds release ships it, including rebaked artifacts as part of that release's normal process).
- Pre-landing manual verification: DOCKER mode end-to-end.
