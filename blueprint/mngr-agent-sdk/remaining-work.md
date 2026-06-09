# mngr-backed Agent SDK — remaining work / handoff

Status as of branch `mngr/agent-sdk` (PR #1969). This documents everything still to do: tests
skipped for the mngr target, functionality not yet implemented, the design approach + blockers
for each, and test/infra follow-ups. The core (`query`, `ClaudeSDKClient` lifecycle + multi-turn,
session functions, resume/continue, options, tools, type shapes) is implemented and verified live
against the real API for **both** the real `claude_agent_sdk` target and the mngr target.

## How the suite is wired (so the rest makes sense)

- `imbue.mngr_robinhood.agent_sdk` re-exports all SDK **types** verbatim from `claude_agent_sdk`
  and re-implements the behavioral entry points on mngr (`_agent_sdk/{driver,client,sessions,
  context,message_parser}.py`; shared helpers in `agent_runtime.py`).
- The live `test_sdk_*.py` suite is parametrized by the `sdk` fixture (conftest) over two targets:
  `real_sdk` (the real package) and `mngr_sdk` (our module). Tests reference `sdk.query` /
  `sdk.ClaudeSDKClient` / the session functions; types are still imported from `claude_agent_sdk`
  (identical objects).
- The mngr target runs claude interactively in tmux; the real target runs `claude --print`. The
  `_sdk_tmux_guard` fixture (via `sdk_cwd`) satisfies the tmux resource guard for both targets.
- `sdk_cwd` writes `~/.claude.json` (trust + onboarding + API-key approval) for the mngr target so
  the interactive agent boots non-interactively, and destroys SDK agents on teardown.

## Tests currently skipped for the mngr target

All of these run against `real_sdk` and skip only `mngr_sdk` (via the `requires_native_sdk`
fixture, except `total_cost_usd` which is the same mechanism). Unskipping each requires the
corresponding functionality below.

| Test(s) | File | Skipped because |
|---|---|---|
| `test_can_use_tool_*` (allow, deny, input-rewrite, deny/interrupt, context, consulted-once, multiple-tools, records-denial) | `test_sdk_permissions_and_hooks.py`, `test_sdk_permissions_hooks_advanced.py` | `can_use_tool` in-process callback not wired |
| `test_pre_tool_use_hook_*`, `test_post_tool_use_hook_*`, `test_pre_and_post_hooks_*`, `test_hook_matcher_*`, `test_user_prompt_submit_hook_*`, `test_two_hook_matchers_*`, `test_plan_mode_prevents_tool_execution`, `test_pre_tool_use_hook_can_deny_*` | `test_sdk_permissions_and_hooks.py`, `test_sdk_permissions_hooks_advanced.py` | `hooks` in-process callbacks not wired |
| `test_interrupt_ends_an_in_flight_turn` | `test_sdk_interrupt.py` | `interrupt()` not wired |
| `test_include_partial_messages_yields_stream_events`, `test_stream_event_has_documented_fields` | `test_sdk_client_advanced.py` | partial-message `StreamEvent` streaming not available |
| `test_get_server_info_returns_a_dict` | `test_sdk_client_advanced.py` | `get_server_info()` not surfaced |
| `test_fork_session_creates_new_session_id` | `test_sdk_sessions_advanced.py` | `fork_session` not implemented |
| `test_result_total_cost_is_positive` | `test_sdk_types_detail.py` | `total_cost_usd` absent from mngr's transcript |

Everything else in the suite runs against both targets. (Note: a full both-target
`just test-sdk-live` run has NOT been executed end-to-end — see "Test/infra follow-ups".)

## Unimplemented functionality (with approach + blockers)

### 1. `can_use_tool` permission callback + `hooks` (in-process Python callbacks) — biggest gap
- **Current**: the options are accepted by `ClaudeAgentOptions` (re-exported) but completely
  ignored by the mngr driver. `ClaudeSDKClient` has no callback wiring.
- **Why hard**: the real SDK consults these via claude's **control protocol** over the
  subprocess's stdio — the SDK runs `claude` in a control mode and answers permission/hook
  requests in-process. mngr drives claude **interactively in tmux** and only reads the native
  session-JSONL transcript; there is no bidirectional control channel to call back into Python
  mid-turn.
- **Possible approaches**:
  - (a) Run the mngr claude agent in the SDK's stdio control mode and bridge the control
    request/response stream back to the in-process callbacks. This is essentially re-hosting the
    real SDK transport inside mngr — large, and fights mngr's "agent = tmux process" model.
  - (b) Translate `hooks` into claude **settings-file hooks** (shell commands) that POST to a
    local HTTP server which invokes the Python callbacks; translate `can_use_tool` via
    `--permission-prompt-tool` pointing at a local MCP server that calls the callback. Works for
    the *mechanism* but is a lot of plumbing, and the local-server/MCP bridge must be reachable
    from the agent.
  - **Open question**: is wiring these worth it, or should the mngr SDK document them as
    permanently real-SDK-only? They are the bulk of the skipped tests (~19).

### 2. `interrupt()`
- **Current**: raises `AgentSdkNotImplementedError`.
- **Approach**: send an interrupt (Ctrl-C / signal) to the running agent's tmux window mid-turn
  without destroying it, then let the drain loop observe the turn end. The orchestrator already
  has SIGINT handling for the agent (`_DestroyOnSignal`), and mngr can send keys to a tmux pane.
- **Open question**: does mngr expose a clean "interrupt the running agent" API (send Ctrl-C /
  signal to window 0) that stops the turn but keeps the agent alive and WAITING? Need to confirm
  and then have `ClaudeSDKClient.interrupt()` call it (via `to_thread`). The test is `@flaky`
  (timing-sensitive: the turn must still be in flight).

### 3. `fork_session`
- **Current**: `driver.deliver_turn` raises `AgentSdkNotImplementedError` when `fork_session=True`.
- **Approach**: claude's `--fork-session` continues a resumed session under a NEW session id. The
  complication is that each mngr agent has its own per-agent claude config dir, so a fresh agent's
  `--resume`/`--fork-session` can't see the source agent's session file. mngr_claude already has
  **session-adoption / preservation** machinery (`_preserve_session_files`,
  `test_adopt_session.py`) that copies session JSONLs between config dirs — that is likely the
  path: adopt the source session into a new agent, then start it with `--fork-session`.
- **Open question**: confirm the adopt-session API can place a source session into a new agent's
  config dir, and that `--fork-session` then yields a distinct `session_id` (the test asserts
  `result.session_id != source`).

### 4. partial-message streaming (`include_partial_messages` → `StreamEvent`)
- **Current**: `include_partial_messages` is accepted but no `StreamEvent`s are emitted (the
  documented "we can't stream individual words" limitation).
- **Why hard**: mngr mirrors claude's **session JSONL** (message-level), not claude's
  `--output-format stream-json --include-partial-messages` token-level stream. The partial events
  simply aren't in the source mngr tails.
- **Approach (heavy)**: capture claude's live stream-json (run with `--include-partial-messages`
  and tee the stdout stream) instead of / in addition to the session-file mirror. This is a
  different capture mechanism and likely a substantial mngr_claude change. Probably stays
  unsupported.

