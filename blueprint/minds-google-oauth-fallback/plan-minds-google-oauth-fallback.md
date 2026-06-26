# Minds Google OAuth fallback

## Refined prompt

now I want users to be able to auth automatically with the new system. the way it should work is as follows:
- Whenever a user asks their mind to use a google API, it should do these steps:
 1. Try to use current credentials - check if they're valid and if so, all's well.
 2. If the creds are invalid, then try to use the Minds Google OAuth (I'll provide the clientID and Secret for `latchkey auth prepare "{'clientId': '...', 'clientSecret': '...'}"` ) so it will use our consent screen.
   * The Minds Google OAuth clientId/clientSecret are strictly hardcoded constants in `mngr_latchkey`, a single pair reused for every `google-` service; shipped as placeholders now and always attempted (real creds go in before use — no non-empty gate).
   * Step 2 applies only to services whose latchkey name is prefixed `google-` (all 8 catalog google services); other services keep today's behavior unchanged.
   * Mechanism: `latchkey auth prepare <service> '{"clientId":...,"clientSecret":...}'` then a bare `latchkey auth browser <service>` (which must not auto-trigger self-setup).
   * If a client is already registered for the service (detected via `latchkey auth list`; on read failure, default to not-registered), skip prepare and go straight to `auth browser`, reusing the registered client.
   * Implement `auth_list` / `auth_prepare` / bare-`auth browser` primitives on the `Latchkey` class; orchestrate the 1→2→3 chain in the desktop handler's `grant()`.
 3. If that fails, then use latchkey auth browser prepare to get the user to create their own google oauth project and client, and use that to auth.
   * On *any* step-2 failure, fall through to the self-setup browser flow (v1 does not special-case user cancellation).

Right now, it should be doing 1 and 3, but we need to add 2 in the middle, so it can try to skip the "gimmicky" self-setup step by doing browser prepare.
* Keep the existing single "authenticating, opening a browser" dialog notice for both attempts.
* Unit tests for the new primitives and `grant()` ordering, plus one acceptance test exercising the full fallback ordering end-to-end.

## Overview

- Insert a **Minds-owned Google OAuth attempt** between the existing "are the credentials valid?" check and the "make the user self-set-up their own Google project" fallback, so most users authenticate against the Minds consent screen and never see the gimmicky self-setup flow.
- The new attempt registers a hardcoded Minds OAuth client for the requested service via `latchkey auth prepare <service> '{"clientId":...,"clientSecret":...}'`, then runs a bare `latchkey auth browser <service>`.
- Scope is **Google services only** (latchkey service name prefixed `google-`); every other service keeps today's exact behavior. One Minds client pair is reused for all 8 catalog google services.
- The fallback chain is **driven by the desktop handler** (`LatchkeyPermissionGrantHandler.grant`); `mngr_latchkey`'s `Latchkey` class only gains thin, single-purpose primitives (`auth_list`, `auth_prepare`, a bare `auth_browser_login`).
- Behavior is conservative: on **any** Minds-attempt failure we fall through to self-setup; if a client is **already registered** (Minds- or user-set-up), we skip prepare and reuse it so we never clobber a working setup.

## Expected behavior

- User asks their mind to use a Google API; the mind hits the gateway, gets blocked, and files a permission request as today.
- On Approve, for a `google-` service whose credentials are **not valid**:
  - **No client registered yet:** Minds registers its client (`auth prepare`) and opens the **Minds consent screen** (`auth browser`). On success the grant is applied — the user never sees self-setup.
  - **Minds attempt fails for any reason** (prepare error, Google error, user closes/cancels the window): the flow falls through to the existing self-setup browser flow (`auth browser-prepare` + `auth browser`), exactly as today.
  - **A client is already registered** (from a prior Minds attempt or a prior self-setup): skip prepare and go straight to `auth browser`, reusing that client; on failure, fall through to the existing self-setup browser flow (same fallback as any other failure).
- Credentials already valid → grant applied immediately, no browser (unchanged).
- Non-`google-` services → identical to today (single `auth_browser` self-setup chain).
- Services with no browser auth option (e.g. Coolify) → still `NEEDS_MANUAL_CREDENTIALS` (unchanged).
- The permission dialog still shows the single "Authenticating… opening a browser window" notice for both the Minds and self-setup attempts; no new UI.
- While the constants are placeholders, the Minds attempt is still made and simply fails through to self-setup (real creds will be dropped in before release).

## Implementation plan

### `libs/mngr_latchkey/imbue/mngr_latchkey/core.py`

- **New module constants** (strictly hardcoded, placeholders for now):
  - `MINDS_GOOGLE_OAUTH_CLIENT_ID: Final[str]` and `MINDS_GOOGLE_OAUTH_CLIENT_SECRET: Final[str]` — the single Minds client pair, reused for every google service.
  - `GOOGLE_SERVICE_NAME_PREFIX: Final[str] = "google-"` — the gate for which services use the Minds client.
- **`Latchkey.auth_list(self) -> frozenset[str]`** — runs `latchkey auth list`, parses the JSON object, returns the set of service names that have a registered client/credential entry. Mirrors `services_info`'s degradation contract: any process error, non-zero exit, or malformed JSON returns `frozenset()` (treated as "nothing registered", per the not-registered default). Uses `_build_env_with_latchkey_directory` (local mode, `LATCHKEY_GATEWAY` cleared) and a short timeout like `_SERVICES_INFO_TIMEOUT_SECONDS`.
- **`Latchkey.auth_prepare(self, service_name: str, client_id: str, client_secret: str) -> tuple[bool, str]`** — builds the JSON payload with `json.dumps({"clientId": ..., "clientSecret": ...})` and runs `auth prepare <service> <json>` through the existing `_run_latchkey_auth_command` helper (`argv=["auth", "prepare", service_name, payload]`). Returns `(is_success, detail)`.
- **`Latchkey.auth_browser_login(self, service_name: str) -> tuple[bool, str]`** — new public "bare" browser login: a single `auth browser <service>` with **no** `browser-prepare` fallback, via `_run_latchkey_auth_command(argv=["auth", "browser", service_name])`. Used for the Minds path and the already-registered path.
- **Refactor `Latchkey.auth_browser`** (the existing self-setup chain, step 3) to call `auth_browser_login` for its first attempt, then keep its current "if the failure says `browser-prepare`, run it and retry" logic. Behavior is unchanged; the bare attempt is just factored out. Call site in `predefined.py` keeps using `auth_browser` for self-setup.

### `apps/minds/imbue/minds/desktop_client/latchkey/handlers/predefined.py`

- **`LatchkeyPermissionGrantHandler._authenticate_google(self, service_name: str) -> tuple[bool, str]`** — new private orchestration helper (keeps `grant()` readable; orchestration stays in the handler):
  - `already_registered = service_name in self.latchkey.auth_list()`.
  - If not `already_registered` (Minds attempt): `prepared, _ = self.latchkey.auth_prepare(service_name, MINDS_GOOGLE_OAUTH_CLIENT_ID, MINDS_GOOGLE_OAUTH_CLIENT_SECRET)`; if `prepared`, `ok, detail = self.latchkey.auth_browser_login(service_name)` and return on success.
  - If `already_registered`: `ok, detail = self.latchkey.auth_browser_login(service_name)` (reuse the registered client, no prepare) and return on success.
  - On **any** failure of the fast attempt above (Minds or already-registered), fall through to `return self.latchkey.auth_browser(service_name)` — the existing self-setup pathway (`browser-prepare` + `browser`), which intentionally overwrites a failed registration with the user's own client.
- **Modify `grant()`** at the existing "not valid + browser supported" branch (currently the single `self.latchkey.auth_browser(...)` call): replace the call with a gate:
  - `if service_info.name.startswith(GOOGLE_SERVICE_NAME_PREFIX): is_success, detail = self._authenticate_google(service_info.name)`
  - `else: is_success, detail = self.latchkey.auth_browser(service_info.name)` (non-google unchanged).
  - The surrounding `FAILED` / apply-grant / response-event logic is untouched.
- **Import** the three new constants from `imbue.mngr_latchkey.core`.
- `render_request_detail_fragment` / `will_open_browser` need **no change** — google services are not-valid + browser-capable, so the notice already shows.

### `libs/mngr_latchkey/imbue/mngr_latchkey/testing.py`

- **Add overrides to `FakeLatchkey`** for `services_info`, `auth_browser`, and the new primitives (`auth_list`, `auth_prepare`, `auth_browser_login`): configurable results and ordered call-sequence recording, without spawning subprocesses, so tests can assert the 1→2→3 ordering. (The current `FakeLatchkey` only fakes the gateway/password/jwt lifecycle, so these are new overrides. Per CLAUDE.md, no test file for `testing.py` itself.)

### Changelog (append to existing branch entries)

- This branch (`preston/minds-oauth`) already has `apps/minds/changelog/preston-minds-oauth.md` and `libs/mngr_latchkey/changelog/preston-minds-oauth.md` (from the latchkey 2.18.0 bump). Append a bullet to **each** describing the Minds Google OAuth fallback (one per project, double-newline-separated bullets).

## Implementation phases

- **Phase 1 — Latchkey primitives + constants.** Add the constants and `auth_list` / `auth_prepare` / `auth_browser_login` to `core.py`, refactor `auth_browser` to reuse the bare login, and extend `FakeLatchkey`. Add unit tests for the primitives. System still behaves as today (nothing calls the new methods yet).
- **Phase 2 — Handler orchestration.** Add `_authenticate_google` and the google/non-google gate in `grant()`. Add unit tests asserting the full 1→2→3 call ordering across branches. Feature is now live for google services.
- **Phase 3 — Acceptance test + changelog.** Add one acceptance test exercising the end-to-end fallback ordering and append the changelog bullets. Run the full suite.

## Testing strategy

- **Unit — `core.py` primitives** (in `core_test.py`, following existing patterns that point `Latchkey` at a temp `latchkey_directory` with a recording fake `latchkey` binary):
  - `auth_list` parses a populated JSON object into the right service-name set; returns `frozenset()` on non-zero exit, missing binary, and malformed JSON.
  - `auth_prepare` invokes `auth prepare <service> <json>` with a correctly-serialized `{"clientId","clientSecret"}` payload; maps exit code → `(is_success, detail)`.
  - `auth_browser_login` runs a single `auth browser` with no `browser-prepare` retry.
  - `auth_browser` (self-setup) still runs `browser-prepare` + retry when the first attempt reports preparation is required (regression guard for the refactor).
- **Unit — `grant()` orchestration** (in `predefined_test.py`, using the extended `FakeLatchkey` + `build_fake_gateway_client`), asserting both outcome and the recorded auth-call sequence:
  - google, creds valid → grant applied, no auth calls.
  - google, not registered, prepare ok, login ok → `GRANTED`; sequence is `auth_prepare` → `auth_browser_login`; self-setup `auth_browser` never called.
  - google, not registered, prepare ok, login fails → falls to `auth_browser` (self-setup); `GRANTED` if that succeeds, `FAILED` if not.
  - google, not registered, prepare fails → falls straight to `auth_browser` (self-setup).
  - google, already registered, login ok → `auth_browser_login` only (no `auth_prepare`); login fails → falls through to self-setup `auth_browser` (`GRANTED` if it succeeds, `FAILED` if not).
  - `auth_list` read failure → treated as not-registered → Minds `auth_prepare` attempted.
  - non-google, not valid → `auth_browser` (self-setup) only; behavior identical to today.
  - service without browser option → `NEEDS_MANUAL_CREDENTIALS` (unchanged).
- **Acceptance (`@pytest.mark.acceptance`)** — one end-to-end test driving `grant()` through the three google paths in sequence (Minds-success, Minds-fail→self-setup, already-registered→straight-to-browser) via a configured `FakeLatchkey`, asserting the exact latchkey call ordering, the resulting `GrantResult`, and the post-grant `latchkey_permissions.json`.
- **Edge cases:** placeholder/empty creds still attempted (no non-empty gate); `INVALID` (expired) and `MISSING` both treated as "not valid"; the `google-` prefix matches all 8 catalog services and nothing else; `will_open_browser` notice unchanged.

## Resolved decisions

- **Constants location:** the Minds Google client constants live in `mngr_latchkey/core.py` (the layering smell of Minds-specific values in an otherwise Minds-agnostic library is accepted).
- **`auth list` read-failure default:** default to "not registered" and attempt `auth prepare`. The small risk of re-registering over an existing client on a (rare, local) read failure is accepted.
- **Already-registered failure:** on a failed `auth browser` for an already-registered service, fall through to the existing self-setup pathway (not a terminal `FAILED`).
- **Self-setup overwriting a registration:** intended — when the fast attempt fails and we fall to `auth browser-prepare`, it overwrites the failed (Minds or registered) client with the user's own.

## Open questions

- **Out of code scope:** the real Minds Google Cloud OAuth client must have all needed scopes enabled (gmail, calendar, drive, docs, sheets, people, analytics, directions) and its consent screen published; that is an infra/config task, not part of this change.
