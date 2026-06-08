# Plan: Live SDK test suite for `query()` and `ClaudeSDKClient`

## Refined prompt

> we want to make an exhaustive test suite for the query() and ClaudeSDKClient from here: https://code.claude.com/docs/en/agent-sdk/python.md
>
> Put all of these tests in mngr_robinhood, marked with a special mark so that they are *not* included in our CI tests (they will actually cost a decent amount to run, because they should be live integration tests and make live API calls)
>
> * Exclude from CI via a new `sdk_live` marker (registered in `conftest.py`, added as `and not sdk_live` to the offload filter expressions) **plus** a `RUN_SDK_LIVE_TESTS=1` env-var guard.
> * Skip the suite if `ANTHROPIC_API_KEY` is absent; the key is a first-party `ANTHROPIC_API_KEY`.
> * Guards live in an autouse fixture / `collection_modifyitems` hook in `conftest.py`; each test file just sets `pytestmark`.
> * Add a `just test-sdk-live` recipe (sets `RUN_SDK_LIVE_TESTS=1`, runs `-m sdk_live`) plus a "Running the live SDK tests" section in `mngr_robinhood/README.md`.
>
> The tests must *only* exercise the documented interfaces and types, and do so in an end to end way--do NOT rely on any internals or other (in process) side effects.
>
> * Cover `query()`, `ClaudeSDKClient` lifecycle (connect/query/receive_response/receive_messages, multi-turn, async-context-manager, disconnect), and control (`interrupt`/`set_model`/`set_permission_mode`).
> * Cover `can_use_tool` permission callbacks + `hooks`: one allow + one deny (assert the denied tool does not run) + one `PreToolUse` hook.
> * Cover message + content-block type-shape assertions and the session functions (`list_sessions`/`get_session_messages`/`get_session_info`/`rename_session`/`tag_session`).
> * Cover the observable, behavior-affecting subset of `ClaudeAgentOptions` (`system_prompt`, `allowed_tools`/`disallowed_tools`, `cwd`, `model`, `max_turns`, `permission_mode`, `add_dirs`, `env`, `settings`, `can_use_tool`, `hooks`); skip fields with no observable effect.
> * Custom in-process MCP tools (`tool()`/`create_sdk_mcp_server`) are out of scope.
> * Split into multiple `test_sdk_*.py` files by surface; use `async def` tests with `@pytest.mark.asyncio`.
> * Session functions: create a session via a real turn inside a `tmp_path` cwd, then read it back via the session functions with `directory=tmp_path`.
> * `interrupt()` test starts a long turn, interrupts mid-stream, asserts the stream ends, and is marked `@pytest.mark.flaky`.
>
> There is a .env file in the current directory with an API key so that the tests can be run.
>
> * Do not auto-load `.env`; require `ANTHROPIC_API_KEY` to already be exported in the environment.
>
> The purpose of these tests is clean room verification that, in fact, the implementations of those interfaces work as documented.
>
> If you happen to find any mismatch between the documentation and implementation, please flag it.
>
> * Assert documented behavior so any mismatch is a hard test failure; flag every mismatch in the final write-up.
> * Error-path tests cover only the documented exceptions (`ValueError`/`FileNotFoundError`/`AttributeError`) triggered via documented interfaces; flag the undocumented SDK exception classes as a doc gap.
> * Add `claude-agent-sdk` as a normal dependency of `mngr_robinhood`; add `pytest-asyncio` to a dev/test dependency group with `asyncio_mode = "strict"`.
> * Default to the cheapest model (the `"haiku"` alias); no turn caps. Use generous per-test timeouts.
> * Tool/permission/session tests run inside `tmp_path` so nothing touches the repo.
> * If the `claude` CLI is missing or too old at runtime, let the suite fail loudly (do not skip).

## Overview

- Build a **live, opt-in** integration suite that verifies the published Claude Agent SDK Python API end-to-end against real API calls, treating the docs page as the contract (clean-room verification).
- The suite lives entirely in `libs/mngr_robinhood`, exercises **only documented public names** imported from `claude_agent_sdk`, and never touches mngr/SDK internals or relies on in-process side effects.
- It is excluded from every CI lane by a dedicated `sdk_live` marker (added to the offload filter expressions) **and** a `RUN_SDK_LIVE_TESTS=1` env guard, so it can never run accidentally or incur cost in CI.
- Cost is bounded by defaulting to the `"haiku"` model alias and keeping prompts trivial; the suite skips cleanly when `ANTHROPIC_API_KEY` is absent and fails loudly when the `claude` CLI is missing.
- Any divergence between the documented behavior/types and the real implementation surfaces as a hard test failure and is collected into an explicit "doc/implementation mismatches" write-up at the end.

## Expected behavior

