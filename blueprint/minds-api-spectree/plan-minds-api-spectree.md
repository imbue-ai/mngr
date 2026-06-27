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