### 5. `get_server_info()` (and the `tools` list in `system/init`)
- **Current**: `get_server_info()` raises `AgentSdkNotImplementedError`. The synthesized
  `system/init` message reports a **hardcoded** built-in tools list (`_DEFAULT_REPORTED_TOOLS`),
  not claude's actual negotiated commands/tools.
- **Why**: claude's server info (available slash commands, output style) and the real tool list
  come from claude's init/control response, which mngr doesn't capture. The native session JSONL
  `system` event may or may not carry the command list.
- **Approach / open question**: investigate whether claude's session JSONL has a `system` event
  with `tools`/`commands`/`output_style`; if so, parse it for both `system/init.data["tools"]` and
  `get_server_info`. Otherwise these stay real-SDK-only. (Today `test_system_init_data_lists_
  available_tools` passes only because `Bash` happens to be in the hardcoded list.)

### 6. `total_cost_usd`
- **Current**: `ResultMessage.total_cost_usd` is always `None`; the cost test skips for mngr.
- **Why**: cost is a stream-json `result`-event field, not present in the session JSONL.
- **Approach**: either capture the stream-json result event, or **compute** cost from the
  per-message `usage` token counts × a model pricing table (see the `claude-api` skill for current
  per-token prices). The computed value would be approximate but `> 0`, which is all the test
  asserts. Decide whether an approximate computed cost is acceptable or misleading.

### 7. `set_model` / `set_permission_mode` (mid-session)
- **Current**: accepted but no-op against the running agent (logged at debug). The tests pass
  because they only assert the session stays usable and the model is still haiku.
- **Why**: mngr can't reconfigure a running claude process's model/permission-mode live (the real
  SDK sends a control request).
