# Convert the Minds API to spectree + pydantic validation

> **Refined prompt:** let's work through any details required to *actually* convert to using spectree and using the pydantic models for validation -- it really should be possible to do so without introducing any regressions (you just need to be careful to actually convert the necessary handlers on both the front end and back end for relevant routes that might change)
>
> * Adopt **spectree** (new runtime dep) as the single source of truth for `/api/v1`: handlers carry pydantic request models (body **and** query); spectree both validates requests and generates the OpenAPI.
> * Models move out of `api_schema.py` into a new lower module `api_models.py`, imported by both the handlers and the schema (avoids the `api_schema`->`api_v1` import cycle).
> * `/api/schema` serves **spectree's generated spec, filtered to gateway-reachable routes** (still excluding `/desktop` and `/files`), keeping the existing auth + gateway-filter wrapper; the hand-built generator is removed.
> * **All** `/api/v1` routes are decorated (including the cookie-only `/desktop` namespace); JSON request bodies **and** query params are validated and documented.
> * **Response validation is enforced**: every JSON success route returns its pydantic response model; accurate response models are defined for all of them.
> * **Auth runs before validation** (an unauthenticated request gets 401, never a pre-auth 422).
> * Validation failures return a custom stable **422** `{"errors": [{"field", "message"}]}` where `field` is the dotted pydantic `loc`; produced by one app-level handler and documented in the schema.
> * Create's **semantic** errors (account-required + `redirect_url`, `anthropic_api_key` required, invalid name) stay handler-emitted as `{error, field, redirect_url}` at 400 -- so create has two error shapes, both handled.
> * A shared `static/api_errors.js` normalizes any API error response into `{message, field}`; every JS/Jinja/Electron consumer routes through it; create maps a 422 `field` back to the form-field highlight.
> * Operation polling is restructured from `/workspaces/operations/<id>` to `/workspaces/operations/<type>/<id>` (`type` in `create|destroy|restart`), with matching `/logs` and `DELETE`; each type gets a precise model; old untyped routes are hard-cut and pollers updated in lockstep.
> * A coverage test asserts every gateway-reachable `/api/v1` route is spectree-decorated. Scope is the **minds repo only** (forever-claude-template agent consumers come later).

---

## Implementation status & handoff (READ THIS FIRST)

This section is the source of truth for picking the work up cold. Everything below the `## Overview` heading is the original design; this section records what is **already landed**, the **traps discovered**, the **one open decision**, and the **exact remaining work**.

