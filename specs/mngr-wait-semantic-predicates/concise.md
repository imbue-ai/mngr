# mngr wait: semantic predicates (generalize end-of-turn detection)

## Overview

* Extend `mngr wait` (in `libs/mngr_wait/`) with semantic predicates beyond lifecycle states. Concretely: add a `--turn-boundary` predicate that blocks until a Claude agent finishes its current assistant turn (terminal `stop_reason`, no `tool_use` in the final content), and prints the assistant's reply to stdout.
* Motivation: `mngr_subagent_proxy` ships a private 694-line `subagent_wait.py` module that does exactly this. End-of-turn detection is a generic primitive that should live in mngr core, not in a plugin -- other plugins, automation pipelines, and user scripts all want it.
* Design constraint from user: do NOT couple this to `mngr message` (e.g. as `--wait`). End-of-turn is a *trigger condition*; many things may want to fire on the same trigger (capture reply, run webhook, kick next pipeline stage, collect telemetry, sync between agents). A standalone predicate fits these uses cleanly.
* Subagent-proxy's `subagent_wait.py` keeps existing for now as a thin wrapper that adds proxy-specific transforms (`[ERROR]` prefix on destroyed-mid-turn, `MNGR_SUBAGENT_RESULT_MAX_CHARS` truncation, `PERMISSION_REQUIRED` watermark dedup). Once stable, the wrapper can shrink further or be removed.

## Architecture fit with current `mngr_wait`

The existing wait loop (`libs/mngr_wait/imbue/mngr_wait/api.py:wait_for_state`) is already abstracted around two callbacks:

```
poll_fn: Callable[[], CombinedState]       # how to read current state
check_state_match(combined_state, ...)     # how to detect a match
```

The plumbing supports semantic predicates with one structural change: the state type and match predicate need to be parameterized. Right now both are hardcoded to `CombinedState` (host_state + agent_state). For semantic predicates we need a richer state (transcript-tail position, last seen end-of-turn event, last permission target).

Two implementation paths:

1. **Add a parallel `wait_for_event` function** alongside `wait_for_state`. Same poll-loop skeleton, different state type and match predicate. Reuse `resolve_wait_target` 100%, reuse timeout/interval mechanism, reuse state-change logging hook (repurposed for transcript-advancement events). Lower-risk, no breaking changes.
2. **Generalize the loop** with a `WaitStrategy[T]` interface (`poll() -> T`, `check_match(T) -> str | None`). Cleaner long-term but touches the existing public API.

Recommended: start with path (1). The duplication is modest (~50 lines) and the API surface stays narrow. Path (2) can come later if more predicates land.

## What "turn boundary" means concretely

Mirror the semantics in `subagent_wait.is_end_turn_event`:

* event must be `type: "assistant"`
* `message.stop_reason` must be in `{end_turn, stop_sequence, max_tokens}`
* `message.content` must NOT contain a `tool_use` block

The "wait" returns the concatenated `text` blocks from `message.content`. Optionally also returns the `stop_reason` and the full assistant message structure for callers that want more.

Source of truth: the agent's Claude transcript JSONL under
`<host_dir>/agents/<agent_id>/plugin/claude/anthropic/projects/<encoded_cwd>/<session_id>.jsonl`.
The session id can change mid-wait (resume / new session); `subagent_wait` already handles this -- copy the logic.

## CLI shape

```
mngr wait <target> --turn-boundary [--format=text|json] [--max-chars=N] [--timeout=DUR]
```

* `--turn-boundary`: select the predicate. Mutually exclusive with the existing positional state list.
* `--format=text` (default): print the assistant's reply text to stdout, exit 0.
* `--format=json`: print `{"stop_reason": "...", "text": "...", "elapsed_seconds": ...}`. Useful for scripts.
* `--max-chars=N`: truncate text output (no default cap -- generic CLI shouldn't inherit the proxy's 100KB cap; users who want it pass it explicitly).
* `--timeout` and `--interval` reuse the existing flags.

Exit codes mirror existing wait:
* 0: predicate matched
* 1: error
* 2: timeout

Edge cases to expose as different exit codes or stderr signals:
* Agent destroyed mid-wait. Currently `subagent_wait` returns an `[ERROR]`-prefixed body; that's a textual hack for Haiku echoing. Generic CLI should exit non-zero with a clear stderr message.
* Permission dialog raised. Either expose as a separate predicate (`--permission-dialog`) or surface as a non-fatal stderr warning. Recommend adding `--permission-dialog` as a parallel predicate so callers can compose: `mngr wait X --turn-boundary --or --permission-dialog` (future flag).

## Implementation steps

