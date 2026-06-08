# Potential undocumented divergences: mngr-backed Agent SDK vs. the real `claude_agent_sdk`

This is an unverified brainstorm of places where `imbue.mngr_robinhood.agent_sdk` (the
mngr-backed re-implementation) may behave differently from the upstream `claude_agent_sdk`,
**excluding** the divergences that are already documented in the README or that explicitly raise
`AgentSdkNotImplementedError` (`fork_session`, host-config hermeticity).

Items are sorted roughly by how large / impactful the divergence could be. Nothing here has been
verified yet -- these are candidates to investigate.

Reference points used:

- Real surface: `.venv/.../claude_agent_sdk/{__init__.py,client.py,query.py,types.py}`
- mngr surface: `libs/mngr_robinhood/imbue/mngr_robinhood/agent_sdk.py` + `_agent_sdk/*`

---

## Critical -- public surface is incomplete or silently no-ops

1. **Most of the public API is NOT re-exported, despite the docstring claiming "every type is
   re-exported verbatim."** `agent_sdk.py` re-exports ~30 names; the real `__all__` has ~130.
   Importing any of these from `imbue.mngr_robinhood.agent_sdk` raises `ImportError`:
   `Transport`, `CLIJSONDecodeError`, `create_sdk_mcp_server`, `tool`, `SdkMcpTool`,
   `ToolAnnotations`, all MCP types (`McpServerConfig`, `McpStatusResponse`, `McpServerStatus`,
   `McpToolInfo`, ...), all task types (`TaskStartedMessage`, `TaskProgressMessage`,
   `TaskNotificationMessage`, `TaskUsage`, `TaskBudget`), `DeferredToolUse`, the rate-limit types,
   `ServerToolUseBlock` / `ServerToolResultBlock` / `ServerToolName`, `ContextUsageResponse` /
   `ContextUsageCategory`, `PermissionUpdate`, every `*HookInput` type + `HookCallback` +
   `HookEventMessage`, `ThinkingConfig*`, `EffortLevel`, `SdkBeta`, `SdkPluginConfig`,
   `SandboxSettings` (+ network/ignore), `SessionStore` and all store types/functions, the session
   mutations `delete_session` / `fork_session`, the subagent functions `list_subagents` /
   `get_subagent_messages`, and all the `*_from_store` / `*_via_store` async variants. Code written
   against the real SDK's imports will fail at import time.

2. **A large set of `ClaudeAgentOptions` fields is silently ignored** (accepted because the type is
   identical, but never mapped to a flag or behavior in `map_options_to_agent_args`). Each is a
   silent behavioral no-op rather than an error:
   - `tools` (base built-in tool preset / list) -- cannot restrict the base tool set.
   - `mcp_servers` / `strict_mcp_config` -- MCP servers configured via options do nothing.
   - `agents` -- programmatic subagent definitions are dropped.
   - `skills` -- skill enabling is dropped.
   - `plugins` -- SDK plugins are not loaded.
   - `extra_args` -- arbitrary pass-through CLI args are dropped entirely.
   - `output_format` -- structured output schema is ignored (`ResultMessage.structured_output`
     stays `None`).
   - `thinking` / `max_thinking_tokens` / `effort` -- thinking/effort control is dropped.
   - `betas` (e.g. 1M-context beta), `fallback_model`, `max_budget_usd`, `task_budget`,
     `permission_prompt_tool_name`, `user`, `max_buffer_size`, `cli_path`, `sandbox`,
     `enable_file_checkpointing`, `session_store` / `session_store_flush` / `load_timeout_ms`,
     `include_hook_events`, `stderr` (callback never invoked) -- all ignored.

3. **`can_use_tool` is invoked far more often than in the real SDK.** Upstream consults
   `can_use_tool` only when the CLI's permission rules evaluate to "ask". The bridge wires it as a
   catch-all `PreToolUse` hook (`matcher: "*"`), so it fires for *every* tool call except those in
   `allowed_tools`. Tools that would be auto-allowed by `permission_mode` (`acceptEdits`,
   `bypassPermissions`) or by `permissions.allow` settings rules will still hit the callback here.
   This changes which tools the callback sees and can flip allow/deny outcomes.

