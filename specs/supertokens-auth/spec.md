# SuperTokens Authentication for Minds Cloudflare Forwarding

## Overview

* The minds desktop client currently authenticates with the cloudflare_forwarding Modal service using hardcoded Basic Auth credentials (`CLOUDFLARE_FORWARDING_USERNAME` / `CLOUDFLARE_FORWARDING_SECRET` env vars, validated against a static `USER_CREDENTIALS` JSON blob in a Modal Secret). This is fragile, insecure, and doesn't scale.
* Replace this with SuperTokens-based authentication: users sign in once on the desktop client, and the stored access token (JWT) is used for all cloudflare_forwarding API calls.
* SuperTokens Cloud (managed core) is already provisioned. The local `.env` file contains the connection URI and API key.
* Auth methods: email/password, Google OAuth, and GitHub OAuth. Email verification is required.
* This is additive -- the existing one-time-code local auth system and Basic Auth fallback on cloudflare_forwarding are preserved.

## Expected Behavior

### Sign-up flow (email/password)

* User clicks "Login" in the title bar (or is redirected after a "not authorized" interception).
* If the user has never signed in before (no local flag file), the auth page defaults to the **sign-up** form. Otherwise, it defaults to **sign-in**.
* Sign-up form has email and password fields. On submit, a `POST` to `/auth/signup` creates the account via the SuperTokens backend SDK.
* On success, the backend stores the access + refresh tokens server-side in `~/.mngr/minds/supertokens_session.json` and sets a local flag file indicating the user has signed in before.
* The user sees a "Check your email" page with a "Resend verification email" button. The page polls until the email is verified, then redirects to the landing page.
* The title bar updates to show the user's email address.

### Sign-in flow (email/password)

* User enters email and password. `POST` to `/auth/signin` validates credentials via the SuperTokens backend SDK.
* On success, tokens are stored server-side. Title bar updates.

### OAuth flow (Google / GitHub)

* User clicks "Sign in with Google" or "Sign in with GitHub".
* The system browser opens to the OAuth provider's authorization URL (obtained from the SuperTokens backend SDK).
* After authorization, the provider redirects to `http://127.0.0.1:{port}/auth/callback/{provider}`.
* The local FastAPI server handles the code exchange via the SuperTokens SDK, stores tokens server-side, and renders a "You can close this tab" page.
* The server emits an `auth_success` JSONL event on stdout. Electron picks this up and refreshes the UI (navigates to landing page, updates title bar).
* If the OAuth provider supplies a display name, the title bar shows that; otherwise, it shows the email.
* The OAuth callback route must NOT require one-time-code local auth.

### Title bar

* When not signed in: shows "Login" in the upper-right area of the title bar (to the left of the "open in browser" button). Clicking it navigates to the auth page.
* When signed in: shows the user's display name (from OAuth provider) or email. Clicking it shows a small dropdown with "Settings" and "Sign out".
* "Sign out" clears the stored tokens, updates the title bar back to "Login".
* "Settings" navigates to a settings page showing account details (email, auth provider, user ID). Includes a "Forgot password" link (for email/password users) and a "Sign out" button.

### Password reset

* Available on the sign-in page ("Forgot password?" link below the password field) and on the settings page.
* Clicking it shows a form to enter the email address. `POST` to the SuperTokens password reset endpoint sends a reset email via SuperTokens' built-in email delivery.
* The reset link in the email points to `http://127.0.0.1:{port}/auth/reset-password?token=...`. If the app has restarted on a different port, the link breaks -- the user can request a new one from within the app.
* The reset page has new-password + confirm fields. On submit, the password is updated via the SuperTokens SDK.

### Cloudflare forwarding authentication

* When the REST API v1 handler (e.g. `_handle_cloudflare_enable`) needs to call cloudflare_forwarding, it checks for a valid SuperTokens session (stored tokens in the session file).
* If a valid session exists: the access token (JWT) is sent as `Authorization: Bearer {token}` to cloudflare_forwarding.
* If the access token has expired: the backend refreshes it using the stored refresh token, updates the session file, and retries.
* If no session exists or the refresh token is also expired: the request is rejected, and the backend emits an `auth_required` JSONL event on stdout. Electron handles this by foregrounding the window and navigating to the auth page, which displays the message "You need to sign in to Imbue in order to share".
* The same check applies to the web UI toggle (`_handle_toggle_global` in `app.py`).