1. **Factor a transcript-tailer module** in `mngr_wait` (or `mngr_claude` -- it's Claude-specific, but `mngr_wait` already depends on `mngr_claude` is unclear; check). The module exposes: `tail_transcript(agent_id, host_dir, session_id) -> Iterator[dict]` that yields new JSONL events and handles session-id resets. Source: the relevant ~200 lines of `subagent_wait.py` (the `TailState`, `read_new_jsonl_lines`, `_refresh_tail_path` parts).
2. **Add `wait_for_turn_boundary`** to `libs/mngr_wait/imbue/mngr_wait/api.py`. Same poll-loop shape as `wait_for_state` but with a tail-state poll_fn and an end-of-turn match predicate. Returns a result with the assistant's text and stop_reason.
3. **Wire CLI flag** in `libs/mngr_wait/imbue/mngr_wait/cli.py`. Add `--turn-boundary`, `--format`, `--max-chars`. Validate that `--turn-boundary` is incompatible with the positional state list.
4. **Tests**:
   - Unit tests in `mngr_wait`: end-of-turn detection on canned JSONL fixtures, session-id rollover, no-tool-use guard, truncation.
   - Integration test that drives a real Claude agent via `mngr_claude` test fixtures and waits for its first end-of-turn.
   - Ratchet check that `subagent_wait.py` isn't growing in ways that should now be in core.
5. **Migrate `subagent_wait.py`** to delegate to the new `mngr wait` core. Keep the proxy-specific transforms (truncation default, `[ERROR]` prefix, watermark dedup) in the wrapper. Update the `mngr-subagents` skill to teach `uv run mngr wait <slug> --turn-boundary` instead of `python -m imbue.mngr_subagent_proxy.subagent_wait <slug>`.
6. **Documentation**: update `mngr_wait`'s README with a "Semantic predicates" section. Document the predicate space and how to add new ones (transcript-tail polling pattern + match predicate).

## Open questions

* **Where does the transcript-tailer live?** `mngr_wait` is generic over agent types; the tailer is Claude-specific. Options: (a) put it in `mngr_claude` and have `mngr_wait` import from it (architectural inversion -- `mngr_wait` may already not depend on `mngr_claude`); (b) put it in `mngr_wait` and call out the Claude-specific assumption; (c) introduce a small `mngr_wait_claude` module in `mngr_wait` that bridges. Lean toward (b) initially, with a note that other agent types could plug in later.
* **Should `--permission-dialog` ship in v1?** Useful for the `subagent_wait.py` migration (it currently surfaces this), but the OR-composition story isn't designed yet. Could ship as a separate predicate (one wait stops on EITHER predicate) or punt to v2.
* **Should `mngr message --wait` exist as a convenience?** No, per user direction. Composition (`mngr message X foo && mngr wait X --turn-boundary`) is the expected pattern.
* **Multi-agent semantics**: if the user does `mngr wait <name> --turn-boundary` and multiple agents match the name, what happens? Existing `mngr wait` raises `UserInputError` ("multiple matches; disambiguate"). Keep the same behavior.
* **Idempotency / re-entry**: if Claude already finished its turn before `mngr wait` started, does it match immediately or wait for the *next* end-of-turn? `subagent_wait.py` uses a watermark file to track position across re-runs (Haiku-retry case). For the generic CLI, the cleanest semantics is "match the first end-of-turn observed AFTER the wait starts". Document this; users who want "did the last turn end" should poll the transcript directly.

## Out of scope for this spec

* Combining predicates (`--turn-boundary AND --permission-dialog`): future v2.
* Non-Claude agent types (codex, etc.): the predicate vocabulary differs.
* Replacing `subagent_wait.py` entirely: the wrapper stays for the proxy plugin's specific needs.
* Deprecating `python -m imbue.mngr_subagent_proxy.subagent_wait`: keep working for back-compat; update the skill to prefer the new CLI.

## Files to read first (for the implementer)

* `libs/mngr_wait/imbue/mngr_wait/api.py` -- existing wait loop. Note the `poll_fn` + `check_state_match` structure; that's the shape the new predicate will reuse.
* `libs/mngr_wait/imbue/mngr_wait/data_types.py` -- existing `CombinedState` / `WaitResult`. The new predicate needs a parallel state type.
* `libs/mngr_wait/imbue/mngr_wait/cli.py` -- existing CLI flags.
* `libs/mngr_subagent_proxy/imbue/mngr_subagent_proxy/subagent_wait.py` -- existing transcript-tailer with end-of-turn detection. ~200 lines of this are the parts to lift into core; the rest is proxy-specific (truncation default, `[ERROR]` prefix, watermark dedup, heartbeat) and stays in the plugin wrapper.
* `libs/mngr_claude/` -- check whether to put the Claude-specific tailer here vs. in `mngr_wait`. Look at existing dependencies between the two libs.
