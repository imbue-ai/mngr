# Divergences: mngr-backed Agent SDK vs. the real `claude_agent_sdk`

Catalog of places where `imbue.mngr_robinhood.agent_sdk` (the mngr-backed re-implementation)
behaves differently from the upstream `claude_agent_sdk`, **excluding** what the README already
documents (`fork_session`, host-config hermeticity) and surfaces that explicitly raise
`AgentSdkNotImplementedError`.

## Status workflow

Each item carries a **Status** that moves through these stages:

- `proposed` — candidate divergence, not yet checked against the code.
- `investigated` — verified against both implementations; verdict + draft recommendation recorded.
- `decided` — a human has reviewed/edited the recommendation and signed off on the fix approach.
- `fixed` — the change has been implemented (and tested).

Every item below is now `investigated`.

**Verification method:** verdicts are from direct code inspection of both implementations
(the real-SDK internals were read out in full: arg builder, session listing/mutation, and the
control-protocol query loop). In addition, `imbue/mngr_robinhood/test_sdk_divergences.py` encodes
several divergences as living tests: each asserts the real SDK's behavior and is
`xfail(strict=True)` for the mngr target, so a normal run is green (real PASS, mngr XFAIL) and
`pytest --runxfail` shows the "real passes / mngr fails" split. Items marked **Test:** below were
run that way against the live API (or offline where no network is needed) and observed to fail for
the mngr target:

- #1, #6, #7, #8, #9 — offline (no API): real PASS, mngr FAIL/XFAIL.
- #3, #18 — live (one cheap haiku call each): real PASS, mngr FAIL.

The remaining items' verdicts rest on the matching source on both sides (not yet encoded as tests).

Each item also carries a **Verdict**:

- `confirmed` — the divergence is real.
- `not-a-divergence` — investigation showed both implementations behave the same.
- `partial` — real but narrower / more nuanced than originally stated.

Evidence file references:

- Real SDK: `.venv/.../claude_agent_sdk/` — `client.py`, `query.py`, `types.py`, `__init__.py`,
  `_internal/transport/subprocess_cli.py` (`_build_command`), `_internal/query.py`,
  `_internal/sessions.py`, `_internal/session_mutations.py`, `_internal/message_parser.py`.
- mngr SDK: `libs/mngr_robinhood/imbue/mngr_robinhood/agent_sdk.py` + `_agent_sdk/*` +
  `agent_runtime.py`.

---

## Critical -- public surface is incomplete or silently no-ops

### 1. Most of the public API is not re-exported

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** Empirically, the real `__all__` has **123** names and **86** of them are missing
  from `imbue.mngr_robinhood.agent_sdk` (confirmed by importing both modules). Missing
  (→ `ImportError` / `AttributeError` when accessed): `Transport`, `__version__`,
  `CLIJSONDecodeError`, `create_sdk_mcp_server`, `tool`, `SdkMcpTool`, `ToolAnnotations`, all MCP
  types (`McpServerConfig`, `McpStatusResponse`, `McpServerStatus`, `McpToolInfo`, …), task types
  (`TaskStartedMessage`, `TaskProgressMessage`, `TaskNotificationMessage`, `TaskUsage`,
  `TaskBudget`), `DeferredToolUse`, rate-limit types, `ServerToolUseBlock` /
  `ServerToolResultBlock` / `ServerToolName`, `ContextUsageResponse` / `ContextUsageCategory`,
  `PermissionUpdate`, every `*HookInput` + `HookCallback` + `HookEventMessage`, `ThinkingConfig*`,
  `EffortLevel`, `SdkBeta`, `SdkPluginConfig`, `SandboxSettings`, `SessionStore` + all store
  types/functions, `delete_session`, `fork_session`, `list_subagents`, `get_subagent_messages`,
  and all `*_from_store` / `*_via_store` variants. The module docstring's "every type is
  re-exported verbatim" is inaccurate.