- Running `just test-sdk-live` (or `RUN_SDK_LIVE_TESTS=1 uv run pytest -m sdk_live` with `ANTHROPIC_API_KEY` exported) executes the full live suite and makes real API calls.
- A normal CI run (`just test-offload`, acceptance, and release lanes) collects **zero** `sdk_live` tests; the filter excludes them and they would be skipped by the guard regardless.
- Locally, without `RUN_SDK_LIVE_TESTS=1` or without `ANTHROPIC_API_KEY`, every `sdk_live` test is **skipped** with a clear reason rather than failing.
- If the `claude` CLI cannot be found or is too old when the suite actually runs, tests **fail loudly** (environment precondition, not a skip).
- `query()` is verified with a plain string prompt and a streaming-input (async-iterable of message dicts) prompt; the async iterator yields the documented `Message` union members, terminating in a `ResultMessage`.
- `ClaudeSDKClient` is verified across its documented lifecycle (async context manager, `connect`/`query`/`receive_response`/`receive_messages`, multi-turn on one connection, `disconnect`) and control surface (`set_model`, `set_permission_mode`, and an `interrupt()` that ends an in-flight turn).
- `can_use_tool` allow vs deny is observable: an allowed tool runs and a denied tool does not; a `PreToolUse` hook fires and can block a call.
- Message and content-block objects match documented shapes (e.g. `AssistantMessage.content` is a list of the documented blocks; `ResultMessage` carries `subtype`/`is_error`/`session_id`/`result`/`usage`).
- Session functions create-then-read round-trip inside a temp directory: a turn produces a session that `list_sessions`/`get_session_info`/`get_session_messages` can read and that `rename_session`/`tag_session` can mutate.
- The observable `ClaudeAgentOptions` subset behaves as documented (e.g. `system_prompt` steers output, `disallowed_tools`/`allowed_tools` gate tools, `cwd`/`add_dirs` set the working context, `model` selects the model, `env`/`settings` are applied).
- Documented error paths raise the documented exception types (`ValueError`/`FileNotFoundError`/`AttributeError`) when driven through documented interfaces.

## Changes

- **Dependencies (`libs/mngr_robinhood/pyproject.toml`)**: add `claude-agent-sdk` as a normal dependency; add `pytest-asyncio` to a dev/test dependency group; set `asyncio_mode = "strict"`; raise/override the per-test timeout for this suite to a generous value so live calls don't trip the existing 30s default.
- **Marker registration & guards (`libs/mngr_robinhood/conftest.py`)**: register the `sdk_live` marker via `register_marker`; add a `collection_modifyitems` hook (and/or autouse fixture) that skips `sdk_live`-marked tests unless both `RUN_SDK_LIVE_TESTS=1` and `ANTHROPIC_API_KEY` are set; provide shared fixtures (a base `ClaudeAgentOptions` factory pinned to the `"haiku"` alias, a `tmp_path`-based cwd helper, and any small assertion helpers).
- **CI exclusion (`offload-modal.toml` and its flaky variant entry)**: append `and not sdk_live` to the pytest filter expressions so per-PR offload never collects the suite; confirm acceptance/release filters don't select it.
- **Test files (new `libs/mngr_robinhood/imbue/mngr_robinhood/test_sdk_*.py`)**, each setting `pytestmark = [pytest.mark.sdk_live, pytest.mark.asyncio]` and importing only documented names from `claude_agent_sdk`:
  - `test_sdk_query.py` — `query()` with string and streaming-input prompts; observable `ClaudeAgentOptions` subset.
  - `test_sdk_client.py` — `ClaudeSDKClient` lifecycle, multi-turn, context manager, disconnect, `set_model`, `set_permission_mode`.
  - `test_sdk_interrupt.py` — `interrupt()` mid-stream, marked `@pytest.mark.flaky`.
  - `test_sdk_permissions_and_hooks.py` — `can_use_tool` allow + deny; one `PreToolUse` hook (runs in `tmp_path`).
  - `test_sdk_types.py` — message-union and content-block type-shape assertions.
  - `test_sdk_sessions.py` — `list_sessions`/`get_session_info`/`get_session_messages`/`rename_session`/`tag_session` create-then-read round-trip in `tmp_path`.
  - `test_sdk_errors.py` — documented `ValueError`/`FileNotFoundError`/`AttributeError` paths.
- **Convenience & docs**: add a `just test-sdk-live` recipe (exports `RUN_SDK_LIVE_TESTS=1`, runs `-m sdk_live`); add a "Running the live SDK tests" section to `libs/mngr_robinhood/README.md`.
- **Changelog**: add `libs/mngr_robinhood/changelog/mngr-claude-sdk-tests.md` (and a `dev/changelog/mngr-claude-sdk-tests.md` if the offload filter / justfile edits count as a `dev` touch) describing the new opt-in suite.
- **Mismatch reporting**: collect every doc/implementation discrepancy discovered while writing the tests (e.g. undocumented SDK exception classes, renamed/missing `ClaudeAgentOptions` fields, type-shape differences) and report them in the final write-up rather than silently adapting the tests.
