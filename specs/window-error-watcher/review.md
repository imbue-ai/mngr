# Window Error Watcher — Review

## Summary

- The implementation is a clean, well-factored realization of the spec: a new
  FCT lib (`libs/error_watcher/`) mirroring `app_watcher`, with a pure,
  fully-unit-tested core and a thin I/O shell driven through an injected
  `CommandRunner`. All 47 lib tests pass; the structure, service registration,
  changelog, and README all match the plan. Every `REQ-*` has corresponding
  code.
- **Top thing to decide before merge:** the dedup credit is consumed *before*
  the alert is confirmed delivered (`new_matches` marks lines `seen` at
  `watcher.py:316`, the send happens at `:321`). If no agent is messageable
  (Scenario 4), `mngr list` fails, or `mngr message` returns non-zero, the
  matched lines are already recorded as "reported" and are never re-alerted —
  the error is silently dropped within the process. This is an edge-triggered
  but genuine gap against REQ-MATCH-3's wording ("already reported").
- **Nothing is a hard blocker.** Most findings are edge cases, v1-naivety
  consequences the spec partly anticipates, or test-coverage gaps. The
  implementation is correct on the happy path and well within "ship for v1"
  quality — but the lost-alert ordering (#1), the no-retry single pick (#2),
  and the volatile-line re-alert storm (#4) are worth a conscious decision.

## Requirements Coverage

| Requirement | Status | Evidence |
|-------------|--------|----------|
| REQ-SPAWN-1 (new lib, console-script, `[services.error-watcher]`) | Covered | `libs/error_watcher/pyproject.toml:11-12`, `services.toml:21-27`, root `pyproject.toml` members/sources/deps |
| REQ-SPAWN-2 (long-lived 5s loop, SIGTERM/SIGINT) | Covered (caveat) | `watcher.py:324-340`; `POLL_INTERVAL_SECONDS=5`. See finding #10: real stop path is SIGHUP, not the installed handlers |
| REQ-SPAWN-3 (discover session via `display-message`, no pidfile) | Covered | `watcher.py:221-229` (`get_session_name`) |
| REQ-SPAWN-4 (single-window failure must not crash loop) | Covered (test gap) | `_default_command_runner` never raises (`watcher.py:202-218`); failures logged. The failure branches are untested — finding #8 |
| REQ-SCAN-1 (enumerate windows + capture each) | Covered | `watcher.py:232-257`, `run_one_poll:310-313` |
| REQ-SCAN-2 (exclude own window) | Covered | `watcher.py:311` (`OWN_WINDOW`), test `test_run_one_poll_ignores_errors_in_its_own_window` |
| REQ-SCAN-3 (tolerate window create/destroy) | Covered | `capture_window` returns `""` on non-zero (`watcher.py:243-257`), test `test_run_one_poll_tolerates_a_window_capture_failure` |
| REQ-SCAN-4 (rendered pane only) | Covered | `capture-pane -p` (`watcher.py:249`) |
| REQ-MATCH-1 (flag `error\|exception`) | Covered | `DEFAULT_ERROR_PATTERN` (`watcher.py:26-28`), `match_lines` |
| REQ-MATCH-2 (case-insensitive) | Covered | `re.IGNORECASE`, test `test_match_lines_is_case_insensitive` |
| REQ-MATCH-3 (alert only new, per-window) | Covered (caveat) | `new_matches` (`watcher.py:78-95`). Defect: marks `seen` before delivery — finding #1 |
| REQ-MATCH-4 (single-place pattern, env-overridable) | Covered (test gap) | `compile_error_pattern` (`watcher.py:187-199`), `ERROR_WATCHER_PATTERN` read at `:328`. Wiring untested — finding #8 |
| REQ-NOTIFY-1 (`mngr list --format json` → `mngr message` random) | Covered | `build_list_command`, `build_message_command`, `_alert_random_agent` (`watcher.py:260-290`) |
| REQ-NOTIFY-2 (identify session+window, include lines) | Covered | `format_alert` (`watcher.py:104-118`), tests |
| REQ-NOTIFY-3 (messageable = not STOPPED, no auto-start, source eligible) | Covered (caveat) | `select_messageable_names` (`watcher.py:177-184`); argv omits `--start` (defaults `False`, verified in `vendor/mngr/.../message.py:67`). Under-filters other non-deliverable states — finding #3 |
| REQ-NOTIFY-4 (no messageable agent → log + skip, no error) | Covered (caveat) | `_alert_random_agent:278-282`, test `test_run_one_poll_skips_when_no_messageable_agent`. Interacts with finding #1 |
| REQ-NOTIFY-5 (uniform random) | Covered (caveat) | `choose_recipient` (`watcher.py:170-174`). No retry on send failure — finding #2 |
| REQ-NOTIFY-6 (batch multiple windows into one message) | Covered | `format_alert`, test `test_run_one_poll_batches_multiple_windows_into_one_message` |

## User Scenarios

- **Scenario 1 (a service prints a traceback):** Delivered. `run_one_poll`
  captures the window, matches, and sends one message; covered by
  `test_run_one_poll_sends_one_alert_for_a_new_error` (asserts the recipient,
  the single send, and the window/line content).
- **Scenario 2 (same error still on screen):** Delivered. `new_matches`
  suppresses already-seen lines; covered by
  `test_run_one_poll_does_not_realert_on_a_static_error`.
- **Scenario 3 (multiple windows error in one poll):** Delivered. A single
  batched message names both windows; covered by
  `test_run_one_poll_batches_multiple_windows_into_one_message`.
- **Scenario 4 (no agents available):** Delivered on the surface — it logs and
  continues without error (`test_run_one_poll_skips_when_no_messageable_agent`).
  But the matched lines were already marked `seen`, so when an agent later
  becomes messageable the still-on-screen error is *not* re-alerted (finding
  #1). No test covers the "agent returns, error re-alerts" follow-up.
- **Scenario 5 (watcher is stopped):** Outcome delivered (the process exits),
  but via the default SIGHUP action, not the installed SIGTERM/SIGINT handlers,
  because bootstrap stops services with `tmux kill-window` (finding #10). Not
  tested (live-tmux is intentionally manual per the plan).
- **Scenario 6 (watcher would match its own output):** Delivered. `OWN_WINDOW`
  is excluded; covered by `test_run_one_poll_ignores_errors_in_its_own_window`.

## Test Coverage

- **Tests added:** `libs/error_watcher/src/error_watcher/watcher_test.py` (33
  unit/integration tests covering the pure core and seven `run_one_poll`
  integration scenarios with an injected `_FakeCommandRunner`) and
  `libs/error_watcher/test_error_watcher_ratchets.py` (14 ratchet checks). The
  `mngr` argv builders are validated against the live mngr CLI tree via
  `assert_mngr_argv_valid` — a strong contract test that fails at merge time on
  an upstream rename.
- **Test suite status:** `cd .external_worktrees/forever-claude-template && uv
  run pytest libs/error_watcher --no-cov --cov-fail-under=0` → **47 passed, 0
  failed** (re-run during this review).
- **Broader suite:** the diff only touches `libs/error_watcher/` plus root
  config (`services.toml`, `pyproject.toml`, `uv.lock`); no other project's
  code changed. The full FCT suite was not re-run here (CI covers it). The
  seed's note that `libs/bootstrap` has one pre-existing macOS-only failure
  (`test_detect_snapshot_settings_falls_back_to_direct_when_no_btrfs`, absent
  `findmnt`) is unrelated to this change — `bootstrap` is untouched.
- **End-to-end:** live-tmux E2E is intentionally manual per the FCT CLAUDE.md
  ("Verifying interactive components with tmux"); the `run_one_poll`
  integration tests are the automated E2E layer. tmux is not installed in this
  macOS workspace, so the live scan could not be exercised here.
- **Gaps (finding #8):** `main()`, `_handle_signal`, the `ERROR_WATCHER_PATTERN`
  env wiring, the `_alert_random_agent` send-failure / `mngr list`-failure
  branches, and `_default_command_runner` (the only impure function, on which
  the whole no-crash contract rests) have no test. Nothing is skipped, xfail,
  or pending.

## Code Review Findings

No repo-specific code-review skill is configured (`.sculptor/docs.md` does not
exist in this repo). The review was run via the repo's documented convention
(`/code-review` per the monorepo CLAUDE.md) at `high` effort, with multiple
independent finder agents over the FCT clone diff. Findings below are ranked
most-severe first. None is a hard blocker; severities are the reviewer's
judgement for a v1 service.

1. **[Correctness — Medium-High] Dedup credit is consumed before the alert is
   confirmed delivered.** _[RESOLVED — FCT commit `6735584e`]_ `watcher.py:316` — `new_matches` adds the matched
   lines to `seen[window]` during the scan loop, then `_alert_random_agent`
   runs at `:321`. If enumeration returns no messageable agent (Scenario 4),
   `mngr list` fails, or `mngr message` returns non-zero (logged, not raised at
   `:284-287`), the lines are already recorded as "reported" and are
   permanently suppressed on every later poll — even once an agent becomes
   messageable. Violates the intent of REQ-MATCH-3 ("output it has already
   reported"): the output was marked reported but never reported. Fix shape:
   record `seen` only after a successful send, or separate "matched" from
   "alerted".

2. **[Correctness — Medium] Single random recipient with no fallback on send
   failure.** _[RESOLVED — FCT commit `dd2b89d3`]_ `watcher.py:277,283` — `choose_recipient` picks exactly one agent;
   if `mngr message` to it fails, the failure is logged and the poll returns
   with no retry against the rest of the messageable pool. Combined with #1, one
   bad pick (e.g. the agent stopped between `list` and `message`) drops the
   alert entirely despite other reachable agents.

3. **[Correctness — Medium] `select_messageable_names` excludes only
   `STOPPED`.** _[RESOLVED — FCT commit `0a67eb66`; added `type == "claude"` filter, kept STOPPED as the sole excluded state per mngr's actual send-path rule]_ `watcher.py:184` — `AgentLifecycleState`
   (`vendor/mngr/.../primitives.py:266`) also has `DONE`, `REPLACED`, `UNKNOWN`,
   `RUNNING_UNKNOWN_AGENT_TYPE`. `mngr message` resolves with `target_state=None`
   and `is_start_desired=False` (`message.py:146,175`), so it *attempts*
   delivery to such an agent and fails when its session is dead — a wasted pick
   (compounds #1/#2). Also diverges from the cited reference
   `list_claude_agent_names` (`claude_auth.py:400`), which additionally filters
   `type == "claude"`. The spec says "STOPPED ... per mngr's own rule", so this
   matches the spec's literal wording but not mngr's real deliverability.

4. **[Correctness — Medium] Exact-line dedup re-alerts every poll for volatile
   error lines.** _[RESOLVED — FCT commit `507e214a`; dedup now keys on a digit-normalized line]_ `watcher.py:91` — dedup keys on the exact line string, so an
   error line carrying a timestamp / counter / request-id (`[12:00:05] ERROR
   ...` then `[12:00:10] ERROR ...`) is a "new" match every 5 s and fires a
   fresh alert to a random agent. The stated non-goals only excuse benign
   *false positives* ("0 errors", "ErrorBoundary"), not an alert storm from
   volatile-but-real error lines.

5. **[Resource — Low-Medium] `seen` grows without bound.** _[RESOLVED — FCT commit `36740cda`; prune closed windows + per-window key cap]_ `watcher.py:79,93` —
   the per-window sets only ever grow and window keys are never evicted, in a
   permanent process. Worsened by #4 (high-cardinality lines). A bounded /
   LRU structure, or hashing the matched block, would cap it.

6. **[Correctness — Low-Medium] `mngr list` non-zero return short-circuits
   before parsing, and `returncode=1` is an overloaded sentinel.**
   _[RESOLVED — FCT commit `f129139b`; parse list regardless of exit, distinct `RUNNER_FAILURE_RETURNCODE`, preserve timeout stdout]_
   `watcher.py:269` returns before `parse_agent_summaries`, so if `mngr list`
   ever exits non-zero while still emitting a valid `{"agents": [...]}` payload,
   no alert is sent. `_default_command_runner:213` also maps every
   `SubprocessError`/`OSError` (including a `capture-pane` timeout, whose partial
   stdout is discarded) to `returncode=1`, colliding with a real exit-1.

7. **[Robustness — Low] Window targeting by name is ambiguous on duplicate
   names.** _[NOT FIXED — deliberate. Bootstrap names each service window `svc-<name>` uniquely, so duplicate names cannot arise in practice; the index-based refactor adds complexity to a deliberately-simple script for a case that does not occur, and the review itself notes the name-based approach is consistent with bootstrap's own pattern.]_ `watcher.py:249` — `capture-pane -t {session}:{window}` resolves a
   duplicate name to one window, so the other is never scanned and the two
   share a `seen` key. Mirrors bootstrap's own name-based `list-windows`
   approach, so it's consistent with the existing codebase pattern;
   `#{window_index}` would be unambiguous.

8. **[Test gap — Medium] The I/O shell is untested.** _[RESOLVED — `_default_command_runner` (success / missing-binary / timeout / sentinel) and `_alert_random_agent`'s send-failure & list-failure branches are now covered by FCT commits `dd2b89d3` (#2) and `f129139b` (#6); `_handle_signal` is covered by FCT commit `840b5b9b`. `main()`'s `while True` loop is intentionally left to live tmux verification rather than unit-tested, to avoid refactoring it purely for testability.]_ `watcher.py:324` (`main`),
   `_handle_signal`, the `ERROR_WATCHER_PATTERN` wiring, `_alert_random_agent`'s
   send-failure / list-failure branches, and `_default_command_runner` have no
   coverage. REQ-SPAWN-4 (failed send logged, not crashing) and REQ-MATCH-4 (env
   wiring) are asserted nowhere; a regression there would pass CI.

9. **[Cleanup/Reuse — Low-Medium] `build_list_command` / `build_message_command`
   duplicate existing builders.** _[NOT FIXED — deliberate. `mngr_cli_contract` is a test-only validator that depends on `imbue-mngr`, and `error_watcher`/`telegram_bot` do not declare it as a runtime dependency (it is only importable via the dev workspace venv). Centralizing the builders there would pull all of mngr into those libs' runtime closures for four trivial argv lines, and the design intent is that this service stays a simple FCT-side script that does not couple to the mngr interface. The minor duplication is the better trade.]_ `watcher.py:121,126` are byte-for-byte copies
   of `_build_list_command` (`claude_auth.py:312`) and `_build_message_command`
   (`telegram_bot/bot.py:59`) — now three copies. The `mngr_cli_contract` lib
   (already a shared dep of all three) is a natural home for one shared builder.

10. **[Spec accuracy — Low] SIGTERM/SIGINT handlers aren't on the real stop
    path.** _[RESOLVED — FCT commit `840b5b9b` installs an explicit SIGHUP handler (the signal `tmux kill-window` actually delivers) alongside SIGTERM/SIGINT, so the watcher now exits via an installed handler on the real stop path. Note: the spec prose itself lives in the monorepo and was left unchanged per the constraint that only review.md is edited here.]_ `watcher.py:335` — bootstrap stops services via `tmux kill-window`
    (`manager.py:617`), which delivers SIGHUP (confirmed by
    `vendor/mngr/.../testing.py`), not SIGTERM. The process still exits cleanly
    via the default SIGHUP action, and this exactly mirrors `app_watcher` (which
    the spec says to mirror), so the outcome satisfies Scenario 5 — but the
    spec's description of the mechanism is inaccurate.

11. **[Consistency — Low] No `if __name__ == "__main__": main()` guard.** _[RESOLVED — FCT commit `840b5b9b`]_ Both
    siblings (`app_watcher/watcher.py`, `telegram_bot/bot.py`) have one; here
    `python -m error_watcher.watcher` is a silent no-op. The `uv run
    error-watcher` console script (the actual launch path) works fine.

12. **[Efficiency — Low] `get_session_name` re-shells every poll.** _[NOT FIXED — deliberate. The cost is one `tmux display-message` call per 5s poll (negligible), while hoisting the session would change `run_one_poll`'s signature and ripple through every integration test. Left as-is to keep the change minimal, per the guidance to not change more than necessary.]_
    `watcher.py:306` — the session name is constant for the process lifetime but
    is re-fetched via `tmux display-message` every 5 s. `main()` already hoists
    `pattern` / `rng` / `seen` out of the loop; the session could be hoisted the
    same way.

## Overall Assessment

Ready to merge for a v1 service, with a couple of conscious decisions. The code
is clean, idiomatic, faithful to the spec and the `app_watcher` pattern, and
well-tested on the happy path. The biggest real risk is the **lost-alert
ordering** (finding #1) plus its compounding factors (#2, #3): under transient
"no messageable agent" or send-failure conditions, an error can be marked as
reported without ever being reported, and never retried. That is edge-triggered
(the common case has a live, messageable main agent), so it is not a blocker,
but it is the one place the behavior diverges from the spec's intent without
being listed as a non-goal. The **volatile-line re-alert storm** (#4) is the
most likely day-one annoyance in practice. Everything else is low-severity
hardening, test-coverage, or cleanup. Recommended follow-up: fix #1 (record
`seen` only after a successful send) and decide whether #4 needs a coarser
dedup key; the rest can be tracked as v1.1 polish.

## Resolution (post-review fix pass)

All findings have been addressed in the FCT clone (branch `preston/error-checker`),
each in its own commit (see the per-finding annotations above):

- **Fixed:** #1 (`6735584e`), #2 (`dd2b89d3`), #3 (`0a67eb66`), #4 (`507e214a`),
  #5 (`36740cda`), #6 (`f129139b`), #8 / #10 / #11 (`840b5b9b`).
- **Deliberately not fixed** (rationale inline): #7 (duplicate window names cannot
  arise — bootstrap names service windows uniquely), #9 (centralizing the argv
  builders would couple this simple FCT script to the `imbue-mngr` runtime), #12
  (negligible per-poll cost; the fix would churn `run_one_poll`'s signature and
  every test). #8's only residual is `main()`'s `while True` loop, left to live
  tmux verification.

With #1–#6 fixed, the two highest-risk items the assessment flagged (lost-alert
ordering and the volatile-line re-alert storm) are resolved.
