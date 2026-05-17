# Manual probe findings — `minds env deploy` + deployed services

Branch: `mngr/env-testing` · Date: 2026-05-17 · Activated env: `dev-josh-1`

Goal: walk through realistic deployment scenarios against the existing `dev-josh-1`
environment and the deployed apps; identify bugs and surprising behavior. Not writing
formal tests; this is a checklist of probes + findings.

## TL;DR — top findings by severity

**Bugs that break user-visible flows:**

- **F5**: Three connector endpoints (`/auth/session/revoke`, `/auth/email/is-verified`, `/auth/email/send-verification`) always return 500 due to syncio SuperTokens helpers called inside `async def`. Onboarding / sign-out flow broken today.
- **F9**: `POST /anthropic/v1/messages` on the LiteLLM proxy always returns 400 — the documented `ANTHROPIC_BASE_URL=https://.../anthropic` integration is broken because the proxy config has no `pass_through_endpoints` block. Workaround: clients must use `/chat/completions`.
- **F16**: For dev-tier deploys, Modal env / Neon project / SuperTokens app are created BEFORE the recover-target file is written. A failure in those steps leaves cloud resources behind with no rollback path.
- **F17**: Pool-hosts schema migrations only run for `creates_resources=true` tiers. Staging and production silently skip them — their `pool_hosts` table will drift from dev's schema over time.
- **F19**: `minds env recover` skips Modal rollback when `version is None` (first-ever deploy) but deletes the Modal Secrets. The deployed app stays running, now pinned to deleted secrets — broken at next request.

**Bugs in error paths the operator depends on:**

- **F13/F14**: `minds env recover` on a corrupt or empty recover-target JSON dumps a raw Python traceback instead of a typed `RecoverFailedError`. Recover is the operator's escape hatch from a stuck state — this is exactly the wrong UX for that audience.
- **F27**: Async connector endpoints have no `handle_endpoint_errors` wrapping, so any unhandled exception in them becomes a bare 500 with no detail (F5 is the most visible symptom).
- **F6**: `_authenticate_supertokens`'s explicit `"Email not verified"` error message is dead code — the SuperTokens session getter rejects unverified tokens at the validator step first, so users always see `"Invalid token"` instead.

**Smells / latent footguns:**

- **F2**: Health-check `per_attempt_timeout=3s` is likely too tight for real Modal cold-boots (a cold container can sit on the request for 10+ seconds before sending bytes).
- **F10**: `/key/info` returns stale `spend=0` while admin `/spend/keys` shows the real value. Connector `/keys/{id}` always shows 0 to end users.
- **F15**: Deploy mints + logs a `deploy_id` and reads Vault credentials BEFORE the "outside monorepo" check fires.
- **F18**: `writes_local_state=true` + `creates_resources=false` would assert-error after both Modal deploys succeed.
- **F20**: `recover.py` docstring + spec reference a `neon_restore_point_name` field that doesn't exist on `RecoverTarget` (spec drift).
- **F21**: Recover-target lacks the services list, so `_cleanup_orphan_secrets` uses suffix-match — could catch unrelated names.
- **F22**: Destroy refuses if the env root was manually `rm -rf`'d — operator can't run `destroy` to clean cloud resources after that.
- **F24**: `per_env_*_url` hardcodes `dev` as the tier — would break any future non-dev PER_ENV tier.
- **F25**: Vault entry at `secrets/.../litellm-connector` is silently ignored (the service is in `_DERIVED_ONLY_SECRET_SERVICES`).
- **F26**: `write_recover_target_atomic` race window between `.exists()` and `os.replace` — defeats the "two parallel deploys both fail safely" claim.
- **F28**: FCT `vendor/mngr` and minds-side `libs/mngr` are out of sync — workspace agents created today don't have unreleased mngr fixes.

**Nits / cosmetic:**

- **F1**, **F4**: misleading comments / stale spec wording (no code bug).
- **F3**: connector has no `/health` route (uses `/docs` instead — works but heavy).
- **F7**: tunnel DELETE not idempotent at the HTTP layer.
- **F8**: orphan-tunnel hygiene risk for interrupted flows.
- **F11**: `minds env list` says `(no client.toml)` for production/staging tiers even though they use the in-repo client.toml.
- **F12**: legacy `MINDS_ROOT_NAME=devminds` warning fires on every `minds <anything>` invocation.
- **F23**: destroy step 1 (`mngr destroy`) and step 3 (Cloudflare sweep) double-handle agent-owned tunnels.
- **F29**: Hardcoded "Please ask Josh to provision more" in a 503 from `/hosts/lease`.
- **F30**: `release_host` not idempotent (404 on second call).
- **F31**: `lease_host` partial-SSH-key-injection leaves the VPS with the user's key while rolling back the DB.

## Decisions at a glance

(each finding has full reasoning below in its **Decision:** line)

| ID | Suggested decision | Touches |
|----|--------------------|---------|
| F1 | Reword the comment | `connector/app.py` |
| F2 | Bump health-check timeout 3s→10s, max 30s→60s | `health_check.py` |
| F3 | Add `/health/liveness` route + switch probe | `connector/app.py`, `health_check.py` |
| F4 | Update spec to say `/health/liveness` | spec.md |
| F5 | **Convert all 12 connector `async def` endpoints to sync `def`** (asyncio → syncio SuperTokens; style-guide compliance) | `connector/app.py` |
| F6 | `override_global_claim_validators=lambda *_: []` so "Email not verified" becomes live | `connector/app.py` |
| F7 | DELETE tunnel returns 200 on already-gone | `connector/app.py` |
| F8 | Defer (test-suite fixture concern) | none |
| F9 | Add `pass_through_endpoints` for anthropic to LiteLLM config | `modal_litellm/app.py` |
| F10 | Connector `/keys/{id}` reads spend from `/spend/keys` | `connector/app.py` |
| F11 | `minds env list` shows "(in-repo client.toml)" for reserved tiers | `provisioning.py`, `cli/env.py` |
| F12 | Demote legacy-`MINDS_ROOT_NAME` warning to debug | `bootstrap.py` |
| F13 + F14 | Add `RecoverTargetCorruptError`; catch JSON + pydantic errors | `recover.py`, `cli/env.py` |
| F15 | Move `find_monorepo_root()` to first line of `deploy_env`; vault read after | `provisioning.py`, `cli/env.py` |
| F16 | Write tentative recover-target BEFORE step 1; finalize after snapshot | `provisioning.py`, `recover.py` |
| F17 | **Confirm intent first**, then move pool-hosts migrations out of `creates_resources` gate (or update spec) | `provisioning.py` (or spec.md) |
| F18 | Add model validator forbidding `writes_local_state=true` + `creates_resources=false` | `config/data_types.py` |
| F19 | When `version is None`, recover calls `stop_modal_app` instead of skipping | `recover.py` |
| F20 | Update docstring + spec to drop "restore-point" terminology | `recover.py`, spec.md |
| F21 | Add `services` field to `RecoverTarget`; delete exact names | `recover.py`, `provisioning.py` |
| F22 | Add `--force-without-env-root` flag to destroy | `cli/env.py`, `provisioning.py` |
| F23 | Defer (works correctly; release-test invariant later) | none |
| F24 | Thread `tier` through `per_env_*_url` helpers | `per_env_deploy.py`, `provisioning.py` |
| F25 | Drop `_DERIVED_ONLY_SECRET_SERVICES`; read litellm-connector from Vault normally | `per_env_deploy.py` |
| F26 | Use `os.open(O_CREAT \| O_EXCL)` for recover-target write | `recover.py` |
| F27 | Subsumed by F5 (sync conversion + `handle_endpoint_errors`); add a defensive `exception_handler(Exception)` as a last-resort net | `connector/app.py` |
| F28 | Defer (run `release-minds` skill intentionally) | workflow op |
| F29 | Replace hardcoded "Ask Josh" with generic phrasing | `connector/app.py` |
| F30 | Bundle with F7: release_host returns 200 on already-released | `connector/app.py` |
| F31 | Defer (low blast radius, document orphan growth) | docstring only |