4. **The spawned agent always runs with unattended permission overrides applied**
   (`auto_dismiss_dialogs`, `auto_allow_permissions`, `skipDangerousModePermissionPrompt`,
   `bypassPermissionsModeAccepted` from `UNATTENDED_SETTINGS`). This can override / conflict with
   the caller's `permission_mode` -- e.g. a `permission_mode="default"` session may not actually
   prompt or gate the way the real SDK would, so the observable permission behavior diverges.

5. **`get_mcp_status()` is hardcoded to `{"mcpServers": []}`** regardless of configured MCP
   servers, and returns a plain `Mapping` rather than the real `McpStatusResponse` shape/content.

---

## High -- missing methods and wrong error/lifecycle behavior

6. **Several `ClaudeSDKClient` methods are missing entirely** (raise `AttributeError`, not
   `NotImplementedError`): `rewind_files`, `reconnect_mcp_server`, `toggle_mcp_server`,
   `stop_task`, `get_context_usage`.

7. **`ClaudeSDKClient.__init__` does not accept a `transport=` argument.** The real client takes
   `(options, transport)`; the mngr client is a pydantic model with only `options`. Passing a
   custom transport fails.

8. **`query()` has no `transport=` parameter.** Real signature is
   `query(*, prompt, options=None, transport=None)`; mngr is `query(*, prompt, options=None)`.

9. **Wrong exception types for the documented "not connected" / error paths.** Calling
   `query` / `interrupt` / `set_model` / `set_permission_mode` / `get_server_info` before
   `connect()` raises `RobinhoodError`, but the real SDK raises `CLIConnectionError`. Code that
   catches `CLIConnectionError` (which *is* re-exported) will not catch these. Similarly a missing
   `claude` CLI surfaces as an mngr error, not `CLINotFoundError`.

10. **`can_use_tool` + string-prompt validation is not enforced.** The real SDK raises `ValueError`
    ("can_use_tool callback requires streaming mode") for a string prompt, and another `ValueError`
    if `can_use_tool` and `permission_prompt_tool_name` are both set. The mngr client raises
    neither (it accepts a string prompt with `can_use_tool`).

11. **Streaming-input prompts are collapsed into a single concatenated turn.**
    `_coerce_prompt_to_text` joins all `{"type":"user",...}` dicts with newlines and delivers them
    as one message. The real SDK sends each as a separate streamed user message. Multi-message
    streaming-input semantics differ substantially.

12. **`query()`/`disconnect()` leave a stopped (not destroyed) mngr agent behind.** Each call
    accumulates a `robinhood-`prefixed agent in `mngr list`. The real `query()` is ephemeral and
    leaves only a session file. (`destroy_sessions_in_directory` exists but is not called by the
    normal lifecycle.)

13. **`list_sessions` / `get_session_info` / `get_session_messages` only see agents this SDK
    created** (filtered by the `created-by=robinhood-agent-sdk` label and still alive in mngr).
    The real SDK enumerates all on-disk Claude sessions for the directory. Sessions created outside
    the SDK -- or whose agent was destroyed -- are invisible.

14. **`list_sessions` ignores the `include_worktrees` argument** (accepted, never used).

15. **`rename_session` / `tag_session` persist to mngr agent labels, not to the Claude session
    file.** Titles/tags set via the mngr SDK and the real SDK live in different stores and are not
    interoperable; a title set by one is invisible to the other.

---

## Medium -- synthesized message fields differ from the real ones

16. **`ResultMessage.subtype` is only ever `"success"` / `"error"`.** The real SDK emits richer
    subtypes (`error_max_turns`, `error_during_execution`, `error_max_budget_usd`, ...). On a
    `max_turns` cutoff the mngr SDK still reports `"success"`.

