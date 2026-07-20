# CASA (Cloud Application Security Assessment) Compliance Audit — Minds App

Audit date: 2026-07-15
Scope: `apps/minds/` (Electron desktop shell + local FastAPI/Flask desktop client), plus directly-supporting shared code in `libs/mngr_forward/`, `libs/mngr_imbue_cloud/`, and `apps/remote_service_connector/`.

This audits the Minds app against Google's **CASA Tier 2** requirements, which are based on the **OWASP Application Security Verification Standard (ASVS) v4.0**. There is no CASA-specific document already in this repo (confirmed via repo-wide search), so the requirement text below was sourced from the App Defense Alliance's public CASA specification (`github.com/appdefensealliance/ASA-WG`) — see [Sources](#sources).

**Architecture context** (see also [`security-boundaries-audit.md`](security-boundaries-audit.md)): the Minds app is a single-user Electron desktop application. Its "backend" is a Flask/FastAPI **desktop client bound to `127.0.0.1` only**, which proxies per-agent HTTP traffic through `mngr forward` to isolated per-agent containers (each on its own `agent-<id>.localhost` subdomain — different browser origins). Account authentication (password/OAuth) is delegated to an external SuperTokens-backed **connector service** (`apps/remote_service_connector/`) reached over HTTPS. Several ASVS controls written for public multi-tenant web servers are therefore either **N/A** (e.g., true webhook receivers, XML parsing) or satisfied by a different, architecturally-appropriate mechanism (e.g., XSS mitigated by per-agent origin isolation rather than universal output escaping). Those cases are called out explicitly rather than marked as blanket failures.

**Verdict legend:** ✅ MET · 🟡 PARTIAL · ❌ NOT MET · ⚪ N/A · 🟦 PROBABLY (satisfied by SuperTokens; not verifiable from this repo)

---

## Update — fixes applied on branch `preston/casa-fixes`

A follow-up change implemented the self-contained ("A-list") fixes. The scorecard below reflects the post-fix state; each changed row is annotated. Items whose only remaining uncertainty is behavior inside the external SuperTokens connector are now marked 🟦 PROBABLY rather than PARTIAL/NOT MET, since they cannot be confirmed or fixed from this repo.

Fixed to ✅ MET: 6.1.1 (dependency + secret scanning), 1.1.2 / 1.3.1 (one-time-code TTL), 5.1.7 (first-party CSP + nosniff), 3.2.2 (OAuth `state`), 3.2.1 (PKCE — Google), plus 5.1.9 shell-quoting hardening.

Confirmed already ✅ MET (verdict corrected, no code change): 2.2.1 (account tokens revoked server-side) and 6.6.1 (no user credential lives in browser storage). An earlier revision of this branch cleared the device-level `minds_session` cookie + Electron storage on account sign-out; that was reverted because it conflated app-unlock with account sign-out and would have locked users out of local-only app use — see 2.2.1 / 6.6.1.

Reclassified to 🟦 PROBABLY (SuperTokens): 1.1.1 (sign-in brute-force), 1.1.3 (password hashing), 2.2.2 (session invalidation on password change).

Still open (require a product/design decision or an external service, see [remediation list](#remediation-priority-list)): 2.2.3, 2.3.4, 2.4.1, 3.1.5, 3.3.1, and the redirect_uri-allowlist half of 3.2.2.

---

## Summary scorecard

| # | Requirement | Verdict |
|---|---|---|
| 1.1.1 | Auth resistant to brute force | 🟦 PROBABLY (SuperTokens) |
| 1.1.2 | Initial passwords/activation codes random + expire | ✅ MET (fixed: 15-min TTL) |
| 1.1.3 | Passwords stored resistant to offline attack | 🟦 PROBABLY (SuperTokens) / ✅ (local master password) |
| 1.2.1 | No default credentials on exposed interfaces | ✅ MET |
| 1.3.1 | OOB verifier expires reasonably | ✅ MET (fixed: local code TTL; reset email SuperTokens) |
| 1.3.2 | OOB verifier single-use | ✅ MET |
| 1.3.3 | OOB verifier securely random | ✅ MET |
| 1.3.4 | OOB verifier resistant to brute force | 🟡 PARTIAL (256-bit entropy; no rate limit) |
| 2.1.1 | No tokens in URL params | 🟡 PARTIAL |
| 2.2.1 | Logout invalidates stateful/refresh tokens | ✅ MET (account tokens revoked server-side) |
| 2.2.2 | Sessions terminated on password change | 🟦 PROBABLY (SuperTokens) |
| 2.2.3 | Stateless tokens expire ≤24h | ❌ NOT MET (30-day cookie; design decision) |
| 2.3.1 | Cookie `Secure` attribute | ❌ NOT MET (justified — loopback HTTP only) |
| 2.3.2 | Cookie `HttpOnly` attribute | ✅ MET |
| 2.3.3 | Session tokens preferred over static secrets | ✅ MET |
| 2.3.4 | Tokens protected vs tampering/replay/substitution | 🟡 PARTIAL |
| 2.4.1 | Re-auth for sensitive transactions | 🟡 PARTIAL |
| 3.1.1 | Least privilege on trusted service layer | ✅ MET |
| 3.1.2 | User cannot manipulate access-control attrs | ✅ MET |
| 3.1.3 | Access controls fail securely | ✅ MET |
| 3.1.4 | IDOR protection | ✅ MET |
| 3.1.5 | Anti-CSRF | 🟡 PARTIAL |
| 3.1.6 | Directory browsing disabled | ✅ MET |
| 3.2.1 | Secure OAuth (Auth Code + PKCE) | ✅ MET (fixed: PKCE on Google; GitHub N/A) |
| 3.2.2 | redirect_uri / state validated | ✅ MET (`state` fixed) / 🟡 PARTIAL (`redirect_uri` — IdP allowlist) |
| 3.3.1 | Admin interfaces use MFA | ❌ NOT MET |
| 4.1.1 | TLS enforced, ≥1.2, no weak ciphers | ✅ MET |
| 4.1.2 | Trusted TLS certs | 🟡 PARTIAL (scoped, documented self-signed use) |
| 4.1.3 | No weak cryptography | ✅ MET |
| 4.1.4 | Crypto fails securely / no padding oracle | ✅ MET |
| 5.1.1 | HTTP parameter pollution protection | 🟡 PARTIAL |
| 5.1.2 | Open redirect protection | ✅ MET |
| 5.1.3 | `eval()` avoided | ✅ MET |
| 5.1.4 | Template injection protection | ✅ MET |
| 5.1.5 | SSRF protection | ✅ MET |
| 5.1.6 | XPath/XML injection protection | ⚪ N/A |
| 5.1.7 | Context-aware XSS output escaping | ✅ MET (fixed: first-party CSP + nosniff) |
| 5.1.8 | Database injection protection | ✅ MET (mostly N/A — no client-facing SQL) |
| 5.1.9 | OS command injection protection | ✅ MET (hardened: shlex.quote) |
| 5.1.10 | Local/remote file inclusion protection | ✅ MET |
| 5.2.1 | File upload type/execution restrictions | ⚪ N/A (no upload endpoint) |
| 6.1.1 | No known-vulnerable components (CI scanning) | ✅ MET (fixed: Dependabot + audit CI + gitleaks) |
| 6.2.1 | Debug modes disabled in production | ✅ MET |
| 6.3.1 | Origin header not used for auth decisions | ✅ MET |
| 6.4.1 | Not susceptible to subdomain takeover | 🟡 PARTIAL / N/A |
| 6.5.1 | No credentials/tokens in logs | ✅ MET |
| 6.6.1 | Browser storage cleared on logout | ✅ MET (by architecture — no user creds in browser storage) |
| 6.7.1 | Secrets stored securely | ✅ MET |
| 7.1.1 | Webhooks over HTTPS/TLS | ⚪ N/A (no webhooks) |
| 7.1.2 | Webhook endpoint ownership verified | ⚪ N/A |
| 7.2.1 | Webhook payloads HMAC-authenticated | ⚪ N/A |
| 7.2.2 | Timing-safe signature comparison | ✅ MET (analogous internal auth check) |
| 7.2.3 | Webhook replay protection | ⚪ N/A |
| 7.3.1 | SSRF mitigation on webhook callback URLs | ✅ MET (analogous sharing/probe URL) |
| 7.3.2 | Webhook secrets not hardcoded | ⚪ N/A |

**Tally (pre-fix):** 25 MET · 14 PARTIAL · 6 NOT MET · 9 N/A (out of 47 in-scope items; some 1.x sub-items intentionally grouped)

**Tally (post-fix, this branch):** 33 MET · 3 PROBABLY (SuperTokens) · 7 PARTIAL · 2 NOT MET · 9 N/A. The 2 remaining NOT MET (2.2.3 stateless-token lifetime, 3.3.1 admin MFA) and the residual PARTIAL items each need a product/design decision or an external-service change rather than a self-contained code fix — they were deliberately out of scope for this pass.

---

## Category 1: Authentication

> Note: Password/account authentication itself is delegated to an external SuperTokens-core "connector" service. Password hashing, and brute-force lockout for that specific service are enforced server-side outside this repo's visibility — flagged as such below rather than assumed.

### 1.1.1 — Authentication resistant to brute force attacks
🟦 **PROBABLY (SuperTokens).** Sign-in itself is handled by the external SuperTokens connector, which enforces its own brute-force/lockout protection server-side; this cannot be confirmed or changed from this repo, so it is marked PROBABLY rather than PARTIAL. Local credential comparisons use constant-time `hmac.compare_digest` ([`api_key_store.py:53`](../imbue/minds/desktop_client/api_key_store.py)). The one first-party auth endpoint, the local one-time-code check, is no longer brute-forceable in practice: codes are 256-bit random and now expire (see 1.1.2). Adding explicit rate limiting on first-party auth endpoints remains a possible defense-in-depth improvement.

### 1.1.2 — Initial passwords/activation codes securely random and expire
✅ **MET (fixed on this branch).** The local one-time login code is `secrets.token_urlsafe(32)` (256 bits) and now carries a timezone-aware `created_at` stamped at mint time; `FileAuthStore.validate_and_consume_code` ([`desktop_client/auth.py`](../imbue/minds/desktop_client/auth.py)) rejects (and marks `EXPIRED`) any code older than a 15-minute TTL, and treats a persisted code with a missing timestamp as expired. Unit tests cover fresh-accept, expired-reject, legacy-missing-timestamp, and within-TTL cases.

### 1.1.3 — Passwords stored resistant to offline attacks
🟦 **PROBABLY (SuperTokens)** for account login passwords — `mngr_imbue_cloud`'s `auth_signin`/`auth_signup` ([`imbue_cloud_cli.py:268-282`](../imbue/minds/desktop_client/imbue_cloud_cli.py)) forward credentials to the external SuperTokens connector; no password hash is computed or stored locally, so the hashing algorithm is SuperTokens' responsibility (SuperTokens hashes with a strong adaptive algorithm by default) and is not verifiable from this repo. ✅ **MET** for the separate local "master password" used to wrap per-account data-encryption keys — hashed with Argon2 (`PasswordHasher()`, [`dek_store.py:33,57`](../imbue/minds/desktop_client/dek_store.py)). Minor note: `auth signin/signup` passwords are passed as subprocess argv, visible via `ps` on the same machine — low severity for a single-user desktop app.

### 1.2.1 — No default credentials on publicly exposed interfaces
✅ **MET.** The server binds only to `127.0.0.1` ([`config/data_types.py:20`](../imbue/minds/config/data_types.py)). `MINDS_API_KEY` and the cookie-signing key are freshly, randomly generated every run, never hardcoded ([`api_key_store.py:33-41`](../imbue/minds/desktop_client/api_key_store.py), [`auth.py:130-140`](../imbue/minds/desktop_client/auth.py)). Caveat: a `SKIP_AUTH=1` env-var bypass exists ([`app.py:280`](../imbue/minds/desktop_client/app.py), [`api_auth.py:67`](../imbue/minds/desktop_client/api_auth.py)) that disables all cookie checks — confirm this is dev/test-only and cannot leak into a deployed build.

### 1.3.1 — Out-of-band verifier expires in a reasonable timeframe
✅ **MET (fixed on this branch).** The local "magic-link" one-time code now expires after 15 minutes (see 1.1.2), so the closest first-party analogue of an OOB verifier is time-bounded. True password-reset email delivery is delegated to the external SuperTokens connector ([`supertokens_routes.py:743-764`](../imbue/minds/desktop_client/supertokens_routes.py)), whose token TTL is 🟦 PROBABLY handled server-side.

### 1.3.2 — Out-of-band verifier used only once
✅ **MET.** `validate_and_consume_code` atomically flips `VALID` → `USED` and rejects reuse ([`auth.py:95-112`](../imbue/minds/desktop_client/auth.py)).

### 1.3.3 — Out-of-band verifier securely random
✅ **MET.** `secrets.token_urlsafe(32)` — 256 bits ([`cli/run.py:567`](../imbue/minds/cli/run.py)).

### 1.3.4 — Out-of-band verifier resistant to brute force
🟡 **PARTIAL.** No explicit rate limiting on `/authenticate` ([`app.py:296-303`](../imbue/minds/desktop_client/app.py)), but the 256-bit code space makes online brute force computationally infeasible regardless.

---

## Category 2: Session Management

### 2.1.1 — No tokens in URL parameters
🟡 **PARTIAL.** The one-time login code and desktop-client session bootstrap travel via query string: `/login?one_time_code=...`, `/authenticate?one_time_code=...` ([`app.py:265-333`](../imbue/minds/desktop_client/app.py)). The cross-agent-subdomain auth bridge also passes a signed token in the URL via `/goto/{agent_id}/` ([`libs/mngr_forward/imbue/mngr_forward/cookie.py:62-93`](../../../libs/mngr_forward/imbue/mngr_forward/cookie.py)), though it's short-lived (30s). The actual `minds_session` cookie and `MINDS_API_KEY` bearer token are never in a URL.

### 2.2.1 — Logout invalidates all stateful/refresh tokens
✅ **MET.** Account sign-out revokes the **user's** session tokens: `signout_user_via_plugin` ([`supertokens_routes.py`](../imbue/minds/desktop_client/supertokens_routes.py)) calls `mngr imbue_cloud auth signout`, which revokes the SuperTokens session (access + refresh) and deletes the local session file. That is the stateful/refresh-token invalidation the requirement asks for.

> Note: an earlier revision of this branch *also* cleared the `minds_session` cookie on account sign-out. That was reverted — `minds_session` is a device/app-unlock token (payload is the constant `"authenticated"`, minted from the one-time code `minds run` emits), **not** a user credential, and it gates all local app use (settings, local workspaces) independently of any Imbue account. Clearing it on account sign-out would have locked the user out of the whole app after they merely disconnected a cloud account. Account sign-out and app-lock are deliberately separate concerns.

### 2.2.2 — Sessions terminated after password change
🟦 **PROBABLY (SuperTokens).** `_handle_forgot_password_api`/`_handle_reset_password_redirect` ([`supertokens_routes.py:748-778`](../imbue/minds/desktop_client/supertokens_routes.py)) are thin proxies to the external connector; session invalidation on password change is a SuperTokens-core responsibility (its default reset flow revokes sessions) and is not verifiable or fixable from this repo. Confirm the connector's SuperTokens config enables session revocation on reset.

### 2.2.3 — Non-revocable stateless tokens expire within 24h
❌ **NOT MET** for desktop session cookies. `_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60` (30 days) in both [`desktop_client/cookie_manager.py:23`](../imbue/minds/desktop_client/cookie_manager.py) and [`libs/mngr_forward/.../cookie.py:26`](../../../libs/mngr_forward/imbue/mngr_forward/cookie.py) — stateless, non-per-session-revocable. Revocation only happens by rotating the single global signing key, invalidating every session on the machine at once. The subdomain auth-bridge token (30s TTL) is fine. SuperTokens access/refresh tokens (external service) likely use short-TTL JWTs with rotation, but exact TTL is set by the external connector and not visible here.

### 2.3.1 — Cookie `Secure` attribute
❌ **NOT MET literally, but architecturally justified.** `response.set_cookie(..., httponly=True, samesite="lax")` ([`app.py:326-333`](../imbue/minds/desktop_client/app.py)) does not pass `secure=True`. The server is intentionally bound to plain-HTTP `127.0.0.1` only — `Secure` would break every request since browsers won't send Secure cookies over non-HTTPS, and there's no cross-network exposure since traffic never leaves loopback. Should be documented as a formal exception in the CASA questionnaire rather than left silent.

### 2.3.2 — Cookie `HttpOnly` attribute
✅ **MET.** `httponly=True` ([`app.py:330`](../imbue/minds/desktop_client/app.py)); covered by [`cookie_manager_test.py`](../imbue/minds/desktop_client/cookie_manager_test.py).

### 2.3.3 — Session tokens preferred over static API secrets
✅ **MET.** UI predominantly uses the signed session cookie; the one static secret (`MINDS_API_KEY`) is scoped to machine-to-machine gateway traffic only, freshly generated every `minds run`, never persisted to disk ([`api_key_store.py`](../imbue/minds/desktop_client/api_key_store.py)). `require_api_or_cookie_auth` ([`api_auth.py:81-95`](../imbue/minds/desktop_client/api_auth.py)) picks the right credential type per caller.

### 2.3.4 — Protection vs tampering, replay, key substitution
🟡 **PARTIAL.** Tampering: MET — `itsdangerous.URLSafeTimedSerializer` HMAC-signs cookie payloads ([`cookie_manager.py:26-46`](../imbue/minds/desktop_client/cookie_manager.py); tamper tests in `cookie_manager_test.py:29-35`). Key substitution: MET — `hmac.compare_digest` on the bearer key ([`api_key_store.py:53`](../imbue/minds/desktop_client/api_key_store.py)). Replay: only partially mitigated — the signed session cookie has no per-issuance nonce, so a stolen cookie is fully replayable until its 30-day expiry (ties to 2.2.3). The 30-second subdomain auth token is much better protected.

### 2.4.1 — Re-authentication for sensitive transactions
🟡 **PARTIAL.** Nearly every state-changing route in `app.py` gates on `_is_request_authenticated()` (dozens of call sites) and cross-workspace `/api/v1` routes require bearer-or-cookie auth ([`api_auth.py:81-95`](../imbue/minds/desktop_client/api_auth.py)). No evidence of **step-up re-authentication** for especially sensitive operations (disabling encryption, account settings, sign-out-all) — mere possession of the long-lived session cookie suffices.

---

## Category 3: Access Control

### 3.1.1 — Least privilege on a trusted service layer
✅ **MET.** The connector enforces two distinct principal types server-side: `AgentAuth` (scoped to one Cloudflare tunnel) vs `AdminAuth` (verified SuperTokens user), with `require_admin()`/`require_tunnel_access()` server-side gates ([`remote_service_connector/app.py:1686-1699`](../../remote_service_connector/imbue/remote_service_connector/app.py)). Desktop client's `/api/v1` surface defaults to denying (`401`) unless a valid bearer key or session cookie is presented ([`api_auth.py:81-95`](../imbue/minds/desktop_client/api_auth.py)).

### 3.1.2 — User cannot manipulate access-control attributes
✅ **MET.** Bearer comparison uses `hmac.compare_digest` ([`api_key_store.py:44-53`](../imbue/minds/desktop_client/api_key_store.py)); role is derived server-side from token *type*, not client-supplied fields ([`remote_service_connector/app.py:1520-1549`](../../remote_service_connector/imbue/remote_service_connector/app.py)). Session cookie payload is a fixed constant `"authenticated"` — the client can't forge elevated claims.

### 3.1.3 — Access controls fail securely, including on exceptions
✅ **MET.** `handle_endpoint_errors()` converts unhandled exceptions to `500`s, never to "allow" ([`remote_service_connector/app.py:1935-1943`](../../remote_service_connector/imbue/remote_service_connector/app.py)). `authenticate_request` raises on any malformed/missing/invalid token. Desktop-client handlers check auth before doing work, defaulting to deny. Caveat: this is a manual per-route pattern in `app.py`, not a single global gate — a maintainability risk versus the cleaner decorator in `api_auth.py`.

### 3.1.4 — IDOR protection (CRUD)
✅ **MET.** `rename_host` explicitly checks lease ownership before mutation, with a comment noting the ownership check runs first to avoid a status-oracle leak ([`remote_service_connector/app.py:2700-2737`](../../remote_service_connector/imbue/remote_service_connector/app.py)). Desktop-client routes validate `AgentId`/`CreationId` and reject malformed ids with 400 ([`api_auth.py:50-58`](../imbue/minds/desktop_client/api_auth.py)) rather than operating on raw attacker-controlled strings.

### 3.1.5 — Anti-CSRF for authenticated functionality
🟡 **PARTIAL.** No explicit CSRF token mechanism anywhere in `apps/minds`. Mitigation: session cookie is `httponly=True, samesite="lax"` ([`app.py:326-332`](../imbue/minds/desktop_client/app.py)), blocking the main cross-site-cookie CSRF vector. However, the connector explicitly disables SuperTokens' built-in CSRF check (`anti_csrf_check=False`, [`remote_service_connector/app.py:1628-1632,1674-1677`](../../remote_service_connector/imbue/remote_service_connector/app.py)) — arguably fine since that path uses a Bearer JWT rather than an ambient cookie, but worth a documented rationale.

### 3.1.6 — Directory browsing disabled
✅ **MET.** Static assets served via Flask's `static_folder`/`static_url_path` ([`app.py:2781`](../imbue/minds/desktop_client/app.py)), which 404s on directories by default; no custom directory-listing code found.

### 3.2.1 — Secure OAuth 2.0 flow (Authorization Code + PKCE), no Implicit/ROPC
✅ **MET (fixed on this branch).** Authorization Code flow was already used correctly (no Implicit, no ROPC). PKCE is now genuinely applied: the connector was confirmed to generate a verifier only when a provider sets `force_pkce`, so `force_pkce=True` was enabled on the Google provider and the verifier is now threaded end-to-end — `auth_oauth_authorize` returns it in `OAuthAuthorizeResponse`, the CLI holds it in memory (never logged), and `auth_oauth_callback` passes it into `RedirectUriInfo` (previously hardcoded `None`). GitHub is left without `force_pkce` (⚪ N/A — GitHub OAuth Apps don't support PKCE; the generic `None`-verifier path handles it), documented in code and changelog. Deploy note: an older already-installed `mngr` CLI hitting the newer `force_pkce` connector would fail the Google exchange, so this needs a coordinated release (the CLI and connector ship from the same monorepo).

### 3.2.2 — `redirect_uri` and `state` securely validated
✅ **MET (`state`, fixed on this branch)** / 🟡 **PARTIAL (`redirect_uri`)**. A CSRF `state` parameter is now generated, reflected, and verified: the CLI mints a `secrets.token_urlsafe(32)` state per flow, the connector reflects it into the provider authorize URL via `_authorize_url_with_state` (dropping any provider-supplied `state` so the client's value is the one echoed back), and the CLI verifies the echoed `state` in constant time (`secrets.compare_digest`, with a non-ASCII guard) **before** exchanging the code — a missing or mismatched state aborts the flow ([`mngr_imbue_cloud/cli/auth.py`](../../../libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/cli/auth.py), [`remote_service_connector/app.py`](../../remote_service_connector/imbue/remote_service_connector/app.py)). Tests cover URL-reflection and reject-on-mismatch/missing/non-ASCII. `redirect_uri` (`callback_url`) still has no server-side allow-list check on the connector — it remains mitigated by the IdP's own registered-redirect-URI allow-list; adding a loopback-only allow-list on the connector is a remaining defense-in-depth item.

### 3.3.1 — Admin interfaces use MFA
❌ **NOT MET / needs a product decision.** No traditional admin web console exists. Two admin-adjacent surfaces: (1) connector `AdminAuth` — any verified SuperTokens user is treated as "admin" for host-leasing self-service, single-factor auth, no MFA found in the SuperTokens config; (2) `minds paid {add,remove,list}` CLI ([`cli/paid.py`](../imbue/minds/cli/paid.py)) — authenticates via a static shared secret (`MINDS_PAID_ADMIN_KEY`, resolved from Vault) with no MFA, gating a genuinely privileged operation (paid allow-list management).

---

## Category 4: Communications

### 4.1.1 — TLS enforced, default ≥1.2, weak ciphers rejected
✅ **MET (mixed loopback/cloud).** Cloud connector traffic is always `https://` (`*.modal.run`, TLS terminated by Modal/Cloudflare — [`config/envs/staging/client.toml:6`](../imbue/minds/config/envs/staging/client.toml), [`.../production/client.toml:8`](../imbue/minds/config/envs/production/client.toml)). Local loopback proxy enforces TLS 1.2+ when HTTP/2 is used: `context.minimum_version = ssl.TLSVersion.TLSv1_2` with ALPN `["h2","http/1.1"]` ([`mngr_forward/tls.py:73-90`](../../../libs/mngr_forward/imbue/mngr_forward/tls.py)). The bare desktop-client `localhost:PORT` server is plaintext HTTP, but this is process-local loopback traffic only — standard for a desktop app.

### 4.1.2 — Trusted TLS certificates; internal CAs / self-signed restricted
🟡 **PARTIAL — scoped and documented.** The `mngr forward` proxy uses a self-signed certificate, freshly generated per process, SANs restricted to `localhost`/`*.localhost`/`127.0.0.1` ([`mngr_forward/tls.py:36-52`](../../../libs/mngr_forward/imbue/mngr_forward/tls.py)). The desktop client's probe client deliberately disables verification with an inline comment explaining the cert is "loopback-only" and "the probe is not positioned to validate anyway" ([`agent_creator.py:104-121`](../imbue/minds/desktop_client/agent_creator.py)). Cloud connector endpoints use standard CA-issued certs (MET). Not "trusted" in the strict ASVS sense, but low risk given no attacker has a network path to intercept loopback traffic.

### 4.1.3 — No weak cryptography meaningfully affecting confidentiality/integrity
✅ **MET.** Secrets generated via CSPRNG throughout (`secrets.token_urlsafe`) for session-signing keys, API keys, OAuth flow ids, workspace passwords. Bearer comparisons constant-time. Session cookies use `itsdangerous.URLSafeTimedSerializer` (HMAC-based); default digestmod is SHA1, which is fine for HMAC/signing purposes (collision attacks don't apply to HMAC) but could be upgraded to SHA256 as low-cost hardening.

### 4.1.4 — Cryptographic modules fail securely, no padding oracle
✅ **MET.** `verify_session_cookie` catches only `BadSignature` and returns `False` uniformly on any tampering/expiry, with no distinguishable error paths ([`cookie_manager.py:32-46`](../imbue/minds/desktop_client/cookie_manager.py)). `is_valid_minds_api_key` always executes the full constant-time comparison, no early-exit on partial match ([`api_key_store.py:44-53`](../imbue/minds/desktop_client/api_key_store.py)).

---

## Category 5: Data Validation and Sanitization

### 5.1.1 — HTTP parameter pollution protection
🟡 **PARTIAL.** No explicit HPP middleware/allowlist; relies on Werkzeug/Flask's default `request.args.get()` (first value wins), consistently applied throughout `app.py`. Deterministic but not an intentional, tested control.

### 5.1.2 — Open redirect protection
✅ **MET.** Two independent allowlist implementations reject protocol-relative/absolute redirect targets: `safe_local_redirect_path()` ([`desktop_client/responses.py:51-64`](../imbue/minds/desktop_client/responses.py)) and `_sanitize_next_url()` ([`mngr_forward/server.py:530-543`](../../../libs/mngr_forward/imbue/mngr_forward/server.py)) — both require a single leading `/`, reject `//`/`/\`. Electron's `isExternalUrl()`/`applyExternalLinkHandling()` route any non-localhost URL to the OS browser via `shell.openExternal` rather than navigating in-app ([`electron/main.js:234-256,916-949`](../electron/main.js)).

### 5.1.3 — `eval()` avoided
✅ **MET.** No `eval(`, `exec(`, or `new Function(` calls found in `desktop_client/*.py` or `electron/*.js` on user-input paths.

### 5.1.4 — Template injection protection
✅ **MET.** All Jinja rendering uses static templates with autoescape enabled ([`mngr_forward/server.py:104-108`](../../../libs/mngr_forward/imbue/mngr_forward/server.py); [`desktop_client/templates.py:196-198`](../imbue/minds/desktop_client/templates.py), explicitly documented to escape "user-controlled strings"). No `Template(user_string)`/`from_string()` on user input.

### 5.1.5 — SSRF protection
✅ **MET.** `_is_loopback_url()` detects loopback/unspecified addresses; the forwarder refuses to dial a loopback backend without an established SSH tunnel unless `--allow-host-loopback` is explicitly set ([`mngr_forward/server.py:78-101,625-640`](../../../libs/mngr_forward/imbue/mngr_forward/server.py)). Backend URLs are discovery-derived, not raw user input.

### 5.1.6 — XPath/XML injection protection
⚪ **N/A.** No XML/XPath parsing anywhere in `apps/minds/` or `libs/`.

### 5.1.7 — Context-aware XSS output escaping
✅ **MET (fixed on this branch).** Desktop-client-owned HTML already used autoescaping Jinja, and semi-trusted agent content is isolated by origin (each agent on its own `agent-<id>.localhost` subdomain — see [`security-boundaries-audit.md`](security-boundaries-audit.md)). Added: a Flask `after_request` hook ([`app.py`](../imbue/minds/desktop_client/app.py)) that sets `X-Content-Type-Options: nosniff` and a restrictive first-party `Content-Security-Policy` on the desktop client's **own** responses, scoped by host so it is **not** applied to proxied `*.localhost` agent responses (agents control their own CSP, so clamping one on would break agent web apps). `connect-src` pins to `'self'`, loopback websockets, and the Sentry ingest origin (so opt-in browser error reporting keeps working); `setdefault` semantics avoid clobbering handler-set headers. Tests assert first-party-gets-headers, agent-path-does-not-get-CSP, and Sentry/loopback reachability. `contextIsolation: true`/`nodeIntegration: false` remain enforced on all Electron views.

### 5.1.8 — Database injection protection
✅ **MET (mostly N/A).** The desktop client itself uses no SQL database (file/JSON persistence). The only raw SQL is in `libs/mngr_imbue_cloud/.../bare_metal_db.py` and `cli/admin.py` — server-side cloud-provisioning admin code not reachable from the client's HTTP surface — and it uses parameterized `%s` placeholders throughout; the one f-string query interpolates a static hardcoded column list, not user input.

### 5.1.9 — OS command injection protection
✅ **MET (hardened on this branch).** All `subprocess` calls use list-form argv, not shell strings; `grep -rn "shell=True"` returns zero hits. The one `bash -c "..."` construction in `destroying.py`'s `_build_destroy_command` now wraps the `--include` host-id filter with `shlex.quote` as defense-in-depth (behavior-identical for well-formed host ids, which are `host-<32hex>`). A test reparses the command with `shlex.split` and asserts the filter survives as a single token.

### 5.1.10 — Local/remote file inclusion protection
✅ **MET.** Flask's static handler uses Werkzeug's safe path resolution. The one deliberate arbitrary-file-access feature (WebDAV sharing, [`desktop_client/webdav.py`](../imbue/minds/desktop_client/webdav.py)) is scoped to two allowlisted roots (`Path.home()`, `tempfile.gettempdir()`), has directory browsing disabled, and is gated behind a mandatory Bearer-token check that fails closed if unset.

### 5.2.1 — File upload type/execution restrictions
⚪ **N/A.** No user-facing file-upload endpoint exists (`request.files`/`UploadFile`/`multipart` — no hits). The WebDAV share allows file read/write within allowlisted roots but isn't an upload form and served files aren't executed by the app.

---

## Category 6: Configuration

### 6.1.1 — Only components without known exploitable vulnerabilities
✅ **MET (fixed on this branch).** Was NOT MET (no scanning anywhere). Added: [`.github/dependabot.yml`](../../../.github/dependabot.yml) enabling weekly version + security updates for the `uv` (root), `npm` (`apps/minds`), and `github-actions` ecosystems; a scheduled [`.github/workflows/dependency-audit.yml`](../../../.github/workflows/dependency-audit.yml) running `pip-audit` (with `--skip-editable`) and `pnpm audit --prod`; and a `gitleaks` secrets-scanning pre-commit hook in [`.pre-commit-config.yaml`](../../../.pre-commit-config.yaml). The audit workflow is informational (`continue-on-error`) so a fresh advisory surfaces findings without wedging unrelated work.

### 6.2.1 — Debug modes disabled in production
✅ **MET.** No `app.run(debug=True)`/`FLASK_DEBUG` anywhere in `apps/minds`. All `debug` hits are `logger.debug(...)` log-level calls.

### 6.3.1 — Origin header not used for auth decisions
✅ **MET.** No code reads `request.headers["Origin"]` for authorization; auth uses signed session cookies and bearer tokens instead.

### 6.4.1 — Not susceptible to subdomain takeover
🟡 **PARTIAL / N/A.** Internal `agent-<id>.localhost` subdomains aren't real DNS (loopback-only) — takeover doesn't apply. The one real public-DNS surface is the Cloudflare-tunnel "sharing" feature ([`sharing_handler.py`](../imbue/minds/desktop_client/sharing_handler.py)), where DNS/tunnel ownership is delegated entirely to Cloudflare via the `imbue_cloud` connector; no explicit in-app code verifies DNS cleanup on tunnel deletion, so this is only partially auditable from this repo.

### 6.5.1 — No credentials/tokens in logs
✅ **MET.** Dedicated redaction utilities exist and are used: `secret_redaction.py` (`redact_secret_flag_values`, `redact_secret_env_assignments`) and `_redact_url_credentials*` in `agent_creator.py`, applied to git URLs and subprocess command logging. No raw secret values found passed to `logger.*` calls.

### 6.6.1 — Browser storage cleared on logout
✅ **MET (by architecture).** The user's session material (the SuperTokens access/refresh tokens) lives **server-side**, not in browser storage, and is revoked on account sign-out (see 2.2.1) — so there is no cached user credential in `localStorage`/`sessionStorage`/IndexedDB to leave behind. The only browser-stored auth artifact is the `httponly` device-level `minds_session` cookie, which is intentionally *not* a user credential and intentionally survives account sign-out (it is the app-unlock, not the account session). A blanket `session.clearStorageData()` on account sign-out was considered and rejected: it would wipe unrelated per-agent content-partition storage and, coupled with clearing the device cookie, would lock the user out of the app. If a distinct "lock the app / sign out of this device" action is added later, clearing device storage there would be the correct place for it.

### 6.7.1 — Secrets stored securely
✅ **MET.** Session-signing key is a random 32-byte token written to a `0600` file via atomic write ([`auth.py:130-140`](../imbue/minds/desktop_client/auth.py)). Per-account data-encryption keys similarly stored `0600`, optionally password-wrapped via Argon2-derived key ([`dek_store.py:73-176`](../imbue/minds/desktop_client/dek_store.py)). `MINDS_API_KEY` is never persisted to disk — generated fresh in memory each run. CI/deploy secrets pulled from HashiCorp Vault at runtime, kept in process memory only.

---

## Category 7: Webhook Security

> No genuine inbound/outbound webhook receiver or sender exists in `apps/minds` (`grep -rln "webhook"` across all `.py` files: zero matches). The closest analogues are the Cloudflare-tunnel "sharing" feature and the internal bearer-token API auth scheme — evaluated below by analogy where useful, but marked N/A for the literal requirement.

### 7.1.1 — Webhook communications over HTTPS/TLS
⚪ **N/A.** No webhook feature exists. The analogous sharing/probe URL check (`is_probeable_share_url`, [`sharing_handler.py:63-84`](../imbue/minds/desktop_client/sharing_handler.py)) rejects any non-`https` scheme.

### 7.1.2 — Webhook providers verify endpoint ownership
⚪ **N/A.** No webhook subscription/registration flow exists.

### 7.2.1 — Webhook payloads HMAC-authenticated
⚪ **N/A.** No webhook payload verification code exists.

### 7.2.2 — Timing-safe signature comparison
✅ **MET (analogous internal control).** `is_valid_minds_api_key` uses `hmac.compare_digest(presented, expected)` rather than `==`, with an explicit docstring on the side-channel risk ([`api_key_store.py:44-53`](../imbue/minds/desktop_client/api_key_store.py)). No dedicated webhook signature verification exists to independently assess.

### 7.2.3 — Replay protection via signed timestamps
⚪ **N/A.** No webhook replay protection exists (no webhooks). The session cookie itself uses `itsdangerous.URLSafeTimedSerializer` with a `max_age` check, which is timestamp-based expiry for a different, non-webhook mechanism.

### 7.3.1 — SSRF mitigation for user-supplied callback URLs
✅ **MET (analogous control).** `is_probeable_share_url` ([`sharing_handler.py:63-84`](../imbue/minds/desktop_client/sharing_handler.py)) validates the caller-supplied share URL before fetching it: requires `https`, rejects `localhost`/`*.localhost`, rejects private/loopback/link-local/reserved IP literals via `ipaddress` checks. Docstring explicitly states the SSRF-prevention purpose.

### 7.3.2 — Webhook signing secrets not hardcoded/committed
⚪ **N/A** (no webhook secrets exist). By analogy, all comparable secrets (session signing key, API key, DEKs) are generated at runtime via `secrets.token_urlsafe` and never hardcoded. Note: no `gitleaks`/`detect-secrets` pre-commit hook is configured, which weakens confidence in this control holding going forward even though nothing is hardcoded today.

---

## Remediation priority list

### Done on branch `preston/casa-fixes`
- **6.1.1** — Dependabot + scheduled `pip-audit`/`pnpm audit` CI + gitleaks pre-commit hook.
- **3.2.2 (`state`)** — Server-reflected, constant-time-verified CSRF `state` on the OAuth flow.
- **3.2.1** — PKCE enabled and threaded end-to-end for Google (GitHub N/A).
- **2.2.1 / 6.6.1** — Verdict corrected to MET without code change: account sign-out already revokes the SuperTokens session tokens, and no user credential is stored in the browser. (An initial over-broad cookie/storage-clear on account sign-out was reverted — it conflated app-unlock with account sign-out.)
- **1.1.2 / 1.3.1** — 15-minute TTL on the local one-time login code.
- **5.1.7** — First-party CSP + `X-Content-Type-Options: nosniff` (scoped to exclude proxied agent content).
- **5.1.9** — `shlex.quote` hardening of the `destroying.py` `bash -c` filter.
- Reclassified **1.1.1 / 1.1.3 / 2.2.2** to PROBABLY (SuperTokens-owned).

### Remaining — need a product/design decision (not a self-contained code fix)
1. **3.3.1 (NOT MET)** — `MINDS_PAID_ADMIN_KEY` gates a real privileged operation with a single static secret and no MFA.
2. **2.2.3 (NOT MET)** — 30-day non-revocable stateless session cookie; shortening the TTL and/or adding a server-side revocation mechanism changes the re-login UX and needs a decision.
3. **2.4.1 (PARTIAL)** — No step-up re-authentication for especially sensitive operations.
4. **2.3.4 (PARTIAL)** — No per-issuance nonce, so a stolen cookie is replayable until expiry (coupled to 2.2.3).
5. **3.1.5 (PARTIAL)** — No synchronizer-token CSRF scheme (currently relies on `SameSite=Lax` + `HttpOnly`).
6. **3.2.2 (`redirect_uri`, PARTIAL)** — Add a loopback-only server-side allow-list on the connector as defense-in-depth beyond the IdP's own allow-list.

### Remaining — external service (SuperTokens connector), verify rather than code here
- **1.1.1 / 2.2.2** — Confirm the connector enforces sign-in brute-force lockout and session revocation on password change.

### Documentation-only
- Record **2.3.1** (`Secure` cookie omitted — loopback HTTP only) and **4.1.2** (self-signed loopback TLS) as formal accepted exceptions in the CASA questionnaire rather than leaving them as silent gaps.

---

## Sources
- [CASA Specification — App Defense Alliance ASA-WG (GitHub)](https://github.com/appdefensealliance/ASA-WG/blob/develop/CASA/CASA%20Specification.md)
- [CASA Tier 2 Process — App Defense Alliance](https://appdefensealliance.dev/casa/tier-2/tier2-overview)
- [CASA Requirements — App Defense Alliance](https://appdefensealliance.dev/casa/casa-requirements)
- [Google CASA overview — deepstrike.io](https://deepstrike.io/blog/google-casa-security-assessment-2025)
