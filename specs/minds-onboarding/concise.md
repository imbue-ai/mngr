# Minds Onboarding

## Overview

- The current first-start experience drops users into a complex create-project form with no encouragement to sign up, making features like sharing, cloud hosting, and leased hosts unavailable by default.
- This spec reworks the first-start flow to: (1) default users into account creation, (2) simplify the create-project form to three visible fields (name, account, mode), and (3) make LEASED mode the default for signed-in users so project creation is near-instant via pre-provisioned Vultr hosts.
- Window state persistence (position, size, monitor, URL) is added so the app restores exactly where the user left off, and so first-start detection becomes simply "no saved window state and not authenticated."
- A project destroy flow is added (workspace settings button with confirmation dialog) that also releases leased hosts back to the pool.

## Expected Behavior

### First start (new user, no saved state, no accounts)
- Electron opens a single window and navigates to `/welcome` (a branded splash page).
- The splash page shows "Welcome to Minds" with prominent Sign Up and Log In buttons, and a small "Continue without an account" link at the bottom.
- Clicking Sign Up or Log In navigates to the existing SuperTokens `/auth/` routes.
- After successful signup/login, the user is redirected to `/`, which (with no agents) renders the simplified create-project form. The newly created account is automatically set as the default and pre-selected.

### "Continue without an account" bypass
- Clicking the link shows a confirmation dialog listing what an account enables (project sharing, cloud projects, no BYOK).
- The dialog includes "Cancel" and "Continue" buttons.
- Confirming navigates to the create-project form with "No account (private project)" selected.

### Returning user (saved session state exists)
- Electron restores all previously open windows at their saved positions, sizes, and URLs.
- If a saved monitor no longer exists, the window is placed on the primary display at a reasonable position.
- URLs for workspaces that no longer exist are silently dropped (existing behavior, preserved).

### Returning user (no saved state, but authenticated)
- Electron navigates to `/` (landing page), not `/welcome`.
- The landing page shows the agent list or create form as before.

### Create-project form (simplified)
- Three visible-by-default fields:
  - **Name**: auto-populated from `MINDS_WORKSPACE_NAME` env var, falling back to "assistant" (changed from "selene").
  - **Account**: dropdown with all logged-in accounts plus "No account (private project)". Default account pre-selected.
  - **Mode**: all 5 launch modes (LOCAL, CLOUD, DEV, LIMA, LEASED) visible. LEASED is default when an account is selected; LOCAL is default when "No account" is selected. LEASED is greyed out with a tooltip when "No account" is selected.
- An "Advanced options" toggle reveals: git_url, branch, include_env_file.
- Branch defaults to blank. Blank means "use latest semver tag from template repo, falling back to main."

### Account association at creation time
- The create form POST includes an `account_id` field.
- The backend associates the workspace with the selected account during creation.
- The tunnel token is injected after the agent is fully created and running (not during creation).

### LEASED mode version resolution
- When branch is blank: the version is the latest semver tag (e.g., `v1.2.3`) discovered via `git ls-remote --tags` against the template repo URL.
- When branch is explicitly set: the branch name is used as the version string.
- This makes the dev workflow natural: set a branch name during development and both the minds app and provisioning script use the same version.
- If no leased hosts are available at the requested version, a clear error message is shown and the user can retry.

### LEASED mode progress
- Reuses the same progress page (`creating.html`) as other modes.
- Status messages are adjusted for the LEASED flow (e.g., "Connecting to host...", "Setting up agent..." instead of "Cloning repository...").

### Default account
- The first account a user logs into becomes their default (persisted in `~/.minds/config.toml` as `default_account_id`).
- The default can be changed from the accounts settings page (existing UI, already implemented).

### Project destruction
- A "Destroy project" button is added to workspace settings.
- Clicking it shows a confirmation dialog.
- On confirm: the agent is destroyed (via `mngr destroy`), the leased host is released if applicable, the workspace is disassociated from its account, and all windows for that workspace are closed.
- The window that initiated the destroy navigates to `/`.
- Destruction runs in a background thread with a "Destroying..." progress indicator.

### `/welcome` route behavior
- Always renders the splash page regardless of auth state.
- Electron only navigates there when conditions are met (no saved state AND not authenticated).
- Directly visiting `/welcome` as an authenticated user shows the splash page but is harmless.

## Changes

### Electron (`electron/main.js`)