**Quick stats:** 24 findings get code changes (some bundled into shared PRs), 4 deferred (F8, F23, F28, F31), 3 docs-only (F4, F20, partial F21+F25 docs).

## What I didn't test (gaps)

- **No fresh deploy of a new env.** Per the user's note that fresh deploys are slow and leak resources, I stayed on dev-josh-1 for non-destructive probes and inspected the deploy/destroy code paths from the source. F16, F17, F19 are code-read findings that should be confirmed by an actual fresh-deploy-then-induce-failure-then-recover cycle.
- **No destroy of dev-josh-1.** Would have validated destroy ordering / `keep_last=0` GC / generation-id removal end-to-end. Deliberately preserved the env for further probes.
- **No exercise of `/hosts/lease`** (pool-host flow). The DB has no leasable pool hosts on dev-josh-1; can't test without provisioning a VPS.
- **No tier (staging/production) deploy or destroy attempt.** Out of scope today.
- **No FCT workspace end-to-end** (`minds run` → create agent → agent boots → services register → Cloudflare tunnel routes traffic).

## Convention

Each finding has:

- **Severity** (suspected): `bug` (definite wrong behavior), `smell` (suspicious but
  needs confirmation), `gap` (missing functionality / undocumented limitation),
  `nit` (cosmetic).
- **Where** — exact file:line(s).
- **Repro** — exact command(s) or steps.
- **Observed** vs **Expected**.
- **Why it might matter**.

## Scenarios to walk

(checked off as I go; see Findings below for what each one turned up)