- **Open question**: is the no-op acceptable, or should `set_model` restart the agent with the new
  model while preserving the session via resume? Restart-on-set_model is heavier and changes
  timing; the no-op is honest about the limitation. Currently it does not even persist the new
  value for subsequently-created agents (there are none within one client).

### 8. `get_mcp_status()`
- **Current**: returns `{"mcpServers": []}` (correct for the no-MCP-server case; the SDK does not
  configure MCP servers here). If MCP servers are ever supported, this needs real status.

## Known fidelity gaps (not currently asserted, but real)

- **`model_usage` shape**: `_finalize_turn_messages` maps `model -> raw per-message usage block`
  rather than an aggregated `ModelUsage` summary. The keyed-by-model test passes, but the value
  shape is approximate. (Flagged NITPICK by autofix.)
- **`duration_api_ms`**: set equal to wall-clock `duration_ms` (mngr can't separate API time).
- **`system/init.data["tools"]`**: hardcoded list, not claude's real tool set (see #5).
- **Hermeticity**: the mngr agent is NOT hermetic — it loads the host's claude config (it cannot
  pass `--setting-sources=` because that makes the interactive agent hang on the startup trust
  dialog, since mngr writes its unattended bypass into the project/local settings sources). The
  real SDK uses `setting_sources=[]` and is hermetic. This is why "remember the **secret** word"
  prompts were declined as injection (now reworded to "important"); other host-config effects
  could still surface. **Open design question**: can mngr give the agent hermeticity (ignore host
  CLAUDE.md/settings/hooks) while still booting non-interactively? Would require writing the
  unattended bypass/trust into a source that survives `--setting-sources=`, or a different startup
  path.

## Test / infra follow-ups

- **Run the full both-target suite end-to-end.** Only representative subsets were run live:
  errors/errors_edge (both targets), `query` string-prompt (both targets), the two memory tests
  (both targets), and ~85 mngr-target tests across `query`/`client`/`client_advanced`/
  `query_misc`/`types`/`types_detail`/`options`/`tools`/`sessions`/`session_functions`. The full
  `RUN_SDK_LIVE_TESTS=1 just test-sdk-live` (≈148 tests × 2 targets) has NOT been run in one pass.
  In particular most **real_sdk** variants have not been exercised in this environment (only
  `query` was confirmed); confirm the real target's claude `--print` path works for the whole
  suite under the pytest temp-HOME (it should, since `--print` skips the trust/API-key dialogs).
- **Cost of dual-target runs**: `just test-sdk-live` now makes ~2× the API calls (both targets).
  Consider documenting `-k mngr_sdk` / `-k real_sdk` to run a single target, and pinning the
  cheapest model (already `haiku`).
- **README**: `libs/mngr_robinhood/README.md` still describes the original single-target suite;
  update the "Running the live SDK tests" section to mention the `sdk` fixture / dual-target shape
  and that the mngr target needs a local `claude` + tmux.
- **`test_independent_queries_do_not_share_memory`** (`test_sdk_query_misc.py`) still uses a
  "secret animal" prompt. It passes (it asserts the secret is NOT recalled), but for consistency
  with the reworded memory tests it could be changed to a non-"secret" framing.
- **Coverage**: `libs/mngr_robinhood` per-package gate is `fail_under = 65`; current offline
  coverage is ~68%. The driver/sessions live-orchestration paths (agent create/drive/resume) are
  only covered by the `sdk_live` suite (excluded from CI). If more live code is added, add more
  offline unit tests for any pure logic, or coverage will dip again.
- **Agent cleanup at scale**: `sdk_cwd` teardown calls `destroy_sessions_in_directory`. By design
  the SDK only *stops* agents on `disconnect` (leaving the session readable); in non-test use,
  stopped SDK agents accumulate until `mngr cleanup`/`gc`. Confirm this is acceptable or add an
  opt-in reaper / idle-timeout at create time.

## Out-of-scope-for-v1 (designed-for-later)

- **Remote hosts**: `LOCAL_PROVIDER_NAME` is hardcoded in the driver/sessions; only local agents
  are supported. Remote would need the provider threaded through.
- **Non-claude agent types**: the message parser reads claude's session JSONL and the types are
  `claude_agent_sdk`'s. Supporting codex/opencode/etc. would need per-agent-type transcript
  parsing and degraded fidelity for non-claude.

## Minor / cosmetic

- One commit message (`95a963c17`) lost a backtick-quoted token (`` `_sdk_tmux_guard` ``) because
  the literal had backticks inside a double-quoted `-m` string; the code is correct. Not amended
  (repo policy: never amend).