- **`saveSessionState()`**: extend each entry from `{url}` to `{url, x, y, width, height, displayId}` where `displayId` identifies the monitor.
- **`loadSessionState()`**: read the extended format; on restore, validate the saved display still exists. If not, place the window on the primary display with default dimensions.
- **First-start navigation**: after `fetchInitialChromeState()`, if `!authenticated && restorable.length === 0`, navigate to `backendBaseUrl + '/welcome'` instead of `loginUrl`. If `authenticated && restorable.length === 0`, navigate to `backendBaseUrl + '/'` (existing behavior).
- **Destroy handling**: listen for a custom event or navigation from the backend that indicates a workspace was destroyed; close all windows for that workspace's agent ID.

### Python backend -- new routes

- **`GET /welcome`**: server-rendered splash page with Sign Up, Log In, and "Continue without an account" elements. The confirmation dialog is client-side JS/HTML within the template.
- **`POST /api/destroy-agent/{agent_id}`**: accepts a destroy request; spawns a background thread that runs `mngr destroy`, releases leased host if applicable, disassociates account, and returns a redirect.
- **`GET /api/destroy-agent/{agent_id}/status`**: returns destruction progress (for the progress indicator).

### Python backend -- modified routes

- **`GET /` (`_handle_landing_page`)**: no change in logic -- still shows login page if unauthed, agent list if agents exist, create form if no agents. The only change is that Electron no longer sends unauthenticated users here on first start.
- **`GET /create` or `GET /` (create form path)**: `render_create_form()` now accepts and renders `accounts` (list of logged-in accounts), `default_account_id`, and produces the simplified form layout with advanced options toggle.
- **`POST /api/create-agent`**: accepts new `account_id` field. After creation completes (in background thread), associates workspace with account and injects tunnel token.

### Templates

- **New: `welcome.html`**: branded splash page with Sign Up / Log In buttons and "Continue without an account" link + confirmation dialog.
- **Modified: `create.html`**: simplified layout with account selector, advanced options toggle, mode-dependent LEASED disable logic.
- **Modified: `creating.html`**: support different status message sets based on launch mode (add LEASED-specific messages).
- **Modified: `workspace_settings.html`**: add "Destroy project" button with confirmation dialog.

### `templates.py`

- **New: `render_welcome_page()`**: renders the splash page template.
- **Modified: `render_create_form()`**: add `accounts`, `default_account_id` parameters. Change `_DEFAULT_AGENT_NAME` fallback from `"selene"` to `"assistant"`.
- **New: `render_destroy_progress()`**: renders a destruction progress page (or reuse a generic progress pattern).

### `agent_creator.py`

- **Modified: `start_creation()`**: accept `account_id` parameter. After agent creation completes, call `session_store.associate_workspace()` and inject tunnel token.
- **Modified: `_create_leased_agent()`**: version resolution -- when branch is blank, use `git ls-remote --tags` to find latest semver tag from template repo. When branch is set, use branch name as version string.
- **New: `destroy_agent()`**: background-threaded method that runs `mngr destroy`, calls `release_leased_host()` for leased agents, and disassociates the account.

### `app.py`

- **New: `_handle_welcome_page()`**: renders the welcome/splash page.
- **New: `_handle_destroy_agent()`**: POST handler that starts background destruction.
- **New: `_handle_destroy_agent_status()`**: GET handler for destruction progress.
- **Modified: `_handle_create_form_submit()` / `_handle_create_agent_api()`**: pass `account_id` from form/JSON to `agent_creator.start_creation()`.
- **Modified: `create_app()` route registration**: register `/welcome`, `/api/destroy-agent/{agent_id}`, `/api/destroy-agent/{agent_id}/status`.

### `session_store.py`

- No structural changes needed. Existing `associate_workspace()`, `disassociate_workspace()`, `list_accounts()`, and `get_account_for_workspace()` are sufficient.

### `minds_config.py`

- **Modified**: when `set_default_account_id()` is called for the first time (i.e., on first login), it should be called automatically from the signup/login success path so the first account becomes the default.

### `host_pool_client.py`

- No structural changes. The `version` parameter is already accepted by `lease_host()`.

### Version resolution (new utility)

- **New function** (in `agent_creator.py` or a new `version_resolver.py`): `resolve_template_version(git_url, branch)` that:
  - If branch is non-empty: returns the branch name as the version string.
  - If branch is empty: runs `git ls-remote --tags <git_url>`, parses semver tags, sorts them, and returns the latest. Falls back to `"main"` if no tags found.
