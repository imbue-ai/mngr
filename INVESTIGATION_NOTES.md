# Investigation: failing tests on `mngr/fix-data-parsing` (base `josh/fix_parsing`)

Diff under investigation:
- `0290a9f04 Making fixes and leaving fixmes for handling invalid data` (Josh's most-recent commit)
- merge `5aa9d9fcb` of `origin/mngr/discovery-schema-changed`
- merge `6b42f6143` of `origin/mngr/corrupt-json-midfile`

The two underlying branches each tightened a different invalid-data path; the merges plus Josh's commit on top shifted several callers from "log + return None" to "raise". The tests have not yet been updated to match. There are also a couple of real merge-conflict bugs and one pre-existing main breakage in our way.

## Summary of failing tests by package

| Package | Test | Category |
|---|---|---|
| libs/mngr | discovery_events_test.py::test_parse_invalid_json_returns_none | bug (merge dropped json.JSONDecodeError handler) |
| libs/mngr | discovery_events_test.py::test_parse_unknown_event_type_returns_none | bug (merge dropped unknown-event-type guard) |
| libs/mngr | discovery_events_test.py::test_resolve_provider_names_recovers_after_schema_mismatch | bug + warning leakage |
| libs/mngr | discovery_events_test.py::test_resolve_provider_names_returns_none_for_unknown_identifier | bug (commit `0290a9f04` dropped `return None`) |
| libs/mngr | discovery_events_test.py::test_resolve_provider_names_returns_none_when_any_identifier_missing | same bug |
| libs/mngr | discovery_events_test.py::test_resolve_provider_names_respects_destroy_events_by_id | same bug |
| libs/mngr | discovery_events_test.py::test_resolve_provider_names_respects_destroy_events_by_name | same bug |
| libs/mngr | events_test.py::test_parse_event_line_missing_timestamp_returns_none | test needs update (intentional API change) |
| libs/mngr | events_test.py::test_parse_event_line_malformed_json_returns_none | test needs update |
| libs/mngr | events_test.py::test_parse_event_line_empty_string_returns_none | test needs update + arguably a regression for the watcher caller |
| libs/mngr | events_test.py::test_parse_event_line_whitespace_only_returns_none | same |
| libs/mngr | events_test.py::test_parse_event_line_non_dict_json_returns_none | test needs update |
| libs/mngr | jsonl_warn_test.py::test_parse_returns_none_for_non_dict_json | test needs update |
| libs/mngr | observe_test.py::test_agent_observer_on_discovery_stream_output_ignores_invalid_json | bug (caller assumed parser returns None) |
| libs/mngr | utils/test_ratchets.py::test_no_type_errors | pre-existing on main (unrelated) |
| libs/mngr | cli/common_opts_test.py::test_apply_config_defaults_warns_on_unknown_param_when_lax | pre-existing warning-leakage (unrelated) |
| libs/mngr | cli/common_opts_test.py::test_setup_command_context_warns_on_unknown_command_param_when_lax | same |
| libs/mngr | utils/git_utils_test.py::test_delete_git_branch_returns_false_for_missing_branch | same |
| libs/mngr | utils/git_utils_test.py::test_delete_git_branch_returns_false_for_non_git_dir | same |
| libs/mngr_notifications | watcher_test.py::test_process_events_malformed_json_ignored | regression: watcher loop now crashes on bad lines |
| libs/mngr_notifications | test_ratchets.py::test_no_type_errors | downstream of libs/mngr type error |
| libs/mngr_lima | test_ratchets.py::test_no_type_errors | same |
| libs/mngr_pi_coding | test_ratchets.py::test_no_type_errors | same |
| libs/mngr_vps_docker | test_ratchets.py::test_no_type_errors | same |
| libs/mngr_vps_docker | host_store_test.py::test_parse_batched_json_files_skips_invalid | test needs update (commit dropped the JSONDecodeError catch) |
| libs/mngr_tmr | test_ratchets.py::test_prevent_silent_decode_error_catches | regression: commit demoted `warning` -> `debug` in pulling.py |
| libs/mngr_tmr | test_ratchets.py::test_no_type_errors | downstream of libs/mngr type error |
| apps/minds | config/data_types_test.py::test_parse_agents_from_mngr_output_handles_non_json | test needs update (intentional API change) |
| apps/minds | config/data_types_test.py::test_parse_agents_from_mngr_output_handles_mixed_output | same |
| apps/minds | config/data_types_test.py::test_parse_agents_from_mngr_output_skips_invalid_json_lines | same |
| apps/minds | desktop_client/backend_resolver_test.py::test_stream_manager_on_discovery_stream_output_ignores_unrecognized_events | bug (parser raises instead of returning None) |
| apps/minds | desktop_client/backend_resolver_test.py::test_stream_manager_handle_discovery_line_ignores_invalid_json | same |
| apps/minds_workspace_server | test_ratchets.py::test_prevent_silent_decode_error_catches | snapshot off-by-one (session_parser.py:92) -- predates this branch |
| apps/minds_workspace_server | test_ratchets.py::test_no_type_errors | downstream of libs/mngr type error |
| apps/minds_workspace_server | agent_manager_test.py::test_handle_observe_output_line_invalid_json_is_ignored | bug (caller assumes parser returns None on malformed JSON) |

Total: 30 distinct failing assertions plus 4 pre-existing teardown errors.

## Root causes

### 1. Merge conflict resolution dropped two early-returns in `parse_discovery_event_line` (real bug)

`libs/mngr/imbue/mngr/api/discovery_events.py:469`. Both parents agreed on early-returning `None` for malformed JSON; the `mngr/discovery-schema-changed` parent also early-returned `None` for unknown event types. Both lines disappeared in the merge (`5aa9d9fcb`). The docstring (which still says "Returns None for empty lines, malformed JSON, or unrecognized event types") was not updated, so this is a textbook merge mistake, not a deliberate change.

What it should look like (combining both parents' intent):

```python
stripped = line.strip()
if not stripped:
    return None
try:
    data = json.loads(stripped)
except json.JSONDecodeError:
    return None
event_type = data.get("type")
if event_type not in DiscoveryEventType:
    return None
try:
    return _DISCOVERY_EVENT_ADAPTER.validate_python(data)
except ValidationError as e:
    raise DiscoverySchemaChangedError(str(event_type), str(e)) from e
```

This single fix unblocks at least 5 tests:
- `discovery_events_test.py::test_parse_invalid_json_returns_none`
- `discovery_events_test.py::test_parse_unknown_event_type_returns_none`
- `observe_test.py::test_agent_observer_on_discovery_stream_output_ignores_invalid_json`
- `apps/minds/.../backend_resolver_test.py::test_stream_manager_on_discovery_stream_output_ignores_unrecognized_events`
- `apps/minds/.../backend_resolver_test.py::test_stream_manager_handle_discovery_line_ignores_invalid_json`

### 2. `0290a9f04` dropped `return None` from `resolve_provider_names_for_identifiers` (real bug)

`libs/mngr/imbue/mngr/api/discovery_events.py:622`. The diff replaced

```python
else:
    # Unknown identifier -- fall back to full scan
    return None
```

with

```python
else:
    # Unknown identifier -- fall back to full scan
    logger.debug(...)
```

The trailing `return None` was lost. Now the loop falls through and returns `tuple(sorted(resolved_providers))`, which is an empty tuple when none of the identifiers resolve, or a partial tuple when some do. The caller's contract ("None means do a full scan") is broken; partial results silently propagate.

Tests that depend on the contract:
- `test_resolve_provider_names_returns_none_for_unknown_identifier`
- `test_resolve_provider_names_returns_none_when_any_identifier_missing`
- `test_resolve_provider_names_respects_destroy_events_by_id`
- `test_resolve_provider_names_respects_destroy_events_by_name`
- `test_resolve_provider_names_recovers_after_schema_mismatch` (also fails because of #5 below)

Fix: restore `return None` after the debug log.

### 3. `parse_event_line` now raises on inputs that callers feed it routinely (regression)

`libs/mngr/imbue/mngr/api/events.py:381`. Pre-commit, the function returned `None` for empty / whitespace-only / malformed-JSON / non-dict / missing-timestamp inputs. Post-commit it raises directly:

```python
def parse_event_line(line: str, source_hint: str) -> EventRecord:
    stripped = line.strip()
    data = json.loads(stripped)              # raises on "" and on bad JSON
    if not isinstance(data, dict):
        raise Exception(...)
    return _record_from_event_data(data, ...)  # raises on missing timestamp
```

The notifications watcher calls this in a `for line in content.splitlines()` loop; `splitlines()` doesn't yield empty lines, but the watcher's `test_process_events_malformed_json_ignored` still needs the function to tolerate non-JSON input (or the loop has to catch). Right now the loop has neither.

Tests affected: 5 in `libs/mngr/imbue/mngr/api/events_test.py` plus `mngr_notifications/watcher_test.py::test_process_events_malformed_json_ignored`.

What's needed (one of):
- Update tests (and the `MalformedJsonLineWarner` parse() doc) to assert the new "raise on bad input" contract, AND update the `_process_events` watcher to wrap the call (or pre-filter via `MalformedJsonLineWarner`).
- Or: keep the old `return None` contract for `parse_event_line` but require callers to use the explicit `MalformedJsonLineWarner.parse()` first when they're streaming JSONL files.

Note that several call sites in `events.py` (e.g. lines 640, 999, 1059) still have `if record is None: continue` against the result of `_record_from_event_data` (which can no longer return None). Those guards are dead code now -- harmless but should be cleaned up.

### 4. `MalformedJsonLineWarner.parse` raises on non-dict (regression for tests, also worth a design discussion)

`libs/mngr/imbue/mngr/utils/jsonl_warn.py:47`. Pre-commit returned `None` for non-dict; now raises a bare `Exception(...)`. Two issues:

- The class docstring describes parse() as the polite "buffer-and-warn" path: "A malformed line is silently buffered. The next non-empty line proves the buffered line was not a partial write at end-of-file, so a warning is emitted at that point." Raising on `[1,2,3]` breaks that contract for callers that legitimately tail subprocess output (the discovery stream is the main one).
- It uses `raise Exception(format_string_with_braces, value)` which is not the same as `raise Exception(format_string.format(value))` -- this was probably copy/pasted from a `logger.warning` call site that uses loguru-style positional formatting. The result is `Exception` whose `str()` is the literal tuple, which is fine but ugly.

Test affected: `libs/mngr/imbue/mngr/utils/jsonl_warn_test.py::test_parse_returns_none_for_non_dict_json`.

### 5. The `test_resolve_provider_names_recovers_after_schema_mismatch` ERROR is a test-infra warning leak

In addition to failing on the bug from #2, this test triggers `_loguru_logger.warning("Discovery event schema mismatch; ...")` which the new "error on unexpected warnings in tests" infra (PR #1463, recently merged in `f3bf8a0d8`) treats as a teardown error. The test needs `@pytest.mark.allow_warnings` (or the `with allow_warnings():` context manager).

This same infra is what produces the 4 pre-existing teardown ERRORs: `test_apply_config_defaults_warns_on_unknown_param_when_lax`, `test_setup_command_context_warns_on_unknown_command_param_when_lax`, `test_delete_git_branch_returns_false_for_missing_branch`, `test_delete_git_branch_returns_false_for_non_git_dir`. None of those are caused by Josh's parsing changes -- they're just exposed because the warnings infra is now stricter, and the tests that intentionally exercise warning-emitting paths haven't been annotated yet. This is unrelated to fix-data-parsing but blocks a clean test run.

### 6. `apps/minds/imbue/minds/config/data_types.py::parse_agents_from_mngr_output` now raises (intentional)

`0290a9f04` rewrote this to raise on non-JSON lines and to do `data["agents"]` rather than `data.get("agents", [])`. The 3 failing tests just need to be updated:
- `test_parse_agents_from_mngr_output_handles_non_json` should assert it raises (and on what type/message).
- `test_parse_agents_from_mngr_output_handles_mixed_output` documents an old "skip non-JSON prefix lines" behavior that is gone -- the test should either go (the upstream is supposed to be fixed) or assert it raises.
- `test_parse_agents_from_mngr_output_skips_invalid_json_lines` similarly should assert it raises.

Note: the new message uses loguru-style placeholders (`raise Exception("...: {}", stripped[:200])`), which produces an Exception whose str is `("...", '...')`. Functionally OK, but the test messages should match whatever we settle on.

### 7. `mngr_vps_docker` and `mngr_tmr` ratchet/test fallout (intentional changes need test updates)

- `libs/mngr_vps_docker/.../host_store_test.py::test_parse_batched_json_files_skips_invalid`: `0290a9f04` removed the `try/except` around `json.loads(content)`, so the corrupt file now bubbles. Test needs updating (or, again, the design needs revisiting -- see #9).
- `libs/mngr_tmr/.../test_ratchets.py::test_prevent_silent_decode_error_catches`: `0290a9f04` demoted `logger.warning("Could not read remote outcome file ...")` to `logger.debug` in `mngr_tmr/pulling.py:73`. The ratchet's strengthened rule says "any log below WARNING counts as silent." So either restore the `warning` call (preferred -- no real reason to demote it), or accept this as a real silent catch and re-raise at this site, or bump the ratchet snapshot (worst option).

### 8. `test_no_type_errors` failures everywhere are a pre-existing main breakage, NOT caused by this branch

`libs/mngr/imbue/mngr/cli/connect.py:395` and `:416` both call `build_agent_filter_cel(opts)` but that function now requires `cg: ConcurrencyGroup`. I confirmed by checking out `main` directly: the same two `error[missing-argument]` errors are present there too.

So this is unrelated tech debt that we'll need to fix to get a green run, but it's not in scope for "fix-data-parsing." Cheapest fix is to add the missing `mngr_ctx.concurrency_group` argument at both call sites.

### 9. `apps/minds_workspace_server::test_prevent_silent_decode_error_catches` is a snapshot off-by-one not caused by this branch's commit

The ratchet currently snaps to `5` but counts `6` violations (4 in `server.py`, 1 in `session_parser.py`, 1 in `session_watcher.py`). The 6th violation is in `session_parser.py`, which is a brand new file added by `3bc3ecc07 Rename claude_web_chat to minds_workspace_server`. None of those files were touched by this branch. Most likely the snapshot was set before that file was added (or the strengthened ratchet logic now flags it), and nobody noticed because the test wasn't being run together with the rename.

The cleanest fix is to either log-or-raise at `session_parser.py:92` (matches what the strengthened ratchet wants), or bump the snapshot to `6`. Logging is preferable because the file is parsing untrusted JSONL.

### 10. Workspace server `_handle_observe_output_line` raises "This should never happen" on malformed JSON (regression)

`apps/minds_workspace_server/imbue/minds_workspace_server/agent_manager.py:717` after `0290a9f04`:

```python
event = parse_discovery_event_line(stripped)
if event is None:
    raise Exception("This should never happen")
self._handle_discovery_event(event)
```

The intent is plausible -- if `parse_discovery_event_line` is supposed to either return a valid event or raise, then `None` here is a bug. But:
- Today (because of bug #1), `parse_discovery_event_line` raises on malformed JSON instead of returning None, so the test failure is caused by the json.JSONDecodeError, not by the "This should never happen" path.
- After fixing bug #1, the parser will return None for malformed JSON / unknown event types, and *then* this line will crash the workspace-server subprocess every time stderr noise leaks in. That's worse, not better.

This same shape exists in `apps/minds/.../backend_resolver.py:586`:

```python
elif event is None:
    raise Exception("Unrecognized discovery event line: {}", line[:200])
```

Both call sites need to be reconciled with whatever final contract `parse_discovery_event_line` has. The minimum-friction option is: parser returns None for genuinely-unparseable lines (empty / non-JSON / unknown event type), callers log a warning and skip, no exceptions. The two FIXMEs Josh left ("make the match exhaustive so that we have to think about what to do when there are new types") capture the right intuition for the "I got an event of a type I don't handle" case, which is different from "I got garbage": those should be split.

## Recommended fix sequencing (for the next pass, NOT this one)

1. Restore the two early-returns in `parse_discovery_event_line` -- pure merge fix, unblocks 5+ tests with zero design risk.
2. Restore `return None` in `resolve_provider_names_for_identifiers` -- pure copy-paste fix, unblocks 5 tests.
3. Pick a contract for `parse_event_line` / `MalformedJsonLineWarner.parse` and `parse_discovery_event_line` (return-None vs raise) and apply it consistently to (a) the function, (b) every caller, (c) every test. The style-guide policy ("never silently swallow; warn at minimum for streams/subprocess output") suggests: parser returns Optional, caller warn-logs and skips. That matches the pre-merge behavior of every JSONL reader in the codebase and is what the existing `MalformedJsonLineWarner` infra is built to support.
4. Replace the "raise Exception(...)" strings -- they should be typed `MngrError` subclasses per the style guide ("Never raise built-in Exceptions directly").
5. Resolve the discovery-event-type FIXMEs by making the dispatch in `_handle_discovery_event` exhaustive (a `match` over `DiscoveryEvent`), not by raising. This is the actual robustness win.
6. Clean up dead `if record is None: continue` checks in `events.py`.
7. Restore `logger.warning` (not `debug`) in `mngr_tmr/pulling.py`. The original was already correct.
8. Fix `connect.py` to pass `mngr_ctx.concurrency_group` to `build_agent_filter_cel` (pre-existing main bug).
9. Annotate the 4 pre-existing tests that emit warnings deliberately with `@pytest.mark.allow_warnings` (pre-existing main bug, but in our way for a green run).
10. Decide what to do about `apps/minds_workspace_server/.../session_parser.py:92` (log + skip is most consistent with the rest of the codebase).

## Open questions

- The two `# FIXME: I don't see how we can meaningfully proceed here. At the very least, we need to shuffle the on_error behavior down to here (maybe in mngr context?) so that we can know whether to warn or abort` comments in `docker/host_store.py:179` and `lima/host_store.py:176` are the right framing: the right knob is per-`MngrContext` (the user's --on-error setting). Worth wiring `ErrorBehavior` through `DockerHostStore.list_agents` and `LimaHostStore.list_agents` so corrupt agent json is either a warning or a hard failure based on the user's intent. Out of scope for this pass.