- **Test:** `test_sdk_divergences.py::test_divergence_1_reexports_every_public_name` — real PASS,
  mngr FAIL (86 names missing).
- **Recommendation:** Replace the hand-maintained import list with a single
  `from claude_agent_sdk import *`-style re-export of everything in `claude_agent_sdk.__all__`
  except the names this module deliberately overrides (`query`, `ClaudeSDKClient`,
  `list_sessions`, `get_session_info`, `get_session_messages`, `rename_session`, `tag_session`).
  Add a unit test that asserts `set(claude_agent_sdk.__all__) - set(dir(agent_sdk)) == set()` so
  drift fails CI. For functions/classes that cannot be backed (e.g. `create_sdk_mcp_server`,
  `Transport`, `SessionStore`), still re-export the *type* (the README's contract) and let the
  behavioral surfaces raise where unsupported.

### 2. Many `ClaudeAgentOptions` fields are silently ignored

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `map_options_to_agent_args` (driver.py) maps only `system_prompt`, `allowed_tools`,
  `disallowed_tools`, `model`, `permission_mode`, `max_turns`, `add_dirs`, `settings`. The real
  builder (`subprocess_cli.py::_build_command`) maps the fields below. Each unmapped field is a
  silent no-op (accepted because the dataclass type is shared, but never applied):

  | Option | Real CLI mapping | Recommendation |
  |---|---|---|
  | `tools` | `--tools` (csv; `""` disables all; preset→`default`) | Map directly; same flag works over mngr. |
  | `mcp_servers` (file/dict) | `--mcp-config <json/path>` | Map for file/dict/non-sdk servers. |
  | `mcp_servers` (sdk type) | in-process server | Cannot work over mngr (separate process). Strip + warn, or document as unsupported. |
  | `strict_mcp_config` | `--strict-mcp-config` | Map directly. |
  | `extra_args` | `--<key> [value]` (None→bare flag) | Map directly — high value, low effort. |
  | `betas` | `--betas <csv>` | Map directly. |
  | `fallback_model` | `--fallback-model` | Map directly. |
  | `max_budget_usd` | `--max-budget-usd` | Map directly. |
  | `task_budget` | `--task-budget <total>` | Map directly (pull `["total"]`). |
  | `permission_prompt_tool_name` | `--permission-prompt-tool` | Map directly (note flag drops `-name`). |
  | `thinking` | `--thinking adaptive|disabled` / `--max-thinking-tokens` (+`--thinking-display`) | Map directly. |
  | `max_thinking_tokens` | `--max-thinking-tokens` (only if `thinking` unset) | Map with the same precedence. |
  | `effort` | `--effort` | Map directly. |
  | `output_format` (`json_schema`) | `--json-schema <json>` | Map directly; then surface `structured_output` (see #16/#22). |
  | `skills` | appends `Skill(...)` to `--allowedTools` + defaults `--setting-sources` | Replicate the `_apply_skills_defaults` logic. |
  | `plugins` (local) | `--plugin-dir <path>` per plugin | Map directly. |
  | `sandbox` | merged into `--settings` JSON | Merge into the settings file mngr writes. |
  | `include_hook_events` | `--include-hook-events` | Map directly. |
  | `enable_file_checkpointing` | env `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=true` | Set the env var (still need #6 `rewind_files`). |
  | `agents` | sent via initialize/control protocol (no flag) | No CLI flag exists; needs a settings-file or `--agents`-equivalent approach, or document as unsupported. |
  | `session_store` | `--session-mirror` + in-process store | Store is in-process; cannot bridge over mngr. Document as unsupported. |
  | `user` | subprocess `user=` | mngr controls the process; map only if mngr exposes it, else document. |
  | `fallback_model`, `betas`, etc. | (above) | — |
  | `cli_path`, `max_buffer_size`, `stderr`, `env` | internal / env (not flags) | `env` already handled; `stderr` callback could be wired to the agent's stderr; others N/A. |

  Recommendation summary: add the directly-mappable flags to `map_options_to_agent_args` in one
  pass (big coverage win), and explicitly `log.warning` + document the handful that the mngr
  transport genuinely cannot support (`agents`, sdk-type `mcp_servers`, `session_store`).

### 3. `can_use_tool` is invoked far more often than upstream

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** Upstream invokes `can_use_tool` only when the CLI emits a
  `control_request{subtype:"can_use_tool"}` — i.e. only when permission rules evaluate to "ask"
  (it sets `permission_prompt_tool_name="stdio"` to route those, `_internal/query.py:384-412`).
  The bridge wires it as a catch-all `PreToolUse` hook (`matcher:"*"`, hook_bridge.py:124-130), so
  it fires for *every* tool except those in `allowed_tools` (the only pre-approval honored,
  `_dispatch_permission`). Tools auto-allowed by `permission_mode` or `permissions.allow` settings
  still hit the callback here.
- **Test:** `test_sdk_divergences.py::test_divergence_3_can_use_tool_not_invoked_when_auto_allowed`
  (live) — with `permission_mode="bypassPermissions"`, real PASS (callback never invoked), mngr
  FAIL (`recorded == ['Bash']`).
- **Recommendation:** Hard to make exact over the mngr transport (no stdio control channel). Two
  options: (a) document the "fires for every non-`allowed_tools` tool" semantics as a known
  approximation; or (b) if feasible, use claude's stdio permission-prompt MCP tool against the
  interactive agent instead of a catch-all PreToolUse hook. Lower priority than getting the
  semantics *documented* correctly.

### 4. The agent always runs with unattended permission overrides

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `agent_runtime.UNATTENDED_SETTINGS` always applies `auto_dismiss_dialogs`,
  `auto_allow_permissions`, `skipDangerousModePermissionPrompt`, `bypassPermissionsModeAccepted`.
  These can override the caller's `permission_mode`, so a `permission_mode="default"` session does
  not gate/prompt the way upstream would.
- **Recommendation:** These exist only to stop the agent hanging on boot dialogs. Investigate
  whether `bypassPermissionsModeAccepted` / `auto_allow_permissions` can be scoped to the boot
  dialog dismissal without forcing run-time permission bypass — or, when `can_use_tool`/`hooks`
  are configured, narrow them so the bridge actually governs decisions. At minimum document the
  interaction with `permission_mode`.

### 5. `get_mcp_status()` is hardcoded empty

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `client.py::get_mcp_status` returns `{"mcpServers": []}` unconditionally; upstream
  queries live status via the control protocol and returns `McpStatusResponse`.
- **Recommendation:** Tied to #2 (`mcp_servers`). Once MCP config is mapped, populate the list
  from the configured servers (best-effort, since live connection status is unavailable over the
  mngr transport). Until then, document that status is always empty.

---

## High -- missing methods and wrong error/lifecycle behavior

### 6. Several `ClaudeSDKClient` methods are missing entirely

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** Real client has `rewind_files`, `reconnect_mcp_server`, `toggle_mcp_server`,
  `stop_task`, `get_context_usage` (client.py:370-540); the mngr client has none — calling them
  raises `AttributeError`, not `AgentSdkNotImplementedError`.
- **Test:** `test_sdk_divergences.py::test_divergence_6_client_exposes_documented_methods`
  (parametrized over all 5 methods) — real PASS, mngr FAIL for each.
- **Recommendation:** Add stub methods that raise `AgentSdkNotImplementedError` with a clear
  message (so they're discoverable and consistent with `fork_session`), or implement the few that
  are tractable (`get_context_usage` could run a `/context`-style probe; the MCP/task ones depend
  on the control protocol and are likely unsupported).

### 7. `ClaudeSDKClient.__init__` does not accept `transport=`

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** Real `__init__(self, options=None, transport=None)` (client.py:67). The mngr client
  is a pydantic `MutableModel` with only an `options` field.
- **Test:** `test_sdk_divergences.py::test_divergence_7_client_accepts_transport_argument` — real
  PASS, mngr FAIL.
- **Recommendation:** Accept a `transport` keyword for signature compatibility; since the whole
  point is the mngr backend, raise `AgentSdkNotImplementedError` if a non-None transport is
  passed (custom transports are incompatible with the mngr driver).

### 8. `query()` has no `transport=` parameter

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** Real `query(*, prompt, options=None, transport=None)` (query.py:11). mngr's omits
  `transport`, so `query(prompt=..., transport=...)` raises `TypeError`.
- **Test:** `test_sdk_divergences.py::test_divergence_8_query_accepts_transport_argument` — real
  PASS, mngr FAIL.
- **Recommendation:** Same as #7 — accept and reject non-None with `AgentSdkNotImplementedError`.

### 9. Wrong exception types on the "not connected" / error paths

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** mngr raises `RobinhoodError("...called before connect()")` from `query` /
  `interrupt` / `set_model` / `set_permission_mode` / `get_server_info`; upstream raises
  `CLIConnectionError("Not connected. Call connect() first.")` (client.py). A missing `claude` CLI
  surfaces as an mngr error rather than `CLINotFoundError`.
- **Test:** `test_sdk_divergences.py::test_divergence_9_query_before_connect_raises_cli_connection_error`
  — real PASS, mngr FAIL (raises `RobinhoodError`).
- **Recommendation:** Raise the re-exported `CLIConnectionError` (with the same message) for the
  not-connected case, and map a missing-CLI failure to `CLINotFoundError`, so callers' `except`
  blocks behave identically.

### 10. `can_use_tool` + string-prompt validation not enforced

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** Upstream raises `ValueError` if `can_use_tool` is set with a string prompt
  ("requires streaming mode"), and another if `can_use_tool` and `permission_prompt_tool_name` are
  both set (client.py:160-173). mngr enforces neither.
- **Recommendation:** Decide intent. The mngr bridge actually *can* serve `can_use_tool` with a
  string prompt, so the upstream restriction is arguably unnecessary here — but the mutual-exclusion
  check is cheap and worth replicating. At minimum, document that mngr is more permissive about
  string + `can_use_tool`.

### 11. Streaming-input prompts collapsed into a single concatenated turn

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_coerce_prompt_to_text` (client.py) joins all `{"type":"user",...}` dicts with
  `\n` and delivers one message. Upstream streams each dict as a separate user message
  (client.py:305-311).
- **Recommendation:** Deliver each streamed user dict as its own turn (loop `deliver_turn` per
  message) instead of concatenating, so multi-message streaming-input semantics match. Watch
  end-of-turn detection between messages.

### 12. `query()` / `disconnect()` leave a stopped (not destroyed) agent behind

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `stop_session` / `disconnect` stop the agent but never destroy it; each call
  accumulates a `robinhood-` agent in `mngr list`. Upstream `query()` leaves only a session file.
  `destroy_sessions_in_directory` exists but is not part of the normal lifecycle.
- **Recommendation:** Decide the intended contract. If sessions are meant to be resumable, keep
  them but add a documented cleanup/GC path (and have the one-shot `query()` destroy its agent on
  exit, since upstream `query()` is ephemeral). If not, destroy on `disconnect`.

### 13. Session listing only sees this-SDK's own agents

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_list_sdk_session_agents` filters to `created-by=robinhood-agent-sdk` agents that
  are still alive in mngr. Upstream `list_sessions` scans `~/.claude/projects/<sanitized-cwd>/*.jsonl`
  on disk (`_internal/sessions.py`) — every session for the directory, independent of any agent.
- **Recommendation:** Re-point the session functions at the on-disk transcript store
  (`~/.claude/projects/<sanitized cwd>/`) the same way upstream does, rather than enumerating live
  mngr agents — this also fixes #14/#15/#32 and makes sessions visible after the agent is gone.
  (Requires resolving the per-agent `CLAUDE_CONFIG_DIR` mngr uses.) Larger change; flag for design
  discussion.

### 14. `list_sessions` ignores `include_worktrees`

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** mngr accepts the param but never uses it. Upstream, when true, runs
  `git worktree list --porcelain` and also scans sibling worktrees' project dirs, deduping by
  session id (`_internal/sessions.py`).
- **Recommendation:** Implement alongside #13 (it only makes sense once listing reads from the
  on-disk project dirs). If #13 is deferred, document the param as currently ignored.

### 15. `rename_session` / `tag_session` persist to the wrong store

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** mngr writes mngr agent labels. Upstream appends typed JSONL entries
  (`{"type":"custom-title","customTitle":...}` / `{"type":"tag","tag":...}`) to the session's own
  `.jsonl` transcript (`_internal/session_mutations.py`). Titles/tags set by one are invisible to
  the other.
- **Recommendation:** Append the same typed JSONL entries to the session transcript file so the
  data is interoperable with upstream and survives agent destruction. Pairs with #13.

---

## Medium -- synthesized message fields differ from the real ones

### 16. `ResultMessage.subtype` is only `success` / `error`

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_build_turn_result_message` passes `is_error=False`; `build_result_message` maps
  to `"success"`/`"error"`. Upstream passes the CLI's `subtype` through verbatim — values include
  `error_max_turns`, `error_during_execution`, `error_max_budget_usd` (`_internal/message_parser.py`,
  `_internal/query.py`). On a `max_turns` cutoff mngr still reports `"success"`.
- **Recommendation:** Since the session JSONL has no `result` event, the richer subtype isn't
  directly available. Best effort: detect `max_turns`/`max_tokens` terminal `stop_reason` and a
  dead-agent error and map to the corresponding subtype; otherwise document that only
  `success`/`error` are produced.

### 17. `ResultMessage.is_error` hardcoded `False`

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_build_turn_result_message` always passes `is_error=False`. Mid-turn API errors
  (auth, 429/529) can be reported as success; `errors` / `api_error_status` never populated.
- **Recommendation:** Detect agent death / error markers in the transcript (and the
  unauthenticated synthetic-error case) and set `is_error=True` + populate `errors`. Pairs with #16.

### 18. `ResultMessage.stop_reason` never set

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `build_result_message` has no `stop_reason` parameter (left `None`). Upstream sets
  it from the CLI result.
- **Recommendation:** Thread the last assistant `stop_reason` (already absorbed in
  `_absorb_event_metadata`) into the `ResultMessage`.
- **Test:** `test_sdk_divergences.py::test_divergence_18_result_message_has_stop_reason` (live) —
  real PASS (`stop_reason` populated, e.g. `"end_turn"`), mngr FAIL (`None`).

### 19. `ResultMessage.num_turns` semantics differ

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** mngr sets `num_turns = session.turn_count` (user turns delivered). Upstream maps
  `num_turns = data["num_turns"]` from the CLI result (internal model/tool turns).
- **Recommendation:** Approximate by counting assistant messages (model turns) within the turn, or
  document that `num_turns` here counts delivered user turns. Low stakes.

### 20. `usage` and `total_cost_usd` are internally inconsistent

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_build_turn_result_message` sets `usage = session.latest_usage` (last assistant
  message only) but computes `total_cost_usd` from `session.turn_usage_totals` (accumulated). So a
  multi-message turn under-reports `usage` relative to the cost.
- **Recommendation:** Report `usage` as the accumulated `turn_usage_totals` (consistent with the
  cost computation), or document the discrepancy.

### 21. `duration_api_ms` equals wall-clock `duration_ms`

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_build_turn_result_message` passes `duration_api_ms=duration_ms`. Upstream uses
  the CLI's real API time.
- **Recommendation:** No accurate API-time source over the mngr transport; document as
  approximate (set equal to wall-clock) or omit. Low priority.

### 22. The synthesized `system`/`init` message is thin and partly fabricated

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `build_system_init_message` emits only `session_id`, `model`, `cwd`, hardcoded
  `tools` (`_DEFAULT_REPORTED_TOOLS`), empty `mcp_servers`; emitted lazily after the first
  transcript event. Upstream's init comes from the CLI's `system`/`init` event with many more
  fields (`permissionMode`, `apiKeySource`, `slash_commands`, `output_style`, `uuid`, real tool
  list, …).
- **Recommendation:** Reuse the `get_server_info` probe (#23) — or a real `--output-format
  stream-json` init capture — to populate the init message with the negotiated tools / commands /
  output style instead of a hardcoded list. At minimum stop fabricating `tools`.

### 23. `get_server_info()` probes a throwaway process

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `server_info.probe_server_info` runs a separate `claude -p ... stream-json` in the
  cwd (only `--model` applied). Upstream returns `_initialization_result` captured from the actual
  connected session's `initialize` control response (`_internal/query.py:220`). The probe's
  negotiated commands/output-style/tools can differ from the real session (different settings
  layering, no bridge). Upstream also returns `None` when not connected; mngr raises.
- **Recommendation:** Pass the session's real options (settings, add_dirs, etc.) into the probe so
  it matches the session; and return the documented shape rather than raising when no session.
  Ideally capture init from the agent's own first stream-json init event instead of a side process.

### 24. Partial-message streaming only emits text deltas

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `StreamEventSynthesizer` only ever wraps `text_delta` for a single block (index 0).
  Upstream passes through *all* CLI stream deltas verbatim (`thinking_delta`, `input_json_delta`,
  multiple block indices) — `_internal/message_parser.py` does no delta filtering.
- **Recommendation:** Inherent to the tmux-pane reconstruction (no token-level stream available).
  Document that only assistant text deltas are synthesized and thinking/tool-input deltas are
  absent. (Already partly covered by the "approximate" README note, but the *absence of non-text
  deltas* specifically is not.)

### 25. `receive_messages()` drains one turn instead of streaming continuously

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** mngr `receive_messages` starts a fresh per-turn drain that ends at the terminal
  `ResultMessage`. Upstream yields continuously across turns until an `end`/`error` sentinel
  (`_internal/query.py:845-854`); `result` messages do not stop iteration.
- **Recommendation:** Document the per-turn semantics (it matches the common
  `receive_response()` usage), or, if true continuous streaming is wanted, restructure the drain to
  span turns. Note `receive_response()` already matches upstream (stops at `ResultMessage`).

---

## Lower -- finer-grained behavioral gaps

### 26. `HookMatcher.timeout` is ignored

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** hook_bridge.py hardcodes `"timeout": 600` per settings entry and a `~590s` callback
  wait; `matcher.matcher` is honored but `matcher.timeout` is dropped. Upstream forwards
  `HookMatcher.timeout` to the CLI in the initialize config (`_internal/query.py:194-200`).
- **Recommendation:** Use `matcher.timeout` (default 60s, matching upstream) for both the settings
  `timeout` and the bridge callback wait.

### 27. `ToolPermissionContext.suggestions` is always empty

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_dispatch_permission` builds `ToolPermissionContext(suggestions=[], ...)`. Upstream
  populates it from the CLI request's `permission_suggestions` (`_internal/query.py:393-398`).
- **Recommendation:** The PreToolUse hook payload does not carry permission suggestions, so there's
  no source over this transport. Document as always-empty. Low priority.

### 28. Hook/permission `signal` is always `None`

- **Status:** investigated
- **Verdict:** not-a-divergence
- **Evidence:** mngr passes `signal=None`. Upstream *also* hardcodes `signal=None` (a `# TODO: Add
  abort signal support` placeholder in both `can_use_tool` and hook contexts,
  `_internal/query.py:392,446-450`). Behavior is identical.
- **Recommendation:** None needed. Drop from the fix list (kept for the record).

### 29. `PermissionResultDeny.interrupt` is not honored

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_dispatch_permission` maps a deny to a `PreToolUse` `permissionDecision:"deny"`
  regardless of `result.interrupt`. Upstream sends `interrupt` back to the CLI when truthy
  (`_internal/query.py:429-432`).
- **Recommendation:** A PreToolUse hook cannot directly express "interrupt the whole turn"; the
  closest is to deny and then stop the agent when `interrupt=True`. Investigate feasibility, else
  document as unsupported.

### 30. `query()`'s `session_id` parameter is ignored

- **Status:** investigated
- **Verdict:** partial
- **Evidence:** mngr accepts `session_id` and never uses it. Upstream writes it into the user-frame
  `session_id` field — but it is a placeholder label (default `"default"`), not the real session
  UUID, which is governed by `ClaudeAgentOptions.session_id`/`resume` (client.py:283-311). So the
  practical impact is small.
- **Recommendation:** Low priority; document that the per-call `session_id` label is unused. No
  behavioral fix needed unless multi-session labelling is wanted.

### 31. Image / non-text content blocks in prompts are dropped

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_extract_user_text` keeps only `type=="text"` blocks; `_coerce_prompt_to_text`
  flattens to text. Images / other blocks in a streaming-input prompt are lost. Upstream forwards
  full content.
- **Recommendation:** mngr delivers a turn as a single message string, so rich content can't be
  forwarded as-is. Document the limitation; consider serializing non-text blocks if a use case
  arises.

### 32. `SDKSessionInfo.file_size` is `None` and `summary` is not upstream-shaped

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** mngr sets `file_size=None` and `summary = first_prompt or agent name`. Upstream
  sets `file_size = stat.st_size` of the transcript and `summary = customTitle or lastPrompt or
  summary-field or first_prompt` (`_internal/sessions.py`). (Upstream `summary` is *not* a fresh
  LLM call — it's that fallback chain.)
- **Recommendation:** Fold into #13 (read from on-disk transcript): then `file_size` and the
  `summary` fallback chain come for free.

### 33. User `settings` and the hook-bridge `--settings` both passed

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `_create_agent` / `_rewrite_agent_launch_command` build
  `map_options_to_agent_args(...) + _bridge_settings_args(...)`, so when both `options.settings`
  and the bridge are active, two `--settings` flags are passed. Upstream routes hooks over the
  control protocol, never via a competing settings file.
- **Recommendation:** Merge the user's `settings` and the bridge hooks into one settings file
  (deep-merge the `hooks` key) and pass a single `--settings`, to avoid last-flag-wins clobbering
  the user's settings.

### 34. `set_model` / `set_permission_mode` apply only on the next turn

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `reconfigure_session` rewrites the launch command and restart-with-resumes the
  agent, so the change lands on the next turn. Upstream sends a live control request mid-stream with
  no restart (`_internal/query.py:735-751`).
- **Recommendation:** Inherent to the mngr transport (no stdio control channel to a running
  claude). Document the "applies next turn" semantics.

### 35. `interrupt()` kills/stops the agent process

- **Status:** investigated
- **Verdict:** confirmed
- **Evidence:** `interrupt_session` calls `stop_agent`, ending the turn with a synthesized terminal
  `ResultMessage`; the next `query()` restarts-with-resume. Upstream sends a control-protocol
  `interrupt` to a still-running process (`_internal/query.py:731-733`); partial state and timing
  differ.
- **Recommendation:** Inherent to the transport; the resume-on-next-turn behavior is a reasonable
  approximation. Document the mechanism and that mid-turn partial state may differ.