### Branch / PR
- Branch `mngr/minds-api-final` (base `josh/more-minds-api` on origin; draft PR #2315). Commit/push as you go; the reviewer stop hook is **disabled** (`.reviewer/settings.local.json`), so there is **no autofix gate — you own quality**.
- Read `CLAUDE.md` + `style_guide.md` first (monorepo rules, `uv run` from root, FrozenModel/pydantic conventions, ratchets, changelog-per-project).

### Already landed (committed + pushed, all green)
1. **`1c3204074`** — `GET /api/schema` endpoint (hand-built generator in `api_schema.py`) + the gateway baseline grant. This is the CURRENT schema endpoint. The gateway already default-allows it: `libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py` grants `minds-api-schema-read` (GET `/minds-api-proxy/api/schema`) on the `latchkey-self` scope. **The schema *path* is unchanged by this plan, so no further gateway change is needed.**
2. **`34051367f`** — extracted all request/response pydantic models into `apps/minds/imbue/minds/desktop_client/api_models.py` (the single-source module). `api_schema.py` imports them.
3. **`fb5b5bebe`** — spectree groundwork:
   - `spectree>=1.4` added to `apps/minds/pyproject.toml` (runtime dep; `uv.lock` synced). Installed version is **2.0.1**.
   - **`api_auth.py`** (new): `require_api_or_cookie_auth` + `json_response`/`json_error`/`json_field_error`/`handle_invalid_random_id` moved here (out of `api_v1.py`). `api_v1.py` re-imports them aliased to the old `_json_*` names (no call-site churn); `api_schema.py` imports the auth decorator from here. **The `api_schema`→`api_v1` import cycle is now broken** (handlers can safely import from `api_models`).
   - **`api_spec.py`** (new): `API_SPEC = SpecTree("flask", openapi_version="3.1.0", ...)` with a `before` hook that reshapes any request-validation failure into the agreed **422 `{"errors": [{"field": "<dotted pydantic loc>", "message": "..."}]}`**.

### Verified integration mechanics (from a throwaway spike — trust these)
- Decorator order: **`@require_api_or_cookie_auth` OUTERMOST, `@API_SPEC.validate(json=, query=, resp=)` INNER.** `functools.wraps` propagates spectree's attributes up, so the route still appears when the spec is built, and **auth runs before validation** (unauthenticated + bad body → 401, not 422). ✅ required by the design.
- `@API_SPEC.validate(...)` **enforces request validation at request time WITHOUT calling `API_SPEC.register(app)`** — so decorating routes does NOT expose any `/apidoc/*` doc UI. `register(app)` is only needed to *build the full spec* (for Phase A's `/api/schema`-from-spectree); when you do call it, suppress/avoid serving the public doc endpoints.
- `openapi_version="3.1.0"` is honored and validates against the existing `openapi-spec-validator` test.
- `API_SPEC.spec` must be read inside a Flask app context (fine for the request-time `/api/schema` handler).

### Regression traps (MUST handle — these are why naive decoration breaks "no regressions")
1. **`extra="forbid"`**: the `api_models` are `FrozenModel` (forbids extra keys). Used as request models they would **422 any request with extra fields** that handlers ignore today (notably `report`, which passes arbitrary fields through). → **Request models must be `extra="ignore"`.** (Response/doc models can stay strict.) Consider a small `_RequestModel(BaseModel)` base with `model_config = ConfigDict(extra="ignore")` for request bodies/queries.
2. **Content-type**: spectree parses JSON only for `application/json`; handlers use `get_json(force=True)` (parses regardless). The create form + Flask test client send `application/json`, and the agent/gateway contract is JSON, so this is usually fine — but **audit callers** and ensure none rely on a missing/other content-type, or you'll newly 422 them.
3. **Optional/absent body**: routes that tolerate an absent body today must use an all-optional request model (or skip `json=` on them) so they don't start 422-ing.
4. **Response byte-fidelity (the 2b landmine)**: runtime-enforcing response models means returning model instances whose serialization must EXACTLY match today's JSON across ~26 routes (keys, null/optional handling, 202 status), or drift → **500**. This is the one open decision (below).

### THE ONE OPEN DECISION — confirm with the user before Phase A
**Response models: document-only (recommended) vs. runtime-enforced (original 2b).**
- **Recommended: document-only.** Pass `resp=` models to `@API_SPEC.validate(...)` purely so the generated OpenAPI documents responses, but **keep handlers returning their existing `make_response(json.dumps(...))` Flask `Response` objects** (spectree does not enforce/serialize a raw `Response`, so there is **zero response-shape regression risk and no 500s**). This still achieves single-source-of-truth: the schema's response section is generated from the same models. It relaxes the earlier "2b enforce" answer specifically because two implementation forks showed runtime response-enforcement fights the "no regressions" requirement.
- Alternative (strict 2b): refactor every success path to return the model instance and verify byte-fidelity per route. Higher fidelity, much higher risk + slower.

The rest of this handoff assumes **document-only** unless the user says otherwise.

### Remaining work

**Phase A — backend (request/query validation + schema-from-spectree).** Do it in small route groups, committing green between groups.
- In `api_models.py`: give request/query models `extra="ignore"`; add the still-missing models — query models for `sharing/<svc>/readiness?url=` and `desktop/stop-hosts?agent_id=` (repeated → list), and response (doc) models for the routes not yet covered (`version`, `backups`, `health`/`HostHealthResponse`, sharing-status, the four `/desktop/*`, and the per-type operations responses). Reuse existing models where they already match.
- Decorate every `/api/v1` handler in `api_v1.py` with `@require_api_or_cookie_auth` (outer) + `@API_SPEC.validate(json=/query=/resp=...)` (inner). Remove only the **structural** manual checks spectree now covers (malformed/missing/typed-wrong → now 422); **keep all semantic checks** (create's account-required + `redirect_url`, `anthropic_api_key` required, invalid-name `{error, field}` 400; the readiness `url` SSRF guard `is_probeable_share_url`; 404/409/501/502 paths).
- **Operations restructure**: replace `GET/DELETE /workspaces/operations/<operation_id>` and `.../logs` with `/workspaces/operations/<type>/<id>` (`type` ∈ `create|destroy|restart`), each with a precise response model; delete the id-prefix dispatch and the destroy-vs-restart precedence logic in `_handle_operation_status`/`_handle_operation_logs`/`_handle_dismiss_operation`. Update the create/destroy/restart handlers that build the polled/redirect URLs (e.g. the create `redirect_url`, the 202 handles) to the typed form.
- **`/api/schema`**: replace the hand-built generator internals in `api_schema.py` with: `API_SPEC.register(app)` (docs NOT publicly served) + serve `API_SPEC.spec` **filtered to gateway-reachable routes** (keep the existing `_is_gateway_reachable_path` exclusions: `/api/v1/desktop/`, `/api/v1/files`) wrapped by `require_api_or_cookie_auth`. Keep the path `/api/schema`. Update `api_schema_test.py` to validate the spectree-sourced doc and keep the drift assertion (documented paths == gateway-reachable routes).
- **Coverage test**: assert every gateway-reachable `/api/v1` route appears in the generated spec (i.e. is decorated) — a new undecorated route fails CI.
- Update `api_v1_test.py`: malformed/missing-field input now expects the **422 `{"errors":[...]}`** shape; old untyped operations URLs → typed; keep all semantic-error, 401, 404, 501 assertions.

**Phase B — front-end (lockstep, or the UI breaks).**
- Add `apps/minds/imbue/minds/desktop_client/static/api_errors.js`: normalize any API error response — the 422 `{"errors":[{field,message}]}` list OR create's 400 `{error, field, redirect_url}` — into `{message, field}`.
- Route every server-error display through it: `static/creating.js`, `static/sharing.js`, `static/destroying.js`, `static/workspace_settings.js`, `templates/pages/Create.jinja`, `templates/Associate.jinja`, `templates/pages/Landing.jinja`, `electron/main.js`. **Create contract to preserve** (`Create.jinja#submitCreate`/`showCreateError(message, field)`): on `202` + `{operation_id}` → navigate `/creating/<id>`; else if `{redirect_url}` → navigate; else `showCreateError(error, field)` which reveals the advanced view and rings `document.getElementById(field)`. The normalizer must yield a `field` that matches the form input ids for create's field-level errors.
- Update the create/destroy/restart pollers and the Electron restart/recovery flows to call the typed `/api/v1/workspaces/operations/<type>/<id>` (+`/logs`, +`DELETE`) URLs.
- Manual verification: the desktop UI flows need a real Electron run (`just minds-start`); pytest cannot verify JS.

### How to verify / test (per CLAUDE.md)
- `just test-quick "apps/minds/imbue/minds/desktop_client"` (full suite, currently 1134 passing) + `apps/minds/imbue/minds/test_ratchets.py` (stage changes first) + `uv run ty check` on changed files + `ruff format`/`ruff check`. Touch latchkey tests only if you change `mngr_latchkey`. Do **not** run full `just test-offload`; CI runs the rest.
- Manually exercise (build app via `create_desktop_client`, hit routes): `GET /api/schema` → valid filtered OpenAPI; malformed body → 422 custom shape; unauthenticated → 401; create no-account → 400 with `redirect_url`; a success response is byte-identical to before (document-only); a typed operations URL works.
- Changelog: update `apps/minds/changelog/mngr-minds-api-final.md`; add a `dev/changelog/mngr-minds-api-final.md` note if you touch root config; `libs/mngr_latchkey` already has an entry from prior work (only touch if you change it).

### Key files
- `apps/minds/imbue/minds/desktop_client/`: `api_v1.py` (26 routes, handlers to decorate), `api_models.py` (the models), `api_auth.py` (auth + json helpers), `api_spec.py` (`API_SPEC` + 422 hook), `api_schema.py` (current hand-built `/api/schema`, to be re-sourced), `state.py`, `app.py` (blueprint registration + where `API_SPEC.register` would go), `responses.py` (`make_response` etc.).
- Tests: `api_v1_test.py`, `api_schema_test.py`. Front-end: `static/*.js`, `templates/**/*.jinja`, `electron/main.js`.
- Gateway (done, reference only): `libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py` (`_PERM_MINDS_API_SCHEMA`).

---

## Overview

- Make pydantic models the **single source of truth** for the `/api/v1` surface: one set of models both validates requests/responses at runtime (via **spectree**) and generates the published OpenAPI -- eliminating the current split where `api_schema.py` holds documentation-only models that can silently drift from the hand-written handler logic.
- **spectree** (pydantic-native) is chosen over the marshmallow stacks (flask-smorest/APIFlask) to honor the repo's "validation only through pydantic" rule; it is added as a runtime dependency because validation now happens on every request.
- The conversion is **behavior-preserving where it matters and deliberately behavior-changing where agreed**: auth still runs first; create's bespoke semantic errors are untouched; but malformed input now returns a uniform, documented 422 instead of each handler's ad-hoc 400, and the front-end is updated in lockstep so nothing breaks.
- Two structural cleanups ride along because they make the models clean and remove existing hacks: **operation polling becomes type-segmented** (`/operations/<type>/<id>`), which deletes the id-prefix dispatch and the destroy-vs-restart precedence workaround; and the **models/auth move to a lower module** so there is no import cycle.
- A **coverage test** plus the existing OpenAPI-validation/drift tests keep the single-source-of-truth guarantee from regressing.

## Expected behavior

- An agent (or the UI) calling a route with a malformed/typed-wrong/missing-field body or query param gets a stable **422** `{"errors": [{"field": "<dotted loc>", "message": "..."}]}` describing every problem, instead of the previous per-route 400 `{"error": "..."}` strings.
- An **unauthenticated** request still gets **401** and is never body-validated (no pre-auth 422 or input echo).
- A successful response whose body fails to match its declared model surfaces as a **500** (enforced response validation) -- a developer-time signal that a handler and its model drifted; well-formed responses are unchanged on the wire.
- The **create form** behaves exactly as today from the user's view: a structural problem (e.g. empty `git_url`) highlights the offending field via the shared error normalizer, and the existing semantic outcomes (no account -> sign-up redirect, missing API key -> field error, bad name -> field error) are unchanged.
- Every UI flow that surfaces server errors (create, destroy, sharing, workspace settings/association, provider toggle, the Electron quit/restart flows) shows a correct human message, and field-specific ones still ring the right field, because all of them go through one client-side error normalizer.
- `GET /api/schema` returns the same gateway-filtered OpenAPI 3.1 surface as today (only gateway-reachable routes; `/desktop` and `/files` excluded), now sourced from spectree so request/response/query schemas are complete and always match what the handlers actually accept and return.
- Operation polling moves to `/api/v1/workspaces/operations/<type>/<id>` (and `/logs`, and `DELETE`); the create/destroy/restart pages and the Electron restart/recovery flows poll the typed URL they already know the type for. The old untyped URLs no longer exist.
- The full OpenAPI (including cookie-only `/desktop` routes) is generated internally, but only the gateway-reachable subset is exposed at `/api/schema`; no separate Swagger/Redoc UI is served.
- A brand-new workspace can still fetch `/api/schema` immediately (gateway baseline grant is unchanged).

## Changes

**Dependencies**
- Add `spectree` as a runtime dependency of the minds app.

**Models (new single source of truth)**
- Add a new lower-level module holding all `/api/v1` request, query, and response models (moved out of `api_schema.py`), so both the route handlers and the schema import it without an import cycle.
- Define accurate request models (body and, where present, query) and response models for **every** JSON route, including the `/desktop` namespace; the operations responses become three precise per-type models; the `agent_id`-list query (stop-hosts) and the readiness `url` query get query models.
- Relocate the shared auth decorator so it sits below both the handlers and the schema (supporting the no-cycle move).

**Backend handlers**
- Decorate every `/api/v1` handler with spectree request/response validation, ordered so the existing auth check runs first.
- Refactor each JSON route's success path to return its pydantic response model (so spectree validates and serializes it); error paths stay as explicit responses.
- Keep create's (and any peer's) **semantic** validations in the handler, emitting the existing `{error, field, redirect_url}` 400 shape; only structural/type validation moves to spectree.
- Keep handler-side semantic guards that pydantic can't express (e.g. the readiness `url` SSRF check) in the handler.
- Register one app-level error handler that converts spectree/pydantic validation failures into the custom 422 `{"errors": [...]}` model.
- Restructure the operation routes to `/workspaces/operations/<type>/<id>` (+ `/logs`, + `DELETE`), replacing the single id-keyed handler and removing the id-prefix dispatch and destroy-vs-restart precedence logic.

**Schema endpoint**
- Wire a single spectree instance into the app and register it after the blueprints so it can generate the spec from the decorated routes.
- Replace the hand-built generator: `/api/schema` now filters spectree's generated OpenAPI down to gateway-reachable routes (excluding `/desktop` and `/files`) and serves it, keeping the existing auth wrapper and gateway-filter; do not expose spectree's own docs UI or unfiltered spec.

**Front-end (the "convert both ends" part)**
- Add a shared client error normalizer (`static/api_errors.js`) that turns any API error response (the 422 `{"errors": [...]}` list or create's 400 `{error, field, redirect_url}`) into `{message, field}`.
- Route every server-error display through it: `creating.js`, `sharing.js`, `destroying.js`, `workspace_settings.js`, `Create.jinja`, `Associate.jinja`, `Landing.jinja`, and `electron/main.js`; create maps a returned `field` to the form input highlight.
- Update the create/destroy/restart pollers and the Electron restart/recovery flows to call the new typed `/operations/<type>/<id>` URLs.

**Tests**
- Add a coverage test asserting every gateway-reachable `/api/v1` route is spectree-decorated (a new route without a model fails CI).
- Update the existing `/api/schema` validation/drift tests to assert against the spectree-sourced document (still valid OpenAPI 3.1; documented paths exactly match gateway-reachable routes; `/desktop` and `/files` excluded).
- Add tests for the 422 contract (malformed body/query -> 422 `{"errors":[{field,message}]}`), auth-before-validation (unauth -> 401 not 422), the preserved create semantic errors, and the typed operation routes.
- Update existing route tests that asserted the old 400/`{error}` shape or the old untyped operations URLs.

**Changelog**
- One entry each for `apps/minds`, `dev` (root dependency-group change for spectree), and any other touched project.

## Out of scope / deferred

- Updating forever-claude-template agent consumers to the new error/route shapes (the agent-facing routes are not yet wired into FCT; that conversion happens later).
- Serving a developer Swagger/Redoc/Scalar UI or an unfiltered public spec.
- Pushing create's semantic checks into pydantic validators (they need app state; they stay in the handler).