- [x] 1. `/generation` empty-string on dev tier — bug or intended? → **intended**
- [x] 2. Health endpoints behave as `await_apps_healthy` expects — **mostly OK; one smell**
- [x] 3. SuperTokens auth round-trip via connector — **BIG bug found**
- [x] 4. Cloudflare tunnel create/list/delete — **mostly OK; small idempotency nit**
- [x] 5. LiteLLM virtual key + Anthropic call + spend tracking — **two real bugs**
- [x] 6. Connector `/keys/*` ↔ LiteLLM proxy round-trip — works for paid users; surfaces F10
- [x] 7. `minds env list` (active marker, output format) — works; F11 misleading "no client.toml" wording
- [x] 8. `minds env activate --create <new-name>` then list — works
- [x] 9. Invalid dev env name refusal — works
- [x] 10. `minds env deactivate` round-trip — works
- [x] 11. Preflight: recover-target file refusal — works for valid file; **F13/F14 ungraceful on corrupt/empty**
- [x] 12. Deploy refusal outside monorepo — works but **F15 deploy id minted before check fires**
- [x] 13. Deploy refusal for staging/production without `--yes-i-mean-*` — works
- [ ] 14. `--keep-agents` flag plumbing — deferred (docstring already says it's a noop)
- [x] 15. Code-read pass: orchestration order vs spec — F16, F17, F18 found
- [x] 16. Code-read pass: idempotency claims — F19, F20 found
- [x] 17. Code-read pass: failure / recover paths — F21, F22, F23 found
- [x] 18. Vault access semantics (missing/extra fields) — F24, F25 found

## Findings

(numbered as I find them; not in any particular order)

### F1 — `/generation` empty-string on dev tier is intended, but comment is misleading

- **Severity:** nit
- **Where:** `apps/remote_service_connector/imbue/remote_service_connector/app.py:1523-1525` and surrounding doc comment.
- **Observed:** `GET https://minds-dev-dev-josh-1--rsc-dev-api.modal.run/generation` returns `{"generation_id":""}`.
- **Why:** Dev tier has `lifecycle.tracks_generation = false` (see `apps/minds/imbue/minds/config/envs/dev/deploy.toml:31`). `deploy_env` only calls `ensure_generation_id` when `tracks_generation` is true (`provisioning.py:582-584`), so no `MINDS_TIER_GENERATION_ID` ever gets pushed into the dev `litellm-connector-<tier>` Modal Secret. The endpoint's `os.environ.get(_GENERATION_ID_ENV_VAR, "")` returns `""`.
- **Smell:** The doc comment at `app.py:1523-1525` says: *"Empty when the deploy didn't push one (e.g. an older deploy from before this branch landed) so the endpoint is always callable; the client uses an empty string as 'no generation tracked yet'."* — This frames empty as a *legacy / transitional* state, but for the entire dev tier it's the **steady state**. Anyone reading the comment may mistakenly think a dev-tier empty id is a bootstrap artifact and try to "fix" it. Recommend rewording to call out that empty is the normal steady state when `tracks_generation=false`.
- **Not a bug**, but the comment will lead someone astray.
- **Decision:** Reword the comment at `app.py:1523-1525` to make clear that empty is the **steady state** for any tier with `tracks_generation=false` (not a legacy artifact). Two-line change, no behavior change.

### F2 — `per_attempt_timeout=3.0s` is likely too tight for real Modal cold-boots

- **Severity:** smell (needs reproduction during an actual fresh deploy)
- **Where:** `apps/minds/imbue/minds/envs/health_check.py:49` (`_DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS: Final[float] = 3.0`).
- **Observed:** When `dev-josh-1`'s LiteLLM proxy was cold, my first `GET /health/liveness` hung for ≥10 seconds before I cancelled. A subsequent (warm) request returned in 525 ms. This means the cold-boot includes a window where the request just sits without any byte coming back.
- **Why it might matter:** `await_apps_healthy` (a) calls `client.get(url, timeout=3.0)` (httpx `timeout` here applies to the whole request including read), (b) treats a `httpx.TimeoutException` as *transient*, and (c) sleeps `poll_interval=2.0s` between attempts. So each "cold poll" costs ≈ 5 s wall-clock (3 timeout + 2 interval). With `max_seconds=30s`, only ~6 polls fit. If Modal's container cold-boot takes >25 s before the connector's first byte (Python import + FastAPI startup is in this range for our image), the connector poll runs out the clock and emits `HealthCheckFailedError` even though the app is in fact fine and would have answered after ~30 s. This converts a successful deploy into a deploy that triggers `minds env recover` — which would then roll back to a no-op for the dev tier (no Neon writes happened, prior app versions are still good) but still leaves a recover-target file the operator has to clear.
- **Repro idea (not yet executed — destructive):** Force a cold deploy via `modal app stop rsc-dev` for `dev-josh-1`, then watch the next `minds env deploy` health-check window. I haven't done this — preserving dev-josh-1 per the user's request not to thrash it unnecessarily.
- **Suggested fix:** Bump `_DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS` to 10 s and `_DEFAULT_MAX_SECONDS` to 60 s; or treat httpx connect/read timeouts as transient (already done) AND extend the cold-boot tolerance window such that elapsed-vs-budget math gives the cold app a realistic chance.
- **Decision:** Bump the two constants — `_DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS = 10.0` and `_DEFAULT_MAX_SECONDS = 60.0`. Skip the cold-boot-window refactor unless we see a real failure under the new budgets. Update the corresponding `health_check_test.py` snapshots / parametrized cases.

### F3 — Connector exposes no `/health` route; `/health/liveness`-style probe missing too

- **Severity:** gap (low priority)
- **Where:** `apps/remote_service_connector/imbue/remote_service_connector/app.py` — no `@web_app.get("/health")` decorator anywhere.
- **Observed:** `GET https://.../rsc-dev-api.modal.run/health` → 404 `{"detail":"Not Found"}`.
- **Why:** `await_apps_healthy` uses `/docs`, which works because FastAPI auto-mounts it. But `/docs` is a pretty heavy probe (returns 4 KB of HTML, mounts the Swagger UI which can be slow during cold-boot). A dedicated `/health/liveness` (like LiteLLM's) would be cleaner and faster.
- **Not blocking**, but worth a tiny PR: add a no-auth `/health/liveness` route returning `{"status":"ok"}` and switch the health-check URL to it.
- **Decision:** Add a `@web_app.get("/health/liveness")` route returning `{"status":"ok"}`. Update `health_check.py:232` to use `/health/liveness` instead of `/docs` (smaller, faster probe). Symmetric with the LiteLLM proxy's liveness route. Update the docstring in `health_check.py` that explains why `/docs` was chosen.

### F4 — `GET /health` on the LiteLLM proxy returns 401, not 200

- **Severity:** nit / documentation gap
- **Where:** `apps/modal_litellm/app.py` (LiteLLM's native FastAPI app — we don't own this).
- **Observed:** `GET https://.../llm-dev-proxy.modal.run/health` → `HTTP 401 {"error":{"message":"Authentication Error, No api key passed in.",...}}`. The `/health/liveness` variant is the unauthenticated probe.
- **Why it matters:** The spec at `apps/minds/specs/minds-deploy-safety-overhaul/spec.md` says ("Health check" section): *"Endpoints: `<connector_url>/generation` (expects 200 with the generation-id-shaped response), `<litellm_proxy_url>/health` (expects 200)."* But the actual implementation in `health_check.py:233` uses `/health/liveness`, not `/health` — and that's correct, because `/health` requires auth. **The spec is stale here.** Anyone reading it will think `/health` is meant.
- **Not a code bug** (the implementation is correct), but the spec needs updating.
- **Decision:** Update `specs/minds-deploy-safety-overhaul/spec.md`'s "Health check" section to say `/health/liveness` instead of `/health`. One-line spec edit.

### F5 — `async def` connector endpoints invoking SuperTokens **syncio** helpers always 500

- **Severity:** **bug** (high — these endpoints are unusable today)
- **Where:**
  - `apps/remote_service_connector/imbue/remote_service_connector/app.py:71` imports `get_session_without_request_response` from `supertokens_python.recipe.session.syncio`.
  - `apps/remote_service_connector/imbue/remote_service_connector/app.py:79` imports `get_user` from `supertokens_python.syncio`.
  - Both are then called inside `async def` endpoints:
    - `auth_revoke_sessions` (line 2236) — via `_get_user_id_from_access_token` (line 2253) → `get_session_without_request_response` (line 1347).
    - `auth_is_email_verified` (line 2277) — direct `get_user` call (line 2280).
    - `auth_send_verification_email` (line 2260) — direct `get_user` call (line 2263).
  - The same syncio helpers are also called from non-async endpoints, where it does work (FastAPI's threadpool wrapping gives them their own loop). That's why `/hosts`, `/auth/users/<id>`, `/tunnels` (sync def) are healthy.
- **Observed:** `POST /auth/session/revoke`, `POST /auth/email/is-verified`, `POST /auth/email/send-verification` all return `500 Internal Server Error`. Connector logs (via `modal app logs rsc-dev --env dev-josh-1`) show:

  ```
  File "/usr/local/lib/python3.12/site-packages/supertokens_python/async_to_sync_wrapper.py", line 57, in sync
      return loop.run_until_complete(co)
  ...
  RuntimeError: This event loop is already running
  ```
- **Why:** SuperTokens' `syncio` module wraps the asyncio version with `loop.run_until_complete(coro)`. From a FastAPI `async def` endpoint we're already inside the uvicorn event loop, so the wrapper trips the standard `asyncio.run`-from-running-loop guardrail.
- **Repro (commands):**

  ```bash
  RSC=https://minds-dev-dev-josh-1--rsc-dev-api.modal.run
  EMAIL="probe-$(uuidgen | head -c8)@example.com"
  # Sign up to get an access token + user id
  J=$(curl -sS -X POST -H "Content-Type: application/json" \
        -d "{\"email\":\"$EMAIL\",\"password\":\"ProbeProbe1234!\"}" $RSC/auth/signup)
  ACCESS=$(echo "$J" | jq -r '.tokens.access_token')
  USERID=$(echo "$J" | jq -r '.user.user_id')

  # All three of these 500:
  curl -i -X POST -H "Authorization: Bearer $ACCESS" $RSC/auth/session/revoke
  curl -i -X POST -H "Content-Type: application/json" \
        -d "{\"user_id\":\"$USERID\",\"email\":\"$EMAIL\"}" $RSC/auth/email/is-verified
  curl -i -X POST -H "Content-Type: application/json" \
        -d "{\"user_id\":\"$USERID\",\"email\":\"$EMAIL\"}" $RSC/auth/email/send-verification
  ```

- **Why it matters:** `/auth/session/revoke` is the sign-out path. `/auth/email/send-verification` is how users get verification emails. `/auth/email/is-verified` is the client-side poll to know whether to unblock the UI. With these three broken, end users **cannot**: complete email verification on signup, sign out cleanly, or check verification status. The whole onboarding flow for any minds env that requires email-verified access is effectively broken.
- **Suggested fix:** Switch both imports to their asyncio counterparts (`supertokens_python.recipe.session.asyncio.get_session_without_request_response`, `supertokens_python.asyncio.get_user`) and convert `_get_user_id_from_access_token` + the three endpoint bodies to `async def` / `await` the calls. For the call sites that today live inside *sync* endpoints (`/hosts`, `/keys/*`, `/tunnels/*`, `/auth/users/{id}`, `_default_email_getter`), the same `await` rewrite is required — converting those endpoints to `async def` as well is the simplest cross-cutting fix. Alternatively introduce an `async _get_user_id_from_access_token_async` for the async sites and leave the sync site unchanged.
- **Decision:** **Convert every `async def` endpoint in `connector/app.py` (all 12) to sync `def`.** Switch every `from supertokens_python.recipe.X.asyncio import Y` to `.syncio import Y`, drop every `await`, drop `async` from the function defs. Style-guide compliance ("Never use `async` or `asyncio`") AND the bug class can't recur. All SuperTokens functions the file uses have syncio variants — confirmed via direct introspection. Wrap each newly-sync endpoint with `with handle_endpoint_errors():` so error handling is consistent with the rest of the file (subsumes F27 for the connector). Add a release test that hits `/auth/session/revoke`, `/auth/email/is-verified`, `/auth/email/send-verification` end-to-end against a real SuperTokens app and asserts each returns a real status, not a bare 500. **Follow-up (separate, deferred):** tighten the asyncio ratchet at `apps/remote_service_connector/imbue/remote_service_connector/test_ratchets.py` to also count `async def` and `await` (currently only `import asyncio`) — done in the larger pass that converts the desktop_client's 19 async endpoints.

### F6 — `_authenticate_supertokens`'s "Email not verified" branch is dead code

- **Severity:** smell (debuggability — users get the wrong error message)
- **Where:** `apps/remote_service_connector/imbue/remote_service_connector/app.py:1313-1328`.
- **Observed:** Calling any admin-auth endpoint (e.g. `GET /tunnels`) with a valid access token for an unverified user returns `401 {"detail":"Invalid token"}` — *not* `401 {"detail":"Email not verified"}`.
- **Why:** `get_session_without_request_response(access_token=token, anti_csrf_check=False)` runs SuperTokens' global claim validators by default. The `EmailVerificationClaim` validator rejects the unverified token at session-fetch time, the SuperTokens SDK raises `SuperTokensSessionError`, and `_authenticate_supertokens` catches it at line 1318 → raises 401 "Invalid token". The explicit `if not is_verified: raise HTTPException(... "Email not verified")` at line 1327 is therefore never reached for the case it's trying to handle.
- **Why it matters:** A user whose verification email got delayed gets "Invalid token" with no hint about what to fix. They'll keep trying to sign in / refresh and assume their credentials are wrong.
- **Suggested fix:** Either (a) pass `override_global_claim_validators=lambda *_: []` to `get_session_without_request_response` so the helper reaches line 1326 and can return the distinct "Email not verified" message; or (b) inspect the session error type and surface a more specific message when it's an `EmailVerificationClaim` failure.
- **Decision:** Option (a) — pass `override_global_claim_validators=lambda *_: []` to the session getter call in `_authenticate_supertokens`. Smaller change, and the explicit `if not is_verified: raise 401 "Email not verified"` check at line 1327 becomes live code. Verify by re-running my F5 probe + asserting the 401 says "Email not verified" before email verification.

### F7 — `DELETE /tunnels/{name}` returns 404 on second call (not strictly idempotent at the HTTP layer)

- **Severity:** nit (debatable — depends on intended semantics)
- **Where:** `apps/remote_service_connector/imbue/remote_service_connector/app.py:1563`-ish (the `@web_app.delete("/tunnels/{tunnel_name}")` route).
- **Observed:** First DELETE returns `200 {"status":"deleted"}`. Second DELETE on the same name returns `404 {"detail":"'Tunnel not found: ...'"}`.
- **Why it might matter:** Distributed-systems UX usually wants DELETE to be idempotent (404 → treat as already-gone and return 200/204). The current behavior means a client retry after a transient error sees 404 and has to special-case it. The `destroy` flow in `cloudflare_tunnels.delete_tunnels` already swallows 404 client-side, so this isn't a destroy bug — but any client (the desktop client, a curl-driven dev script) that retries DELETE will misread.
- **Suggested fix:** Either return 204 on already-gone, or document the 404 expectation at the OpenAPI level.
- **Decision:** Treat already-gone as 200 with `{"status":"already_deleted"}`. The `delete_tunnels` provider in `cloudflare_tunnels.py` already swallows 404 client-side, so this stays compatible with the destroy sweep. Apply the same treatment to F30 (`release_host`).

### F8 — Orphaned tunnels accumulate when probing fails partway

- **Severity:** smell (process / hygiene)
- **Where:** general flow; surfaces in my probe — after the first SuperTokens auth probe, the tunnel `b71fb7bb8a2749c9--probe-b793a3fc` is still alive in Cloudflare because the test session ended before deleting it (and the user that created it has an unverified email so its access token gets rejected; only the `minds env destroy` sweep would clean it up via the `metadata.env=<name>` tag).
- **Why it might matter:** Any user whose flow is interrupted partway through (browser tab closed mid-create, OAuth callback fails) leaks a tunnel that only `minds env destroy` will pick up. If we ever change the destroy filter or the tag, these become forever orphans. Real failure-injection tests for the deploy will need a cross-test cleanup pass, otherwise the test runner's Cloudflare account fills up over time.
- **Not a bug in the deploy code**, but worth noting as a hygiene concern for any future automated test suite.
- **Decision:** Defer — no code change today. Address as a fixture in the eventual release test suite (a session-scoped autouse fixture that sweeps `metadata.test=true` tagged tunnels at session end). The destroy-time `metadata.env=<name>` sweep is the production-side backstop.

### F9 — LiteLLM `/anthropic/v1/messages` pass-through is broken (no `pass_through_endpoints` configured)

- **Severity:** **bug** (high — this is the *documented* way Claude Code connects via `ANTHROPIC_BASE_URL`)
- **Where:**
  - `apps/modal_litellm/app.py:90-98` — `LITELLM_CONFIG["general_settings"]` lacks the required `pass_through_endpoints` block.
  - `apps/modal_litellm/README.md:10` and `apps/modal_litellm/app.py:17` both document this endpoint as the way to point `ANTHROPIC_BASE_URL` at the proxy.
- **Observed:** Every call to `POST /anthropic/v1/messages` returns:

  ```
  HTTP 400 {"error":{"message":"{\"type\":\"error\",\"error\":{\"type\":\"invalid_request_error\",\"message\":\"anthropic-version: header is required\"}, ...
  ```

  even when the client passes `anthropic-version: 2023-06-01` (any casing). The error originates from the upstream Anthropic API: LiteLLM proxies through but doesn't carry the client's header. Per LiteLLM docs (v1.85.0), the path `/anthropic/*` requires either an explicit `pass_through_endpoints` config or the new `/anthropic/v1/messages` route LiteLLM added — neither of which the deployed config enables.
- **Repro:**

  ```bash
  LLM=https://minds-dev-dev-josh-1--llm-dev-proxy.modal.run
  # (use master key here for brevity; same failure with a virtual key)
  MASTER=$(vault kv get -format=json secrets/minds/dev/litellm | jq -r '.data.data.LITELLM_MASTER_KEY')
  curl -sS -X POST -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
       -H "anthropic-version: 2023-06-01" \
       -d '{"model":"claude-haiku-4-5-20251001","max_tokens":4,"messages":[{"role":"user","content":"hi"}]}' \
       "$LLM/anthropic/v1/messages"
  ```

- **Why it matters:** Per the README this is THE endpoint Claude Code uses (`ANTHROPIC_BASE_URL=https://.../anthropic`). It's the headline feature of the proxy: a virtual key that Claude Code can use transparently. Anyone wiring this up today will get `400 anthropic-version: header is required` for every prompt. Workaround is to use `/chat/completions` which is OpenAI-shape and works fine — but that requires Claude Code (or any Anthropic SDK client) to be reconfigured.
- **Suggested fix:** Add a `pass_through_endpoints` block to `LITELLM_CONFIG["general_settings"]` for `anthropic` (and/or pin the LiteLLM proxy to a version whose `/anthropic/v1/messages` auto-pass-through is enabled). Then add a release-time integration test that does one round-trip through it with the `claude` CLI.
- **Decision:** Add the `pass_through_endpoints` block under `general_settings` with one entry mapping `/anthropic` → `https://api.anthropic.com` and forwarding the `anthropic-version` + `x-api-key` headers. Pin litellm to a known-good version in `pyproject.toml` if 1.85's behavior is the regression. Validate by re-running the F9 probe via this branch's worktree (a redeploy is required — coordinate with you before doing it).

### F10 — `/key/info` returns stale `spend=0` minutes after the actual usage; admin `/spend/keys` has the real value

- **Severity:** smell (UX bug — users won't see their spend) → likely upstream LiteLLM behavior we have to work around
- **Where:**
  - `apps/modal_litellm/app.py` — the proxy config doesn't override the spend-write-through cadence.
  - `apps/remote_service_connector/imbue/remote_service_connector/app.py:1947, 1976, 2003` — connector's `/keys/{key_id}`, `/keys/{key_id}/budget`, `DELETE /keys/{key_id}` all rely on `/key/info` for the spend column.
- **Observed:** After 5 chat completions through a virtual key, `/key/info?key=<sk-...>` still returned `spend: 0`. `/spend/keys` (admin endpoint) shows `spend: 0.00014` for the same key. After waiting 10 seconds, `/key/info` still shows 0.
- **Why it matters:** The connector exposes `/keys/{key_id}` as the user-facing endpoint for "how much have I spent against this key?" Today that endpoint always shows 0 unless the user happens to wait long enough for LiteLLM to flush LiteLLM_VerificationToken.spend (which appears not to happen on a short test horizon — likely a write-back/flush interval we don't control). The connector's `/keys` (list) endpoint *also* gets stale spend.
- **Repro:** see commands in the LiteLLM probe section above; main flow is `POST /key/generate` → 5×`POST /chat/completions` → `GET /key/info?key=...` shows 0.
- **Suggested fix:** Either (a) make the connector fall back to `/spend/keys` (or `/global/spend/keys`) for the spend column while keeping `/key/info` for the budget config, or (b) configure LiteLLM to write spend back to LiteLLM_VerificationToken on every request (cost overhead but accurate user-facing display). Worth checking LiteLLM docs for a config flag that forces synchronous spend writes.
- **Decision:** Option (a) — connector's `/keys/{id}` and `/keys` (list) supplement `/key/info` with a call to `/spend/keys?user_id=<user_id>` (or `/global/spend/keys` for a single key), and use the latter's `spend` value. Keep `/key/info` as the source for budget config / ownership. Minimal surface area, no LiteLLM proxy changes. Confirm one LiteLLM call's worth of latency overhead is acceptable.

### F11 — `minds env list` reports `(no client.toml)` for production/staging tiers

- **Severity:** nit (misleading output)
- **Where:** `apps/minds/imbue/minds/envs/provisioning.list_dev_envs` (file: `provisioning.py`, plus the human-output formatter at `cli/env.py:867-872`).
- **Observed:**

  ```
  production    /home/rtard/.minds    (no client.toml)    (no client.toml under env_root)
  ```

  But production has a committed `apps/minds/imbue/minds/config/envs/production/client.toml` — that's the source of truth for the tier. `list` doesn't surface this.
- **Why it might matter:** A new operator running `minds env list` on a fresh checkout sees production marked "(no client.toml)" and reasonably assumes production is broken or unprovisioned. The list also doesn't distinguish "tier with repo-side client.toml" from "dev env that hasn't been deployed yet" (which both show the same `(no client.toml)` string today).
- **Suggested fix:** In `list_dev_envs`, fall back to `repo_tier_client_config_path(<tier>)` for reserved tier names. Render `(in-repo client.toml)` for that case and reserve `(no client.toml)` for the genuinely-unprovisioned dev env.
- **Decision:** Patch `list_dev_envs` to fall back to `repo_tier_client_config_path(<tier>)` for the reserved names. Render `(in-repo client.toml)` for tiers, `(no client.toml — run `minds env deploy`)` for unprovisioned dev envs. Also expose `client_config_source: "env_root" | "in_repo" | None` in the JSON / JSONL payloads so machine consumers can distinguish too.

### F12 — Legacy `MINDS_ROOT_NAME` warning fires on every `minds` invocation, including `--help`

- **Severity:** smell (noise)
- **Where:** `apps/minds/imbue/minds/bootstrap.py:68` (`resolve_minds_root_name`).
- **Observed:** `uv run minds <anything>` prints `WARNING | imbue.minds.bootstrap:resolve_minds_root_name:68 - MINDS_ROOT_NAME='devminds' does not match ...` once per invocation — including for `--help`, `env list`, `env activate ...`. Triggered when the parent shell still has a legacy env value.
- **Why it might matter:** docs say this is "harmless" but in practice it makes every minds command noisy for users still carrying around an old `MINDS_ROOT_NAME=devminds` from their shell rc. Anyone using `tab-complete` against `minds env activate <...>` sees the warning multiple times per second.
- **Suggested fix:** Demote to `logger.debug` (warnings are for things the user should act on; this is more like a transient compatibility note). Alternatively, fire the warning only when a command is about to operate against the fallback root — not on every single invocation.
- **Decision:** Demote to `logger.debug`. Single-line change at `bootstrap.py:68`. Anyone with the legacy var who wants visibility can `--verbose`.

### F13 — `minds env recover` against a corrupt recover-target JSON shows a raw traceback

- **Severity:** bug (UX — recover should converge gracefully)
- **Where:** `apps/minds/imbue/minds/envs/recover.py:125` (`RecoverTarget.from_json_bytes`). The wrapping `read_recover_target` does not catch `json.JSONDecodeError`.
- **Observed:** with the file containing the text `not json`:

  ```
  Traceback (most recent call last):
    File ".../recover.py", line 125, in from_json_bytes
      data = json.loads(raw)
    ...
  json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
  ```

- **Repro:**

  ```bash
  cd <monorepo root>
  echo 'not json' > .minds-deploy-recover-target.json
  uv run minds env recover
  ```

- **Why it matters:** A partially-written or hand-edited recover-target file is exactly the scenario `minds env recover` is supposed to handle — the operator can't do anything else (all other `minds env` commands refuse while it exists) until this is resolved. Bare tracebacks are scary for an operator already in a "something failed" headspace.
- **Suggested fix:** Catch `json.JSONDecodeError` in `read_recover_target` (or `from_json_bytes`) and raise a typed `RecoverTargetCorruptError(MindError)` with a message that tells the operator how to inspect / hand-clear the file. The CLI's existing `RecoverFailedError` handling at `cli/env.py:1110-1111` then renders it cleanly.
- **Decision:** Add `RecoverTargetCorruptError(MindError)`. Catch both `json.JSONDecodeError` AND `pydantic.ValidationError` (covers F14) in `read_recover_target`, re-raise as `RecoverTargetCorruptError` with the file path + a one-line "to inspect: cat <path>; to clear: rm <path>" hint. Register a handler in `cli/env.py:env_recover` so it renders cleanly (existing `RecoverFailedError` catch may need extending). Bundle F13 + F14 into one PR.

### F14 — Empty `{}` recover-target JSON dumps a raw pydantic ValidationError

- **Severity:** bug (UX — same theme as F13)
- **Where:** `apps/minds/imbue/minds/envs/recover.py:128` — `cls.model_validate(data)` raises `pydantic_core.ValidationError`, which propagates unwrapped.
- **Observed:** With `{}` written to the recover-target file:

  ```
  pydantic_core._pydantic_core.ValidationError: 7 validation errors for RecoverTarget
  deploy_id
    Field required [type=missing, input_value={}, input_type=dict]
  env_name
    Field required [type=missing, input_value={}, input_type=dict]
  ...
  ```

- **Repro:** `echo '{}' > .minds-deploy-recover-target.json && uv run minds env recover`
- **Why it matters:** Same as F13 — recover is the operator's escape hatch from a stuck state, and a 7-line pydantic dump is the wrong UX. Catch `pydantic.ValidationError`, wrap in a `RecoverTargetCorruptError`. (Both F13 and F14 likely want the same wrapper.)
- **Decision:** Covered by the F13 fix — `RecoverTargetCorruptError` catches both error types.

### F15 — `minds env deploy` mints + logs a `deploy_id` and reads Vault credentials before the "outside monorepo" check fires

- **Severity:** smell (the spec says deploy "refuses to start" outside the monorepo — implies preflight)
- **Where:**
  - `apps/minds/imbue/minds/cli/env.py:959` calls `_load_dev_credentials_from_vault(...)` BEFORE `deploy_env(...)`.
  - `apps/minds/imbue/minds/envs/provisioning.py:463-464` mints + logs the deploy_id BEFORE calling `find_monorepo_root()` at line 469.
- **Observed:** Running `minds env deploy` against an activated env from `/tmp` produces:

  ```
  Deploy id for env 'dev-josh-1': 20260517T223418Z
  Traceback ... NotInMonorepoError: Could not find monorepo root ...
  ```

  i.e. the operator sees a "Deploy id..." log message that implies the deploy is starting, *then* it fails. Also: Vault was read (cheap, but it counts as a side effect).
- **Why it matters:** Tiny operator-experience issue, plus the spec's promise that "no external mutation happens before the recover-target file is written" is broken in spirit by the early Vault reads. (The Vault read is technically read-only, so no real mutation — but it's the kind of detail the spec author probably wanted in the preflight bundle.) Also: the deploy_id is logged but unused, which is confusing — every other instance of a deploy_id log line correlates with mutations.
- **Suggested fix:** Move `find_monorepo_root()` to the very top of `deploy_env` (above `make_deploy_id`), and move the Vault read to inside `deploy_env` (or have the CLI call `find_monorepo_root()` ahead of `_load_dev_credentials_from_vault`).
- **Decision:** In `cli/env.py:env_deploy`, call `find_monorepo_root()` BEFORE `_load_dev_credentials_from_vault`. Move `make_deploy_id()` + the "Deploy id..." log INSIDE `deploy_env`, after the monorepo check (which itself moves to be the first line of `deploy_env`). No external mutation happens before the check now.

### F16 — Step 1 (Modal env / Neon / SuperTokens creation) runs BEFORE the recover-target file is written

- **Severity:** **bug** (rollback invariant broken)
- **Where:** `apps/minds/imbue/minds/envs/provisioning.py:477-509` runs the `creates_resources=true` Step 1 *before* the recover-target file is written at line 577. The pre-deploy snapshot creation (lines 547-559) is also before the file write.
- **Why it matters:** The spec at `specs/minds-deploy-safety-overhaul/spec.md` Recover-target file says: *"Created atomically (tempfile + fsync + rename) after preflight succeeds and after the Neon restore-point is created, **BEFORE any other external mutation.**"* But for dev-tier deploys, the code:
  1. Creates the Neon project (mutation #1)
  2. Creates the SuperTokens app (mutation #2)
  3. Runs schema migrations (mutation #3)
  4. Then creates the snapshot branch
  5. THEN writes the recover-target file

  If any of mutations #1-#3 partially succeed and then fail, the operator has no recover-target file. They've leaked a Neon project / SuperTokens app / partial schema with no rollback path: `minds env recover` will refuse-on-missing-file, `minds env deploy` will succeed-but-adopt-existing on the next attempt (provider creation is "idempotent on already-exists"), and the partial schema may break things.
- **Suggested fix:** Either (a) move provider creation AFTER recover-target write — but the snapshot needs the Neon project, so the Neon project create has to happen first; or (b) write a "tentative" recover-target file before Step 1, then re-write it with the snapshot id once that's captured. The spec's intent is clearly (b)-ish — the recover-target file gates everything.
- **Decision:** Option (b) — write a "tentative" recover-target file BEFORE Step 1, containing `null` for `neon_snapshot_branch_id` / `app_versions_to_restore` (since we don't know them yet). After the snapshot + version capture succeed, overwrite atomically with the final populated target. Recover-against-tentative-target either: skips Neon restore (no snapshot to restore from — log warning) and skips Modal rollback (no versions captured), then proceeds to orphan-secret cleanup. This makes the invariant "any cloud mutation has a recover path" hold. Update `recover_env` to tolerate `null` fields gracefully. Add a release test that injects a failure inside the Neon project creation step and asserts recover converges.

### F17 — Pool-hosts schema migrations only run for `creates_resources=true` tiers (skipped for staging/production)

- **Severity:** **bug** (likely — needs spec confirmation)
- **Where:** `apps/minds/imbue/minds/envs/provisioning.py:480-508` — the `apply_pool_hosts_migrations` call is nested inside `if lifecycle.creates_resources:`.
- **Why it matters:** The spec section "Deploy flow (single, unified)" says step 4 is *"Run migrations — pool-hosts schema_migrations-driven runner against the env's host_pool DB; Prisma migration against litellm_cost."* This is supposed to be unconditional. But staging/production have `creates_resources=false`, so the pool-hosts migration is silently skipped — meaning a new `*.sql` file shipped via PR will get applied to dev envs immediately but NEVER to staging/production unless the operator runs psql manually. That diverges the schemas over time.
  - Prisma migrations for litellm_cost do still run for all tiers because they're triggered inside `providers.deploy_litellm_proxy` (via `modal run migrate_db`), not in `provisioning.py`. So this asymmetry is real: Prisma migrations are universal, pool-hosts migrations are dev-only.
- **Suggested fix:** Move the `apply_pool_hosts_migrations` call out of the `if lifecycle.creates_resources:` block. For shared tiers, it needs the host_pool DSN — which lives in the operator-managed Vault entry `secrets/minds/<tier>/neon.NEON_HOST_POOL_DSN`. Read it via `read_per_env_secret_values("neon", ...)`. Add a release-time check that the schemas match across tiers.

  Open question first: confirm with you that the spec's intent IS to run pool-hosts migrations on every tier (vs. tier ops being expected to run them by hand). The spec language reads as "every deploy" but in practice staging/production migrations could be intentionally operator-controlled.
- **Decision:** **First, confirm intent with you.** If "every tier runs migrations on deploy" is the spec: move `apply_pool_hosts_migrations` outside the gate, read DSN from the operator-managed Vault entry for shared tiers, and add a `schema_migrations`-table comparison test that runs on PRs. If "tier ops control schema by hand": leave the code as-is and update the spec to document the asymmetry.

### F18 — `writes_local_state` is independent in the spec but coupled to `creates_resources` by `assert`

- **Severity:** smell (latent bug if config flexes)
- **Where:** `apps/minds/imbue/minds/envs/provisioning.py:670-671` asserts both `neon_record is not None` and `supertokens_record is not None` when `writes_local_state=true`. Both records are only populated when `creates_resources=true`.
- **Why it matters:** The spec's lifecycle table presents the four flags as independently configurable. Today's tier configurations happen to keep `writes_local_state` and `creates_resources` aligned. But anyone configuring a future tier with `writes_local_state=true` + `creates_resources=false` (e.g. an internal "tier" that uses operator-managed cloud resources but still wants a local client.toml for dev tooling) would hit `AssertionError` partway through deploy — AFTER both Modal apps have been deployed and health-checked. The recover-target file is still on disk, so recover would converge — but the operator would see a Python `AssertionError` traceback.
- **Suggested fix:** Either (a) document the coupling explicitly in the lifecycle spec (raise an `assert_never`-style explicit error early in `deploy_env`), or (b) decouple — `writes_local_state` should be able to source the DSNs / SuperTokens fields from Vault when `creates_resources=false`. (a) is the cheaper fix; (b) is the cleaner design.
- **Decision:** Option (a) — add a `DeployLifecycleConfig` model validator that raises `ValueError` if `writes_local_state and not creates_resources`. Fails at deploy.toml parse time, not partway through a deploy. Update the spec lifecycle table to note the constraint.

### F19 — Recover Step 1 silently skips Modal rollback when `version is None` (first-ever deploy), leaving the deployed app pinned to deleted secrets

- **Severity:** **bug** (high — recover claims to converge but leaves a broken app)
- **Where:** `apps/minds/imbue/minds/envs/recover.py:236-243`.
- **Why it matters:** Consider a brand-new env's first deploy that gets through `modal deploy` for both apps but then fails the health check. The recover-target carries `app_versions_to_restore = {rsc-tier: None, llm-tier: None}`. Recover:
  1. Step 1: rollback skipped because `version is None` (the warning explicitly says "Leaving the app at its current state").
  2. Step 2: Neon restored to snapshot — fine.
  3. **Step 3: every Modal Secret named `<svc>-<tier>-<deploy_id>` is deleted.**
  4. Step 4: recover-target deleted.

  After recover, the Modal apps still exist (deployed in this run, v1), but their pinned secrets are gone. The very next request to either app will fail at `Secret.from_name(...)` resolution. The operator's belief is "I rolled back the failed deploy"; the reality is "the apps are deployed but secret-less, will 500 on any request".
- **Suggested fix:** When `version is None`, the recover should `modal app stop` (or `modal app delete`) the app rather than skipping. Add a `stop_modal_app` call when no prior version exists. Optionally distinguish the two cases ("rolled back" vs "stopped") in the log output.
- **Decision:** When `version is None`, call `providers.stop_modal_app(app_name, target.modal_env, parent_cg)` instead of skipping. Distinguish the log lines: "Rolled back {app} to version {version}" vs "Stopped {app} (no prior version to roll back to)". `stop_modal_app` is already on the `Providers` bundle (used by destroy step 6) — reuse. Add a release test that simulates a first-deploy health-check failure and asserts both apps end up stopped.

### F20 — Recover-flow docstring references a `neon_restore_point_name` field that doesn't exist on `RecoverTarget`

- **Severity:** nit (doc / spec divergence)
- **Where:** `apps/minds/imbue/minds/envs/recover.py:222-227` says *"Step 2: Neon `restore_branch_to_named_restore_point` to the captured restore-point name (only if `neon_restore_point_name` is set)."* But the implementation at lines 254-261 actually checks `target.neon_snapshot_branch_id` / `target.neon_project_id` / `target.neon_branch_id` and calls `restore_branch_from_snapshot` (not a named restore-point). The spec at the top of `specs/minds-deploy-safety-overhaul/spec.md` uses the named-restore-point terminology too.
- **Why it matters:** Spec drift. The implementation took a different (and reasonable) approach — using a child branch + restore-from-branch instead of named restore-points — but the docstrings and the implementation plan in the spec haven't been updated. Anyone consulting the spec to debug a recover failure will look for a field name that doesn't exist on the recover-target file.
- **Suggested fix:** Update the recover.py docstring + the spec to match the branch-based implementation (and drop the dead "named restore point" terminology).
- **Decision:** Update `recover.py:222-227` docstring to describe the branch-based mechanism using `target.neon_snapshot_branch_id`. Update `specs/minds-deploy-safety-overhaul/spec.md`'s "Neon snapshot / restore" and recover-flow sections to drop "restore-point" / "restore-point-name" terminology. No code change.

### F21 — `_cleanup_orphan_secrets` matches by suffix; could delete unrelated secrets

- **Severity:** smell (low-likelihood collision)
- **Where:** `apps/minds/imbue/minds/envs/recover.py:315-321`.
- **Why it matters:** `recover` deletes every Modal Secret whose name *ends with* `-<tier>-<deploy_id>`. If anything else in the Modal env happens to have a name ending with the same suffix (a future plugin / a hand-pushed secret / a different service whose name conflicts), it'd get caught up in the recover. The deploy_id is a UTC timestamp so collisions are unlikely, but the substring match is a hash-collision-style trap.
- **Suggested fix:** Walk `deploy_config.secrets.services` (which is captured in the recover-target file's `tier` via `vault_path_prefix` → `load_deploy_config(target.tier).secrets.services`) and delete exactly those names. The current implementation doesn't have the services list on the recover-target, but adding it is a one-line schema bump.
- **Decision:** Add `services: tuple[str, ...]` field to `RecoverTarget`. Populate it in `deploy_env` from `deploy_config.secrets.services`. In `_cleanup_orphan_secrets`, iterate `target.services` and call `providers.delete_modal_secret(timestamped_secret_name(svc, target.tier, target.deploy_id), ...)` for each. Existing recover-target files become incompatible (no `services` field); add a clear `RecoverTargetCorruptError` message when the field is missing telling the operator to re-deploy.

### F22 — Destroy refuses if env root has been manually removed, leaving cloud resources stranded

- **Severity:** smell (operator footgun)
- **Where:** `apps/minds/imbue/minds/envs/provisioning.py:863-864` raises `DevEnvNotFoundError` if `env_root_exists(name)` is false.
- **Why it matters:** Operator workflow: "destroy failed at step 5, I'll manually clean up Neon by hand, then `rm -rf ~/.minds-<name>/` because it looks stale". Now they want to re-run destroy to pick up Cloudflare/Modal cleanup. But destroy refuses because env root is gone. The operator now has stranded OVH VPSes, stranded Cloudflare tunnels, and stranded Modal apps — and no `minds env destroy` to clean them up.
- **Suggested fix:** Allow destroy to proceed when the env root is missing, with a confirmation flag (e.g. `--force-without-env-root`). Or relax the check: if env root is missing but the operator passed a valid `DevEnvName`, proceed (since the env's cloud-side tag is keyed off the name, not the local root).
- **Decision:** Add `--force-without-env-root` flag on `minds env destroy`. Without it, keep the current refuse-on-missing behavior (protects against typos). With it, skip the `env_root_exists` check, log a warning, and proceed straight to step 1 (mngr agents will be no-op since no agents dir exists), then run all cloud-side cleanup. Step 8 (`delete_env_root`) becomes a no-op since the root is already gone. Also add the flag to destroy's `--help` with an explicit "use only after manually nuking ~/.minds-<name>" disclaimer.

### F23 — Destroy's Step 1 (`mngr destroy`) ordering means tunnels are torn down by mngr-side cleanup AND by Step 3 redundantly

- **Severity:** smell (efficiency; not a correctness bug)
- **Where:** `apps/minds/imbue/minds/envs/provisioning.py:880-887` (Step 1) + `:899-910` (Step 3).
- **Observation:** The mngr agents may themselves own Cloudflare tunnels (created at agent create time by the desktop client per `apps/minds/docs/design.md` section "Cloudflare tunnel integration"). Step 1 runs `mngr destroy <agent>` which should release those agent-owned tunnels; Step 3 then sweeps everything tagged with `metadata.env=<name>`. So per-agent tunnels are deleted at Step 1 (via mngr's own destroy hooks, if any), then re-checked at Step 3.
- **Why it might matter:** If `mngr destroy` doesn't actually clean up Cloudflare tunnels (it doesn't today AFAICT; the desktop client owns the tunnel lifecycle, not mngr), then Step 1 is destroying the agent's container but leaving its tunnel. Step 3 sweeps. So this works — but only because Step 3 is a backstop. If the tag key ever changes (or if a tunnel is created without the env tag), Step 1 won't fix it. Not a today-bug; tag-drift risk.
- **Suggested fix:** Add a release-test asserting `metadata.env=<name>` is on every tunnel created via the connector flow.
- **Decision:** Defer — no code change today. The current behavior is correct (Step 3 is a backstop and works), and the spec says destroy is best-effort. Capture as a release-time invariant test when the test suite lands.

### F24 — `per_env_*_url` helpers hardcode `dev` as the tier, breaking any future PER_ENV non-dev tier

- **Severity:** smell (latent footgun; not exercised by today's configs)
- **Where:**
  - `apps/minds/imbue/minds/envs/per_env_deploy.py:154` — `per_env_connector_url(name, modal_workspace)` returns `https://{workspace}-{name}--rsc-dev-api.modal.run`.
  - `apps/minds/imbue/minds/envs/per_env_deploy.py:163` — same for `per_env_litellm_proxy_url` → `llm-dev-proxy`.
- **Why:** The signature doesn't take `tier` because — today — the only tier with `modal_env_strategy=PER_ENV` is dev. So the hardcode is "fine in practice." But the spec presents `modal_env_strategy` as orthogonal to `tier`. The day someone configures a new tier (say a `staging-dev` for per-dev pre-production iteration) with `PER_ENV` strategy, the URL builder returns `rsc-dev-api.modal.run` URLs that won't match the actual deployed app (`rsc-staging-dev-api.modal.run`) — and `_assert_deploy_url_matches` will catch it AT DEPLOY TIME (good), but only after Modal has already deployed the app. The recover-target file is on disk, so the operator can roll back, but it's a confusing failure.
- **Suggested fix:** Thread `tier` through to `per_env_*_url`. The naming convention becomes `rsc-{tier}-api`. Today's dev URLs need to match — confirm the deployed Modal app is currently named `rsc-dev` (per our probe of dev-josh-1: yes, the URL is `--rsc-dev-api.modal.run`). After threading `tier`, dev-tier URLs stay `rsc-dev`, future tiers get their own.
- **Decision:** Add `tier: str` parameter to `per_env_connector_url` and `per_env_litellm_proxy_url`. Update `_expected_*_url` call sites to pass `tier=tier`. Verify dev URLs stay `rsc-dev` / `llm-dev` so dev-josh-1 (and other dev envs) keep working without a redeploy.

### F25 — `_DERIVED_ONLY_SECRET_SERVICES = {"litellm-connector"}` silently shadows any vault entry under that name

- **Severity:** smell (operator footgun)
- **Where:** `apps/minds/imbue/minds/envs/per_env_deploy.py:76`.
- **Why:** If an operator adds `secrets/minds/<tier>/litellm-connector` to Vault (e.g. to override a specific value), the deploy quietly skips the Vault read and uses only the in-code overrides. The operator's Vault entry is never noticed, and no warning fires (in fact the comment at line 71-75 says the *point* is to suppress the warning). Anyone manually populating `litellm-connector` will silently see their settings ignored.
- **Suggested fix:** Either (a) document this prominently in the deploy.toml `[secrets].services` block + ensure release notes mention it, or (b) read the Vault entry if it exists (allowing overrides to take precedence over the operator's Vault entry, which is the normal `merged = dict(base); merged.update(overrides)` flow). The current behavior is exactly the inverse of every other service in the list.
- **Decision:** Option (b) — drop `_DERIVED_ONLY_SECRET_SERVICES` and let `build_per_env_secret_values` read every service's Vault entry normally. If the entry is missing, the existing fallback (placeholder with a warning) still fires. Operator overrides still win since `merged.update(overrides)` runs last. Removes a special case. Update the deploy.toml comment around `[secrets].services` to note that `litellm-connector` is normally empty in Vault but supports per-key overrides.

### F26 — `write_recover_target_atomic`'s "defends against a race" comment is overly confident — there's still a TOCTOU window

- **Severity:** nit (low likelihood)
- **Where:** `apps/minds/imbue/minds/envs/recover.py:158-186`.
- **Why:** The function does `if final_path.exists(): raise` then `os.replace(tmp_path, final_path)`. Two parallel deploys both pass the `.exists()` check, both write their tmp file, both call `os.replace` — the second silently overwrites the first. The atomicity is per-operation (the file always either is or isn't there) but the **exclusivity** is not enforced.
- **Why it matters:** Two simultaneous deploys against the same env from two different shells would each succeed in writing a recover-target file (one overwrites the other), each push their own Modal Secrets, each `modal deploy`. The "loser" deploy's recover-target is gone, so its mutations are unreversible. Unlikely (operators don't usually parallelize deploys against the same env) but possible.
- **Suggested fix:** Use `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` for the rename target. Or take a flock on a sentinel path before the check.
- **Decision:** Replace `os.replace(tmp_path, final_path)` with an `os.open(final_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` write + fsync, eliminating the TOCTOU. Loses the tempfile-write-then-rename atomicity slightly (a crash mid-write leaves a partial file), so combine with an explicit `RecoverTargetCorruptError` (the F13 fix already handles this case). Soften the now-overconfident comment to reflect the real guarantee.

### F27 — Inconsistent error wrapping: sync def endpoints use `handle_endpoint_errors`, async def endpoints don't

- **Severity:** smell (UX — async endpoint exceptions surface as bare 500s)
- **Where:** `apps/remote_service_connector/imbue/remote_service_connector/app.py:1459-1467` defines `handle_endpoint_errors`. Sync def endpoints (`/tunnels`, `/hosts/*`, `/keys/*`, ...) wrap their body in `with handle_endpoint_errors():`. Async def endpoints (`/auth/signup`, `/auth/signin`, `/auth/session/refresh`, `/auth/session/revoke`, `/auth/email/send-verification`, `/auth/email/is-verified`, `/auth/password/forgot`, `/auth/password/reset`, `/auth/oauth/authorize`, `/auth/oauth/callback`) do not.
- **Why:** Any uncaught exception in an async endpoint surfaces as the default FastAPI 500 `Internal Server Error` (no detail). The sync endpoints get a translation pass via `raise_as_http`. F5's RuntimeError is the most visible symptom of this — but even with F5 fixed, any future bug in an async endpoint will produce an unhelpful error.
- **Suggested fix:** Mirror the wrapping for async endpoints. Either an `async with handle_endpoint_errors_async():` (a similarly-shaped async context manager) or a FastAPI exception handler registered on the app.
- **Decision:** **Largely subsumed by F5.** Once the connector's 12 `async def` endpoints become `def`, every endpoint in the file gets a uniform `with handle_endpoint_errors():` wrapper as part of the F5 conversion (the existing sync endpoints already have it). No separate FastAPI `exception_handler` needed for the connector. Still add a defensive `@web_app.exception_handler(Exception)` registration that routes through `raise_as_http` as a last-resort net for anything that escapes — cheap, covers future regressions if the no-async rule slips again. The desktop_client's async endpoints (out of scope here) keep being a future concern.

### F28 — FCT `vendor/mngr` and the minds-side `libs/mngr` are out of sync

- **Severity:** smell (release/workflow — workspaces created today won't have unreleased mngr fixes)
- **Where:**
  - FCT `/home/rtard/project/forever-claude-template/vendor/mngr` is on commit `bf4d75a97` (`josh/env_testing` branch).
  - This worktree's `libs/mngr` is on commit `1cb84016c` (mngr/env-testing branch) — which has the `ProviderEmptyError` fix from earlier in this session.
  - `git -C vendor/mngr remote` points at `github.com/imbue-ai/mngr.git` — a separate repo (mngr is its own GitHub repo too).
- **Why it matters:** Workspaces that minds creates today (via FCT) get the older `vendor/mngr`. Any mngr behavior the desktop client expects to be present (e.g. the ProviderEmptyError-based silent skip of Modal in `mngr list` if a workspace agent ever invokes it against a non-existent env) won't be there. Whether this is exercised in practice depends on whether the workspace agent runs `mngr list` against Modal — likely not, but worth confirming.
- **Suggested action:** Use the `release-minds` skill to sync `vendor/mngr` to `josh/env_testing` (or wherever the right minds branch is). Add a CI check that asserts FCT `vendor/mngr`'s commit ≥ a documented floor (e.g. the latest tagged minds release).
- **Decision:** Defer the sync — this is a workflow operation (release-minds skill) that you'll want to run intentionally. **Not a code change.** Action item: confirm with you that the current `josh/env_testing` minds branch is the right floor and then I'll invoke `release-minds`.

### F29 — Hardcoded "Please ask Josh to provision more" in a user-facing 503 error message

- **Severity:** nit (UX for non-Josh users)
- **Where:** `apps/remote_service_connector/imbue/remote_service_connector/app.py:1690-1695`.
- **Observed:** Code:

  ```python
  raise HTTPException(
      status_code=503,
      detail=(
          "No pre-created agents match the requested attributes. "
          "Please ask Josh to provision more, or relax the attribute filter."
      ),
  )
  ```

- **Why:** A specific developer's name is baked into the 503. Anyone else who runs `minds env deploy` and exhausts the pool sees a message telling them to "Ask Josh." Even for Josh's team this becomes wrong as soon as someone else takes over pool operations.
- **Suggested fix:** Replace with a tier-agnostic phrasing ("The host pool is empty; an operator needs to bake more hosts via `mngr imbue_cloud admin pool bake`.") or drive it from a `deploy.toml` field (e.g. `operator_contact = "..."`) so each tier can configure who to message.
- **Decision:** Hardcoded tier-agnostic replacement: `"No pre-created agents match the requested attributes. Ask your minds operator to bake more hosts (`mngr imbue_cloud admin pool bake`), or relax the attribute filter."` — no `deploy.toml` field for now (avoids adding a new config knob for a 503 message). One-line string change.

### F30 — `release_host` is not idempotent (second release of the same host returns 404)

- **Severity:** nit (same shape as F7)
- **Where:** `apps/remote_service_connector/imbue/remote_service_connector/app.py:1753-1766`.
- **Why:** First call sets status=`released`. Second call's `SELECT ... WHERE status='leased'` returns nothing → 404. A client retrying release after a transient network error sees 404 even though their lease is already released.
- **Suggested fix:** SELECT without the `status='leased'` filter; check status separately, treat already-released as 200.
- **Decision:** Same shape as F7. SELECT without the `status='leased'` filter; if row exists and `leased_to_user != admin.username` → 403, if status='released' → 200 `{"status":"already_released"}`, if status='leased' → proceed with update. Bundle with F7 into one PR.

### F31 — `lease_host`'s SSH-key injection failure leaves a partial state on the VPS

- **Severity:** smell (low blast radius)
- **Where:** `apps/remote_service_connector/imbue/remote_service_connector/app.py:1701-1714`.
- **Observed:** Calls `_append_authorized_key` twice in sequence (VPS, then container). If the VPS append succeeds but the container append fails, the VPS now has the user's public key — but the user gets a 502 and the DB row is rolled back to `available`. The next user who leases this host will have their key appended in addition to the orphan, growing `authorized_keys` indefinitely (since the container append might also have orphans).
- **Why it matters:** Over many leased-then-failed-injection cycles, `authorized_keys` accumulates dead entries. Not a security hole (each user's key is theirs), but a hygiene drift.
- **Suggested fix:** Either (a) make `_append_authorized_key` idempotent with a marker comment per key so we can prune on lease, or (b) replace `authorized_keys` wholesale on lease (the VPS is single-user anyway).
- **Decision:** Defer. Low blast radius (each user's own key) and the cleanest fix (option b — replace `authorized_keys` wholesale) needs care so we don't lock out the pool-management key itself. Document the orphan-growth behavior in `_append_authorized_key`'s docstring + add a TODO. Revisit when pool-host hygiene matters more.