### Cloudflare forwarding server-side validation

* The cloudflare_forwarding Modal app adds `supertokens-python` as a pip dependency in its Modal image.
* At startup, it initializes the SuperTokens SDK with the same Core connection URI (added as a Modal Secret env var `SUPERTOKENS_CONNECTION_URI`).
* The `_authenticate` function gains a third auth path: if the `Authorization` header contains a `Bearer` token that is NOT a base64-encoded tunnel token, it is validated as a SuperTokens JWT using `get_session_without_request_response()`.
* On success, the user ID is extracted from the session. The first 16 hex characters of the user ID (UUID, hyphens stripped) serve as the "username" for tunnel naming.
* Basic Auth (`_authenticate_admin`) and agent tunnel tokens (`_authenticate_agent`) continue to work as fallbacks.

### Tunnel naming

* Old format: `{username}--{agent_id_prefix}` (where `username` came from Basic Auth `USER_CREDENTIALS`).
* New format: `{user_id_prefix}--{agent_id_prefix}` where `user_id_prefix` is the first 16 hex characters of the SuperTokens user UUID with hyphens stripped.
* No backwards compatibility is needed -- nothing is live yet.
* The `CloudflareForwardingClient.make_tunnel_name()` method and the cloudflare_forwarding server's `_authenticate` both use this same derivation.

### Default Cloudflare Access policy

* When a SuperTokens user creates a tunnel, the default auth policy uses the user's email (extracted from the JWT or fetched from the Core) instead of the `OWNER_EMAIL` env var.
* `OWNER_EMAIL` is kept as a fallback for Basic Auth users.

### Session persistence

* Access and refresh tokens are stored in `~/.mngr/minds/supertokens_session.json`.
* On app restart, the backend reads this file and validates the session. If the access token is expired but the refresh token is valid, it refreshes automatically.
* If both tokens are expired, the user is treated as not signed in (title bar shows "Login").

### Non-signed-in behavior

* The desktop client works normally without signing in. All local features (agent creation, local forwarding, proxy) function as before.
* Only cloudflare_forwarding calls require a valid SuperTokens session.

## Implementation Plan

### New files

* `apps/minds/imbue/minds/desktop_client/supertokens_auth.py` -- SuperTokens SDK initialization, session file management (read/write/refresh tokens), user info retrieval. Core class: `SuperTokensSessionStore` with methods `get_access_token()`, `get_user_info()`, `store_session()`, `clear_session()`, `is_signed_in()`, `has_signed_in_before()`.
* `apps/minds/imbue/minds/desktop_client/supertokens_routes.py` -- FastAPI routes for `/auth/*`: sign-up, sign-in, sign-out, OAuth redirect initiation (opens system browser), OAuth callback, email verification status polling, password reset request/submit, `GET /auth/status` (returns current user info for title bar). All routes use plain `fetch()` from the frontend -- no SuperTokens frontend SDK.
* `apps/minds/imbue/minds/desktop_client/templates_auth.py` -- HTML template functions for auth pages: sign-up form, sign-in form, "check your email" page, password reset form, settings page. Uses the same inline HTML pattern as existing `templates.py`. Includes vanilla JS for form submission via `fetch()`, polling, and OAuth button handlers.

### Modified files

