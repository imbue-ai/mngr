# Plan: Reject malformed `agent_id` at the permission-request gateway

## Refined prompt

> right now this branch will check for "an" error in creating an agent id. however, I want it to check for ANY error, and notify the agent so it's aware that the permission request failed.
>
> * Validate `agent_id` against the canonical `AgentId` format in the Node gateway (`permission_requests.mjs`), because the agent's tool call is a raw HTTP POST with no Python in the path.
> * Implement the check in one language (JS) with a comment that the canonical validation is Python's `RandomId._validate` and the JS check must match it -- no shared-file machinery.
> * Enforce the strict long form (`agent-` + 32 hex per `_validate`); a prefix-only check wouldn't stop `agent-1`, which still crashes the consumer.
> * Keep the error handled on every path: since the consumers of the `AgentId`-constructing functions don't handle it, those functions keep handling it themselves (the `run.py`/`app.py` guards stay as defense-in-depth).
>
> ideally, the permission request should error immediately in the "tool call" (not sure if that's the right terminology) that the agent tries. Please reimplement the fix at that layer so that ANY error will get propagated to the agent.
>
> * Reject an invalid request before it is persisted (never added to the pending list); the gateway's existing top-level try/catch reports the error to the agent.
> * Surface the failure via the gateway's existing error path -- HTTP 400 `{"error": ...}`, exactly what the agent's POST receives.
> * Use that one canonical validity check everywhere (gateway + consumer + tests); conform the existing `agent-1` fixtures to a valid id.
> * Match `_validate`'s case-insensitive hex acceptance (`^agent-[0-9a-fA-F]{32}$`); if it doesn't care, the gateway shouldn't either.
> * The 400 message echoes the offending value back so the agent can self-correct.
> * No legitimate non-agent caller supplies a non-`agent-...` identifier, so strict validation won't break a real flow.
> * Add a `libs/mngr_latchkey` changelog entry for the gateway validation; keep and reword the `apps/minds` entry to note the gateway is now the primary fix.

## Overview

- The bug: an agent can `POST /permission-requests` with a non-`agent-...` `agent_id` (e.g. a hand-crafted body with `ENV_AGENT`). The gateway only checks the field is a non-empty string, persists it, and returns 201. Later, the consumer constructs `AgentId(event.agent_id)`, which raises `InvalidRandomIdError`, kills the `latchkey-permission-requests-consumer` thread, and silently stops every subsequent permission request -- for any agent or service -- from reaching the UI (re-crashing on the same record on each restart).
- This branch currently fixes it defensively on the *consumer* side (`run.py`, `app.py`) by catching `InvalidRandomIdError`. That fix has since also landed on `main` (commit `752c16b4b`).
- The key decision: move the *primary* fix up to the producer layer -- the gateway -- so a malformed `agent_id` is rejected at the agent's tool call (the raw HTTP `POST`), the request is never persisted, and the agent is notified immediately via the HTTP error response.
- Architectural constraint: the agent's "tool call" is a raw HTTP POST straight to the Node latchkey gateway (`permission_requests.mjs`). There is no Python layer in the request path, so we cannot reuse the Python `AgentId`/`RandomId._validate` function at the gateway. We replicate the format as a JS regex, documented as mirroring `RandomId._validate`, and tie the two together with tests.
- The validation is strict by necessity: only the full `agent-` + 32-hex form guarantees the consumer's `AgentId(...)` parse cannot fail. A prefix-only check would let `agent-1` through and would not actually fix the crash.
- The consumer-side guards stay as defense-in-depth: the callers of those functions do not handle the error (an unhandled raise kills the consumer thread / takes down the request panel), so the functions must keep handling it themselves -- and this also covers any already-persisted bad records and non-gateway code paths. Keeping them also avoids diverging from `main`.

## Expected behavior

- An agent that POSTs a well-formed `agent_id` (`agent-` + 32 hex chars, any case) sees no change: the request is created and returns 201 as before.
- An agent that POSTs a malformed `agent_id` (wrong prefix, wrong length, non-hex, placeholder like `ENV_AGENT`, or `agent-1`) now receives an immediate HTTP 400 with body `{"error": "Invalid request body: 'agent_id' must be a valid agent id ('agent-' followed by 32 hex characters); got '<value>'."}`.
- A rejected request is never written to disk and never appears in the pending-requests stream/list. The 201 success path is the only path that persists a request.
- The `latchkey-permission-requests-consumer` thread can no longer be killed by a malformed `agent_id` arriving through the gateway, because such requests never get persisted.
- The existing consumer-side guards still skip any malformed record they encounter (e.g. legacy records persisted before this change, or records written by a future non-gateway path), so the consumer remains robust regardless of upstream validation.
- Validation case-sensitivity matches the Python `RandomId._validate` contract: upper- and lowercase hex are both accepted (real ids from `uuid4().hex` are lowercase).

## Implementation plan

### Gateway validation (primary fix) -- `libs/mngr_latchkey/imbue/mngr_latchkey/extensions/permission_requests.mjs`

- Add a module-level constant near `VALID_REQUEST_ID_PATTERN` (line 116):
  - `const VALID_AGENT_ID_PATTERN = /^agent-[0-9a-fA-F]{32}$/;`
  - Comment: the canonical definition is Python's `imbue.imbue_common.ids.RandomId._validate` (as specialized by `AgentId`, prefix `agent`). This regex must stay in sync with it: `agent-` prefix + exactly 32 hex chars. Case-insensitive to match `_validate`'s `int(hex_part, 16)` acceptance. A cross-language test (see Testing) guards against drift.
- In `parsePermissionRequestBody` (line 642), immediately after `ensureNonEmptyString('', 'agent_id', parsed.agent_id)` (line 657), add a format check (small helper `ensureValidAgentId(value)` mirroring `validateRequestId`'s style, or an inline check):
  - If `!VALID_AGENT_ID_PATTERN.test(parsed.agent_id)`, throw
    `new InvalidRequestBodyError(`'agent_id' must be a valid agent id ('agent-' followed by 32 hex characters); got '${parsed.agent_id}'.`)`.
- No other change needed in `handleCreateRequest` (line 1176) or the top-level handler (lines 1468-1501): the existing `try/catch` already converts a thrown `InvalidRequestBodyError` (statusCode 400) into `sendError(response, 400, ...)`, and validation already runs before `writeJsonFileAtomic`/`notifyNewRequest`, so a rejected request is never persisted or streamed.

### Consumer-side guards (defense-in-depth, keep) -- no code change

- `apps/minds/imbue/minds/cli/run.py` `_StreamedPermissionRequestHandler._maybe_recover_host_permissions` (the `try/except InvalidRandomIdError` around `AgentId(event.agent_id)`): keep as-is. Rationale (per the "handle the error where the caller doesn't" principle): the consumer thread that calls this does not handle the exception -- an unhandled raise kills the thread -- so the function must handle it itself.
- `apps/minds/imbue/minds/desktop_client/app.py` `_displayable_pending_requests` (the `try/except InvalidRandomIdError` around `AgentId(req.agent_id)`): keep as-is. Rationale: its caller renders the request panel and does not handle the exception -- an unhandled raise would take down the whole panel.
- These guards now primarily defend against already-persisted legacy records and any future non-gateway writer, since the gateway prevents new malformed records.

### Tests

- `libs/mngr_latchkey/imbue/mngr_latchkey/extensions/permission_requests_test.py` (Python test that spawns the Node extension):
  - Introduce a shared valid-id constant/fixture, e.g. `VALID_AGENT_ID: Final = "agent-" + "0" * 32` (a literal valid id) or import `from imbue.mngr.primitives import AgentId` and use `AgentId.generate()` (preferred for the end-to-end drift test below). `mngr_latchkey` already depends on `imbue-mngr` (workspace), so the import is available.
  - Replace all ~22 `"agent_id": "agent-1"` literals (and the `assert parsed["agent_id"] == "agent-1"` at line 242) with the shared valid id so existing tests keep returning 201.
  - Add `test_post_rejects_malformed_agent_id`: POST a body with `agent_id` values that should fail (`"ENV_AGENT"`, `"agent-1"`, `"agent-" + "g"*32` non-hex, `"agent-" + "0"*31` too short, `"agent-" + "0"*33` too long, missing prefix `"0"*32`). Assert status 400, assert the error message names `agent_id`, and assert no request file was persisted in the permission-requests directory.
  - Add `test_post_accepts_generated_agent_id` (the belt-and-suspenders drift guard, Q12b): POST with `AgentId.generate()` as `agent_id`, assert 201, and assert the persisted record round-trips through `AgentId(...)` without raising. This pins the JS regex to the real Python format so the two cannot silently drift.
- Existing consumer-guard unit tests (the tests added with the `run.py`/`app.py` guards on this branch) stay unchanged, since the guards stay.

### Changelog (Q9a)

- New: `libs/mngr_latchkey/changelog/preston-invalid-request-handling.md` -- describe the new gateway-side `agent_id` format validation: malformed `agent_id` is now rejected at `POST /permission-requests` with HTTP 400, so it never gets persisted, and the agent is notified at its tool call.
- Update: `apps/minds/changelog/preston-fix-minds-permission-consumer-crash.md` -- reword to note the gateway now rejects malformed `agent_id` up front (primary fix), with the consumer-side skips retained as defense-in-depth for legacy/non-gateway records.

## Implementation phases

1. **Gateway validation.** Add `VALID_AGENT_ID_PATTERN` + the format check in `parsePermissionRequestBody`. After this phase, malformed `agent_id` POSTs are rejected with 400 and never persisted; the consumer can no longer be crashed by gateway-originated bad ids. (Consumer guards already present, so the system is fully working.)
2. **Conform existing tests.** Update the ~22 `agent-1` fixtures (and the line-242 assertion) to a shared valid id so the suite passes against the stricter gateway.
3. **Add new tests.** Add the rejection test (malformed -> 400 + not persisted) and the generated-id acceptance/drift-guard test.
4. **Changelog.** Add the `mngr_latchkey` entry and reword the `apps/minds` entry.
5. **Verification + gates.** Run the full test suite, then run the deferred review gates (`/verify-architecture`, then `/autofix`, with `/verify-conversation` in the background), and re-enable blocking mode on the stop hook.

## Testing strategy

- **Gateway unit/integration (`permission_requests_test.py`, spawns the real Node extension):**
  - All existing create/approve/file-sharing/predefined tests keep passing once fixtures use a valid id (proves the happy path is unchanged).
  - `test_post_rejects_malformed_agent_id`: parametrized over malformed ids; asserts 400, asserts `agent_id` is named in the error body, and asserts the permission-requests directory contains no new file (proves "not added to the list" and "error reported to the agent").
  - `test_post_accepts_generated_agent_id`: posts `AgentId.generate()`, asserts 201, asserts the persisted `agent_id` parses back through `AgentId(...)` (cross-language drift guard tying the JS regex to the Python source of truth).
- **Consumer guards:** the existing `run.py`/`app.py` guard unit tests continue to exercise the skip-on-malformed behavior, covering the legacy/already-persisted path that the gateway cannot retroactively clean.
- **Edge cases to cover:** wrong prefix, empty hex, 31/33-char hex, non-hex chars, uppercase hex (must be accepted), exactly-32 lowercase hex (accepted), the `ENV_AGENT` placeholder from the original bug report, and `agent-1` (the value the old fixtures used).
- **Full run:** `just test-offload` from the repo root before finishing; report the exact command and pass/fail counts.

## Open questions

- **Regex vs. `_validate` permissiveness:** Python's `int(hex_part, 16)` technically also accepts underscores and a leading sign (e.g. `1_0`, `+f...`), which `^agent-[0-9a-fA-F]{32}$` rejects. Real ids (`uuid4().hex`) never contain these, and rejecting them is arguably more correct, so the plan treats the regex as the intended canonical form. Confirm we are comfortable that the gateway is very slightly stricter than `_validate` on these never-occurring inputs (the drift-guard test only asserts real generated ids are accepted, so it stays green).
- **Shared-id fixture style:** use a fixed literal (`"agent-" + "0"*32`) for the bulk fixture replacement and reserve `AgentId.generate()` for the drift-guard test, or use `AgentId.generate()` everywhere? A fixed literal keeps existing assertions deterministic (e.g. `assert parsed["agent_id"] == ...`); generate-everywhere is more realistic but needs the expected value threaded through. Leaning fixed literal for bulk + generate for the drift test.
- **Scope of the `apps/minds` changelog reword:** keep one combined entry describing both the gateway primary fix and the retained consumer defense-in-depth, vs. leaving the existing consumer-focused entry mostly intact and only adding the `mngr_latchkey` entry. Leaning toward a light reword that points at the gateway as primary.
- **Helper vs. inline:** introduce an `ensureValidAgentId(value)` helper (mirrors `validateRequestId`) or inline the single `test()` check in `parsePermissionRequestBody`. Minor; a helper reads better if reused, but it has exactly one call site.