17. **`ResultMessage.is_error` is hardcoded `False` for normal completion.** Mid-turn API errors
    (e.g. auth failure, 429/529) may be reported as a successful result. Related: `errors` and
    `api_error_status` are never populated.

18. **`ResultMessage.stop_reason` is never set** (left `None`); the real SDK reports the actual
    terminal stop reason.

19. **`ResultMessage.num_turns` is the count of *user turns delivered to the session*, not the
    model/tool turn count** the real SDK reports.

20. **`ResultMessage.usage` reflects only the last assistant message's usage**, while
    `total_cost_usd` is computed from the turn's *accumulated* usage -- so the two fields are
    internally inconsistent, and `usage` under-reports a multi-message turn.

21. **`ResultMessage.duration_api_ms` is set equal to wall-clock `duration_ms`** rather than the
    real API time.

22. **The synthesized `system`/`init` message is thin and partly fabricated:** `tools` is a
    hardcoded built-in list (not the negotiated set after `allowed_tools`/`disallowed_tools`/MCP),
    `mcp_servers` is always empty, `model` may be `""` until the first assistant event, and many
    real init fields are absent (`permissionMode`, `apiKeySource`, `slash_commands`, `output_style`,
    `uuid`, etc.). It is also emitted lazily (after the first transcript event) rather than up front.

23. **`get_server_info()` before any turn probes a *throwaway* `claude` process**, not the session
    agent. The probe runs `claude -p` in the cwd with only `--model`, so its reported
    commands/output-style/tools may differ from what the actual session would negotiate (different
    settings layering, no bridge, etc.).

24. **Partial-message streaming only ever emits `text_delta`s for a single content block (index 0).**
    The real SDK streams thinking deltas, `input_json_delta` for tool calls, and multiple block
    indices. Tool-use and reasoning are invisible in the mngr partial stream. (Text approximation
    itself is documented; the *absence of non-text deltas* is not.)

25. **`receive_messages()` drains exactly one turn (until a terminal `ResultMessage`) per call.**
    In the real SDK it yields continuously across turns. An `async for` over `receive_messages()`
    therefore terminates after one turn here instead of staying open.

---

## Lower -- finer-grained behavioral gaps

26. **`HookMatcher.timeout` is ignored** -- every settings hook entry is hardcoded to `timeout: 600`
    and the callback wait is a fixed `~590s`.

27. **`ToolPermissionContext.suggestions` is always `[]`** -- the real SDK supplies permission
    suggestions to the callback.

28. **Hook callbacks always receive `signal=None`** in their context dict; the real SDK provides an
    abort signal.

29. **`PermissionResultDeny.interrupt` is not honored** -- a deny is mapped to a `PreToolUse`
    `permissionDecision: "deny"` regardless of the `interrupt` flag.

30. **`query()`'s `session_id` parameter is accepted but ignored** (real SDK tags messages with it;
    mngr defaults it to `None` and never uses it).

31. **Image / non-text content blocks in a prompt are dropped.** `_extract_user_text` keeps only
    `type=="text"` blocks; the real SDK forwards full content (images, etc.).

32. **`SDKSessionInfo.file_size` is always `None`** (real SDK reports the transcript file size), and
    `summary` is the first user prompt or agent name rather than the real AI-generated summary.

33. **User-supplied `settings` and the hook-bridge `--settings` can both be passed**, so the two
    settings files coexist (and may interact/override) in a way the real SDK -- which routes hooks
    over the control protocol, not via a settings file -- never produces.

34. **`set_model` / `set_permission_mode` apply only on the *next* turn** (they rewrite the launch
    command and restart-with-resume). The real SDK applies them mid-stream via the control protocol.

35. **`interrupt()` works by killing/stopping the agent process**, so the in-flight turn ends with a
    synthesized terminal `ResultMessage` and the next turn restarts-with-resume. The real SDK sends
    a control-protocol interrupt to a still-running process; partial state and timing differ.