* `apps/minds/imbue/minds/desktop_client/runner.py`
  - Import and initialize `SuperTokensSessionStore` (reads env vars `SUPERTOKENS_CONNECTION_URI`, `SUPERTOKENS_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`)
  - Call `supertokens_python.init()` with `framework="fastapi"`, `mode="asgi"`, recipe list: `session`, `emailpassword`, `thirdparty`, `emailverification`
  - Pass the session store to `create_desktop_client()`
  - Modify `_build_cloudflare_client()`: make `username`, `secret`, and `owner_email` optional (they're only needed for Basic Auth fallback). Add session store as an alternative auth source.

* `apps/minds/imbue/minds/desktop_client/app.py`
  - Accept `supertokens_session_store` parameter in `create_desktop_client()`
  - Store it in `app.state.supertokens_session_store`
  - Mount the SuperTokens middleware (`get_middleware()` from the SDK) for handling `/auth/*` routes
  - Include the new auth routes router
  - Add route for the settings page
  - Modify `_handle_toggle_global()` to check SuperTokens session before making cloudflare_forwarding calls; emit `auth_required` event if not signed in

* `apps/minds/imbue/minds/desktop_client/api_v1.py`
  - Before each cloudflare_forwarding call (`_handle_cloudflare_status`, `_handle_cloudflare_enable`, `_handle_cloudflare_disable`), check for a valid SuperTokens session via the session store
  - If not signed in, emit `auth_required` JSONL event and return a 401 response with `{"error": "Not signed in to Imbue", "auth_required": true}`
  - When signed in, use the stored access token for cloudflare_forwarding calls instead of Basic Auth

* `apps/minds/imbue/minds/desktop_client/cloudflare_client.py`
  - Add a `supertokens_token` parameter to methods (or add a method `with_supertokens_auth()` that returns a client configured to use Bearer auth)
  - When a SuperTokens token is available, use `Authorization: Bearer {token}` instead of Basic Auth
  - Derive `owner_email` from the SuperTokens session when available (for default access policies)
  - Update `make_tunnel_name()` to accept a `user_id_prefix` parameter (16 hex chars from the SuperTokens user ID) instead of always using `self.username`

* `apps/minds/imbue/minds/utils/output.py`
  - No changes needed -- `emit_event()` already supports arbitrary event types

* `apps/minds/electron/main.js`
  - Add `auth_success` and `auth_required` to the title bar HTML: add a user display area (right side, before the external link button) that shows "Login" or the user's name/email
  - Add a dropdown menu (Sign out, Settings) triggered by clicking the user display when signed in
  - Add IPC handler or `window.minds` method for `goToAuth()` (navigates to the auth page)
  - On page load, fetch `GET /auth/status` and update the title bar user display

* `apps/minds/electron/backend.js`
  - Add handlers for `auth_success` event: call a callback to refresh the main window's title bar
  - Add handlers for `auth_required` event: foreground the window (`mainWindow.show()`, `mainWindow.focus()`), navigate to the auth page with the "you need to sign in" message

* `apps/cloudflare_forwarding/imbue/cloudflare_forwarding/app.py`
  - Add `supertokens-python` to the Modal image pip dependencies
  - Initialize SuperTokens SDK at startup with `SUPERTOKENS_CONNECTION_URI` env var (from Modal Secret)
  - Modify `_authenticate()`: add a third auth path for SuperTokens JWT Bearer tokens. Use `get_session_without_request_response(access_token=token)` to validate. Extract user ID, derive 16-char hex prefix, return `AdminAuth(username=user_id_prefix)`.
  - Add `SUPERTOKENS_CONNECTION_URI` to the Modal Secrets list
  - Add email verification claim check: reject tokens where the email is not verified

* `apps/minds/pyproject.toml`
  - Add `supertokens-python` dependency

* `apps/cloudflare_forwarding/pyproject.toml`
  - Add `supertokens-python` dependency (also add to the Modal image `pip_install` list)

## Implementation Phases

### Phase 1: SuperTokens backend SDK integration in the desktop client

* Add `supertokens-python` dependency to `apps/minds/pyproject.toml`
* Create `supertokens_auth.py` with `SuperTokensSessionStore` (file-based token storage, read/write/refresh/clear)
* Initialize SuperTokens SDK in `runner.py` with email/password, third-party, session, and email verification recipes
* Create `supertokens_routes.py` with `/auth/signup`, `/auth/signin`, `/auth/signout`, `/auth/status` endpoints
* Create `templates_auth.py` with sign-up and sign-in HTML pages
* Wire everything into `app.py` (mount middleware, include router, pass session store)
* Result: users can sign up, sign in, and sign out via the desktop client web UI. Tokens are stored on disk.

### Phase 2: OAuth and email verification

* Add Google and GitHub OAuth provider configuration to SuperTokens init
* Add `/auth/oauth/{provider}` route that opens the system browser to the OAuth URL
* Add `/auth/callback/{provider}` route (exempt from one-time-code auth) that handles code exchange and stores tokens
* Emit `auth_success` JSONL event after successful OAuth callback
* Add "Check your email" verification page with polling and resend button
* Add password reset routes and pages
* Result: full auth flow works including OAuth (via system browser) and email verification.

### Phase 3: Electron UI integration

* Modify title bar HTML/JS in `main.js` to include user display area (right side)
* On page load, fetch `GET /auth/status` and show name/email or "Login"
* Add dropdown menu (Settings, Sign out) on clicking the user display
* Handle `auth_success` event in `backend.js` to refresh the title bar
* Handle `auth_required` event in `backend.js` to foreground window and navigate to auth page with message
* Add settings page template (account details, forgot password link, sign out button)
* Result: title bar shows auth state, dropdown works, auth-required interception foregrounds the app.

### Phase 4: Cloudflare forwarding integration

* Modify `CloudflareForwardingClient` to support Bearer auth with SuperTokens tokens alongside Basic Auth
* Update `make_tunnel_name()` to use SuperTokens user ID prefix (16 hex chars)
* Modify `api_v1.py` cloudflare handlers to check SuperTokens session before making calls
* Emit `auth_required` event when not signed in, return 401
* When signed in, use the stored access token for cloudflare_forwarding requests
* Derive `owner_email` from SuperTokens session for default access policies
* Result: desktop client uses SuperTokens tokens for all cloudflare_forwarding calls.

### Phase 5: Cloudflare forwarding server-side validation

* Add `supertokens-python` to cloudflare_forwarding Modal image
* Add `SUPERTOKENS_CONNECTION_URI` to Modal Secrets
* Initialize SuperTokens SDK at startup in the Modal function
* Add SuperTokens JWT validation path in `_authenticate()` -- validate token, extract user ID, derive username prefix, check email verification claim
* Keep Basic Auth and agent tunnel token auth as fallbacks
* Result: cloudflare_forwarding accepts SuperTokens JWTs, validates them against the Core, and uses user ID prefix for tunnel naming.

## Testing Strategy

### Unit tests

* `supertokens_auth.py`: test `SuperTokensSessionStore` file operations (store, read, clear, `is_signed_in`, `has_signed_in_before` flag). Mock the SuperTokens SDK calls for token refresh.
* `supertokens_routes.py`: test route handlers with a mock session store. Verify sign-up/sign-in call the SDK, store tokens, and return correct responses. Verify OAuth redirect opens system browser URL. Verify callback stores tokens and emits `auth_success` event.
* `cloudflare_client.py`: test that Bearer auth is used when a SuperTokens token is provided, Basic Auth when not. Test `make_tunnel_name()` with user ID prefix.
* `api_v1.py`: test that cloudflare handlers check for SuperTokens session, return 401 with `auth_required` flag when not signed in, and pass the token through when signed in.
* `cloudflare_forwarding/app.py`: test the new SuperTokens JWT auth path in `_authenticate()`. Mock `get_session_without_request_response()` to return a valid/invalid session. Verify user ID prefix extraction.

### Integration tests

* End-to-end sign-up flow: create account, verify tokens are stored, check `/auth/status` returns user info.
* End-to-end sign-in flow: sign in with existing account, verify token storage and status.
* Token refresh: store an expired access token with a valid refresh token, verify the backend refreshes automatically.
* Auth-required interception: make a cloudflare_forwarding call without a session, verify 401 response with `auth_required` flag and JSONL event emission.

### Edge cases

* App restart with valid session file: verify session is restored and title bar shows the user.
* App restart with expired session file: verify user is treated as not signed in.
* Corrupted/missing session file: verify graceful fallback to not-signed-in state.
* Concurrent cloudflare_forwarding calls during token refresh: verify no race conditions on the session file.
* OAuth callback when app has restarted on a different port: the callback URL in the system browser is stale -- this is expected; the user re-initiates OAuth from within the app.

## Open Questions

* **SuperTokens Core API key security**: The `SUPERTOKENS_API_KEY` is stored in the local `.env` file. If this is the same key used by the Core to authenticate backend SDK connections, it should be treated as a secret. Verify that it's not accidentally exposed to the Electron renderer or logged.
* **Token refresh concurrency**: If multiple REST API v1 calls arrive simultaneously and the access token is expired, multiple concurrent refresh attempts could occur. A simple mutex/lock around the refresh logic in `SuperTokensSessionStore` would prevent this, but the exact mechanism (threading lock vs asyncio lock) depends on whether the refresh is called from sync or async context.
* **SuperTokens SDK middleware interaction with existing auth**: The SuperTokens middleware intercepts requests to `/auth/*` paths. Verify that this doesn't conflict with the existing one-time-code auth middleware or other FastAPI middleware in the stack. The OAuth callback routes must bypass the one-time-code auth check.
* **Email verification link port**: Since the local server uses dynamic ports, email verification links will break if the user restarts the app before clicking the link. The mitigation (request a new verification email from within the app) should be clearly communicated in the "Check your email" UI.
