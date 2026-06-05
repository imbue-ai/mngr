# mngr_robinhood

## Refined prompt

we're going to make a new mngr plugin, mngr_robinhood, that exposes a single command, "mngr robinhood" that acts *exactly* like "claude -p", except it will use mngr create, mngr message, and mngr transcript to implement it.

Go gather all of the context for the mngr library and its mngr_claude plugin (per instructions in CLAUDE.md). Also take a look at the output of "claude --help" to see which options matter for -p / --print

Once you've gathered that context, please work through any implementation difficulties with actually creating this plugin by asking questions per the architect skill.

It *should* be relatively easy to do this--we'll want this new mngr plugin to work exactly like the "claude" CLI program (take all of the same args), except that "-p" is implied (doesn't matter if it is passed, always acts as if it is)

Most of the args can simply be passed through via agent args in our call to mngr create.
The call to mngr create should end up working "in place" in this case.
It *must* also set the right claude agent config vars so that it is able to work unattended (eg, suppress the various dialogs by setting the right config vars).

The args that mention the --print option should *not* be passed through, and should be "simulated"
Basically, there are only a few, and it's about reading from stdin, what input format to expect, what output format to send, etc
You'll probably need to experiment a little bit with claude -p in order to see the exact formats and effects of those options.

* Uses internal Python APIs (`api_create`, `send_message_to_agents`, `read_event_content`); never shells out to `mngr`.
* Spawns a regular `claude` agent type via `mngr create --no-connect --transfer none --message <first-prompt>` (auto-generated agent name with `robinhood-` prefix AND `created-by=robinhood` label, local host, current cwd). For follow-up turns in stream-json input mode, uses `mngr message`. Each turn's reply is harvested from `mngr transcript --format jsonl`.
* The agent is ephemeral: destroyed on exit (success, failure, or signal).
* End-of-turn detection: inline polling of `agent.get_lifecycle_state()` waiting for `WAITING` (premature `STOPPED`/`DONE` returns `EXIT_CLAUDE_ERROR=1`).
* Sets `mngr_claude`'s existing unattended config vars (`auto_dismiss_dialogs=True`, `auto_allow_permissions=True`, plus `settings_overrides.skipDangerousModePermissionPrompt=true` and `settings_overrides.bypassPermissionsModeAccepted=true`) via `mngr create -S` settings overrides; no new permission semantics introduced. The two `settings_overrides.*` flags are normally added by `mngr_claude` only when `not host.is_local`; robinhood always runs on the local host, so we set them explicitly to avoid hangs on those prompts.
* Working directory: user's cwd, in-place; implies `--no-ensure-clean` so a dirty tree is OK.
* `session_preserve_on_destroy` stays at its default (`True`); per-invocation session files remain on disk for debugging.
* Pass through all `claude` flags as agent args via `--` to `mngr create`, except:
  * **Simulated by the wrapper (never forwarded):** `-p/--print` (no-op), `--input-format`, `--output-format`, `--replay-user-messages`.
  * **Rejected with a clear error in v1:** `--fallback-model`, `--max-budget-usd`, `--no-session-persistence`, `--include-hook-events`, `--include-partial-messages`, `-c/--continue`, `-r/--resume`, `--session-id`.
  * **Pass-through verbatim:** `--bare`, `--model`, `--add-dir`, `--allowedTools`, `--disallowedTools`, `--permission-mode`, `--system-prompt[-file]`, `--append-system-prompt[-file]`, `--max-turns`, `--mcp-config`, `--settings`, `--strict-mcp-config`, `--agents`, `--tools`, `--betas`, `--effort`, `--verbose`, `--debug[-file]`, `--exclude-dynamic-system-prompt-sections`, `--json-schema`, `--setting-sources`, `--plugin-dir`, `--plugin-url`, `--file`, and any others not listed above.
* Input formats:
  * `--input-format=text` (default): single prompt from positional argv; if absent, read all of stdin when stdin is not a TTY. Empty prompt with no stdin → exit 2 with a usage hint.
  * `--input-format=stream-json`: NDJSON lines, only `{"type":"user","message":{"role":"user","content":"<string>"}}` shape supported; content-block arrays, images, `tool_use_result`, `control_request`, etc. → reject with a clear error and exit 2.
* Output formats: all driven by `mngr transcript --format jsonl` (the common transcript):
  * `text`: concatenate every assistant `text` block emitted during the agent's internal turns and print it to stdout, plus a trailing newline.
  * `json`: synthesize a `result` envelope matching claude's native shape; fill the assistant `result` text from the transcript and `session_id` / `duration_ms` from what mngr can observe; `total_cost_usd=0`, `usage=null`, and other fields mngr can't observe are zeroed or null.
  * `stream-json`: live-poll the transcript every ~100ms and replay each event as NDJSON, synthesizing a leading `{"type":"system","subtype":"init",...}` envelope and a trailing `result` envelope the same way as `json`.
* mngr's own console chatter (progress lines, "Creating agent..." spinners) is silenced via `--quiet` / `--headless` and loguru-level overrides so stdout shows pure claude-style output; mngr errors still go to stderr.
* Concurrent invocations in the same cwd are allowed; each gets a unique auto-generated name and operates independently (same behavior `claude -p` would have).
* No wrapper-level runaway timeout; users compose with `timeout(1)` or `--max-turns N` if they want one.
* Environment forwarding: the current env is forwarded to the agent via `mngr create --pass-env` for every key in `os.environ`, **except** the per-agent `MNGR_*` and `LLM_USER_PATH` vars that mngr's base `_collect_agent_env_vars` sets specifically for the new agent. Forwarding the parent process's values for those would clobber the spawned agent's correct values (the explicit `env_vars` step happens after mngr's per-agent defaults) and break the readiness hook (which writes `$MNGR_AGENT_STATE_DIR/session_started`), the background-tasks script, and the common-transcript writer.
* Signals: SIGINT and SIGTERM are trapped; the agent is destroyed before the signal is re-raised so the shell sees the conventional `128+signum` exit code.
* Exit code: 0 on a successful turn (transcript shows `assistant` reply, no `is_error` event); 1 on a claude/api error (transcript or stream-json result carries `is_error=true`); 2 on mngr-side failures (agent failed to start, transcript unreadable, invalid args, missing prompt, etc.).
* Pre-flight: none. `mngr create` is responsible for its own failure modes (no claude binary, no auth, etc.); those surface as exit 2.
* Plugin packaging: `libs/mngr_robinhood/imbue/mngr_robinhood/` mirroring `mngr_wait`'s layout. PyPI name `imbue-mngr-robinhood`. Hard deps on `imbue-mngr` and `imbue-mngr-claude`.
* CLI surface: just `mngr robinhood` (no alias). Standard `CommandHelpMetadata` entry so it shows up in `mngr --help` and `mngr ask`.

## Overview

- Adds a new top-level mngr command `mngr robinhood` that behaves as a drop-in replacement for `claude -p`.
- Each invocation spins up a fresh `claude` agent via `mngr create --no-connect --transfer none`, delivers the user prompt(s) through `mngr message`, harvests replies from `mngr transcript`, and destroys the agent on exit.
- Motivation: lets every Claude-driven workflow that today shells out to `claude -p` instead route through mngr — getting agent isolation, environment portability, and the full mngr observability surface "for free", without modifying any consumer scripts.
- All consumed claude flags (`-p`, `--input-format`, `--output-format`, `--replay-user-messages`) are simulated by the wrapper; every other claude flag passes through as agent args.
- Flags whose semantics depend on `--print` internals that mngr cannot reproduce (`--fallback-model`, `--max-budget-usd`, `--no-session-persistence`, `--include-hook-events`, `--include-partial-messages`, `-c/-r/--session-id`) are rejected with a clear error in v1.

## Expected Behavior

- `mngr robinhood "summarize this repo"` runs in the current directory, prints claude's text response to stdout, exits 0.
- `cat error.log | mngr robinhood "explain this"` and `cat error.log | mngr robinhood` both work: stdin is read when no positional prompt is given.
- `mngr robinhood` with neither argv prompt nor piped stdin → exit 2 with `error: no prompt provided`.
- `mngr robinhood "..." --output-format json` prints a single JSON object matching `claude -p --output-format json`'s shape (synthesized `result` envelope with `result`, `session_id`, `duration_ms`, `is_error`, plus zeroed/null cost/usage fields).
- `mngr robinhood "..." --output-format stream-json --verbose` streams NDJSON events as the agent works; first line is a `system/init` envelope, subsequent lines are reformatted transcript events, final line is the `result` envelope.
- `mngr robinhood --input-format=stream-json --output-format=stream-json` reads NDJSON `user` lines from stdin, sends each one as a `mngr message`, waits for end-of-turn, emits NDJSON output events between turns, and exits 0 on stdin EOF (after destroying the agent).
- All claude-side flags (`--model`, `--allowedTools`, `--system-prompt`, `--bare`, etc.) reach the spawned claude unchanged.
- `mngr robinhood -c "..."` (or `--resume`, `--session-id`) exits 2 immediately with `error: --continue / --resume / --session-id are not supported by mngr robinhood in v1`.
- `--include-partial-messages`, `--max-budget-usd`, `--include-hook-events`, `--fallback-model`, `--no-session-persistence` each error the same way (`error: --X is not supported by mngr robinhood in v1`).
- A stream-json input line that is anything other than `{"type":"user","message":{"role":"user","content":"<string>"}}` exits 2 with `error: only simple text user messages are supported by mngr robinhood in v1` and includes the offending line in the message.
- Ctrl-C destroys the spawned agent before re-raising SIGINT; no orphan `robinhood-*` agents remain on the local host.
- The spawned agent name is `robinhood-<coolname>` (e.g. `robinhood-graceful-unicorn`) with a `created-by=robinhood` label; visible briefly in `mngr list` while the run is in progress.
- Multiple `mngr robinhood` invocations in the same cwd run concurrently without locking; each has its own unique agent.
- A working tree with uncommitted changes is fine — `--no-ensure-clean` is implied.
- mngr's normal progress output (status spinners, "Creating agent..." lines) is suppressed; only claude-style output appears on stdout. mngr-side errors still go to stderr.
- The full current env is forwarded to the agent via `--pass-env` so `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, Bedrock/Vertex creds, etc. work without explicit configuration.
- `mngr --help` lists `robinhood`; `mngr ask "how do I run claude unattended"` surfaces it.
- Per-invocation session files remain on disk (mngr_claude default `preserve_sessions_on_destroy=True`) for debugging.

## Implementation Plan

### Package layout

- `libs/mngr_robinhood/`
  - `pyproject.toml` — pypi name `imbue-mngr-robinhood`, entry point `mngr_robinhood = "imbue.mngr_robinhood.plugin"`, deps on `imbue-mngr` and `imbue-mngr-claude`.
  - `README.md` — short description plus a usage example.
  - `conftest.py` — registers the shared conftest hooks and suppresses startup warnings (matches `mngr_wait`).
  - `imbue/mngr_robinhood/`
    - `__init__.py` — blank (per CLAUDE.md).
    - `plugin.py` — pluggy `@hookimpl register_cli_commands()` returning the click command.
    - `cli.py` — the `robinhood` click command + top-level `_run(...)` orchestrator.
    - `arg_partition.py` — pure helpers that split a raw argv into (simulated, rejected, pass-through) buckets.
    - `input_modes.py` — pure helpers for reading the next user-turn prompt from text/stream-json sources.
    - `output_modes.py` — pure helpers that convert common-transcript events into text/json/stream-json output bytes.
    - `orchestrator.py` — the per-invocation lifecycle: create agent, deliver turns, harvest replies, emit output, destroy on exit.
    - `data_types.py` — `ArgPartition`, `InputFormat`/`OutputFormat` enums, `ResultMeta`.
    - `errors.py` — `UnsupportedClaudeFlagError`, `InvalidStreamJsonInputError`, `MissingPromptError`.
    - `test_ratchets.py` — copied from `mngr_wait` scaffold.
    - `arg_partition_test.py`, `input_modes_test.py`, `output_modes_test.py`, `orchestrator_test.py` — pure unit tests.

### Key code paths and signatures

- `plugin.py`
  - `@hookimpl def register_cli_commands() -> Sequence[click.Command]` → `[robinhood]`.

- `cli.py`
  - `class RobinhoodCliOptions(CommonCliOptions)` — captures top-level argv before partitioning.
  - `@click.command(name="robinhood", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})` plus an `@click.argument("argv", nargs=-1, type=click.UNPROCESSED)` so click does no opinionated parsing of claude's own flags.
  - `@click.pass_context def robinhood(ctx, **kwargs)` — sets up the mngr context via `setup_command_context`, parses argv via `partition_args(...)`, dispatches to `orchestrator.run(...)`, exits with the resulting code.
  - `CommandHelpMetadata(key="robinhood", one_line_description="Drop-in mngr-backed replacement for `claude -p`", ...).register()` so `mngr ask` and `mngr --help` know about it.
  - `add_pager_help_option(robinhood)`.

- `arg_partition.py`
  - `SIMULATED_FLAGS: frozenset[str] = {"-p", "--print", "--input-format", "--output-format", "--replay-user-messages"}` — value-or-flag-form aware.
  - `REJECTED_FLAGS: Mapping[str, str]` — flag → user-facing message for the unsupported-in-v1 set.
  - `@pure def partition_args(argv: tuple[str, ...]) -> ArgPartition` — produces `ArgPartition(simulated, pass_through_agent_args, positional_prompt, rejected_with_message_or_none)`. Handles both `--flag=value` and `--flag value` forms by walking the list with a small state machine; raises `UserInputError` on a rejected flag (caught by the CLI and turned into exit 2).
  - `@pure def resolve_formats(simulated: SimulatedFlags) -> ResolvedFormats` — defaults: `InputFormat.TEXT`, `OutputFormat.TEXT`; also validates `--replay-user-messages` only makes sense when both formats are `stream-json`.

- `input_modes.py`
  - `def iter_user_prompts(format: InputFormat, positional: str | None, stdin: TextIO) -> Iterator[str]` — yields one prompt at a time. For `TEXT`: yields exactly one prompt (positional, falling back to `stdin.read()` when not a TTY; raises `UserInputError("no prompt provided")` otherwise). For `STREAM_JSON`: yields one prompt per stdin line, parsing each line as JSON, validating the shape `{"type":"user","message":{"role":"user","content":"<string>"}}`, and raising `UserInputError` with the offending line on any other shape.

- `output_modes.py`
  - `def stream_output(format: OutputFormat, events: Iterator[TranscriptEvent], result_meta: ResultMeta, stdout: TextIO) -> None` — drives output. For `TEXT`: accumulates assistant text deltas, writes the concatenation plus a trailing newline at end. For `JSON`: blocks until all events are consumed, builds the `result` envelope via `build_result_envelope(...)`, writes it as a single JSON line. For `STREAM_JSON`: writes `system/init` first; for each transcript event, writes a synthesized `{"type":"assistant","message":{"role":"assistant","content":[...]}, ...}` line; writes a `result` envelope at the end.
  - `@pure def build_result_envelope(text: str, session_id: str, duration_ms: int, is_error: bool, error_text: str | None) -> dict[str, Any]` — produces the claude-native shape; sets `total_cost_usd=0`, `usage=None`, `num_turns=1`, etc. for fields mngr does not observe.
  - `@pure def event_to_stream_json(event: dict[str, Any]) -> dict[str, Any] | None` — converts a common-transcript event to a claude-stream-json line; returns None for events we drop (e.g. internal mngr lifecycle).

- `orchestrator.py`
  - `class RobinhoodRun(MutableModel)` — holds the lifetime state (`agent_id`, `agent_name`, `host`, `created_at`).
  - `def run(mngr_ctx: MngrContext, partition: ArgPartition, formats: ResolvedFormats, stdin: TextIO, stdout: TextIO) -> int` — top-level driver. Order of operations:
    1. Install SIGINT/SIGTERM handlers that call `destroy_run(...)` then re-raise.
    2. Read the *first* prompt from `input_modes.iter_user_prompts(...)`. If there is no first prompt → `return 2`.
    3. Build `CreateAgentOptions` for an `AgentTypeName("claude")` agent with: `agent_args = partition.pass_through_agent_args`; `initial_message = first_prompt`; settings overrides (see below); auto-generated name `robinhood-<coolname>` and label `created-by=robinhood`; `target_path = Path.cwd()`; transfer mode `NONE`; `--no-connect` semantics (no connection options).
    4. Call `api_create(...)` and remember the returned agent.
    5. For each subsequent prompt (stream-json input mode only), call `send_message_to_agents(message_content=prompt, include_filters=(f"id == \"{agent_id}\"",), error_behavior=ABORT, ...)`. The first prompt is delivered via `initial_message` and skipped here.
    6. After each prompt is delivered, poll `agent.get_lifecycle_state()` every `_POLL_INTERVAL_SECONDS` until it reaches `WAITING` (or terminates as `STOPPED`/`DONE`, which is treated as a claude-side failure). Each poll iteration also drains new events from `read_event_content(target, "claude/common_transcript/events.jsonl")` and pushes them through `StreamingOutputWriter.emit_events(...)`.
    7. On stdin EOF (text mode: always after one turn; stream-json mode: after the iterator drains), finalize output with the result envelope, destroy the agent, and return 0/1.
    8. Map any `MngrError` / `UserInputError` / `BaseException` to the appropriate exit code in a top-level try/except.
  - `def destroy_run(run: RobinhoodRun, mngr_ctx: MngrContext) -> None` — best-effort destroy; logs and swallows errors so signal cleanup never raises.
  - Settings overrides applied via `mngr create -S` (passed through `MngrContext.config`):
    - `agent_types.claude.auto_dismiss_dialogs = true`
    - `agent_types.claude.auto_allow_permissions = true`
    - `agent_types.claude.settings_overrides.skipDangerousModePermissionPrompt = true`
    - `agent_types.claude.settings_overrides.bypassPermissionsModeAccepted = true`
    - (The two `settings_overrides.*` flags are normally added by `mngr_claude` only when `not host.is_local`; robinhood always runs on the local host, so we set them explicitly to avoid hangs on the "bypass permissions mode" and "skip dangerous mode" prompts.)

- `data_types.py`
  - `class InputFormat(UpperCaseStrEnum)` — `TEXT`, `STREAM_JSON`.
  - `class OutputFormat(UpperCaseStrEnum)` — `TEXT`, `JSON`, `STREAM_JSON`.
  - `class ArgPartition(FrozenModel)` — `simulated_flags: Mapping[str, str]`, `pass_through_agent_args: tuple[str, ...]`, `positional_prompt: str | None`.
  - `class ResolvedFormats(FrozenModel)` — `input_format: InputFormat`, `output_format: OutputFormat`, `replay_user_messages: bool`.
  - `class ResultMeta(FrozenModel)` — `session_id: str`, `duration_ms: int`, `is_error: bool`, `error_text: str | None`.

### Logging suppression

- The CLI calls `setup_command_context(...)` with `--quiet` and `--headless` forced True so loguru emits nothing on stderr below WARNING.
- Sub-API calls (`api_create`, `send_message_to_agents`, etc.) already respect loguru levels; no per-call gymnastics needed.

### Signal handling

- `signal.signal(signal.SIGINT, _make_handler(run, mngr_ctx, original_int_handler))` and likewise for `SIGTERM`. Handler destroys the agent then re-installs the original handler and re-raises the signal so the shell sees `128 + signum`.

### Plugin discovery / registration

- The plugin is loaded by mngr's existing plugin auto-discovery via the `mngr` pluggy hook namespace; no explicit enabling step required after `pip install`.
- The `imbue-mngr-claude` dep in `pyproject.toml` guarantees `mngr_claude` is also resolvable; no runtime checks added.

### Documentation

- Add `libs/mngr_robinhood/README.md` with: one-paragraph description, install snippet, three short usage examples (text, json, stream-json), and a list of v1 unsupported flags.
- Update the top-level `README.md` "Sub-projects" list to include `libs/mngr_robinhood/`.
- Add changelog entry at `changelog/mngr-robinhood.md` (stub already exists; expand at PR time).

## Implementation Phases

1. **Skeleton & arg partitioning.** Create the package layout. Implement `arg_partition.py` + tests (rejects bad flags, partitions correctly across all forms `--flag=value`, `--flag value`, short forms). Wire up `plugin.py` and a no-op `cli.py` that just prints the partition. Verify `mngr robinhood --help` works. No agent code yet.
2. **Text-mode end-to-end.** Implement `orchestrator.run(...)` for the single-turn text case: spawn a real claude agent via `api_create`, deliver `initial_message`, wait for `WAITING`, harvest the last assistant `text` from `common_transcript/events.jsonl`, print it, destroy the agent. Implement `--quiet`/`--headless` suppression and signal cleanup. Smoke-test manually.
3. **JSON output.** Implement `output_modes.build_result_envelope(...)` and the `json` output path. Compare side-by-side with `claude -p --output-format json` output on a fixed prompt; fields mngr can't observe are zeroed/null but the shape is identical.
4. **Stream-json input.** Implement `input_modes.iter_user_prompts` for `STREAM_JSON`. Drive multi-turn via `send_message_to_agents` after the first turn. Manually verify with a multi-line stdin script.
5. **Stream-json output.** Implement `output_modes.event_to_stream_json` + live polling (~100ms cadence) from a background thread; emit `system/init` first and `result` last. Manually verify side-by-side with real claude.
6. **Polish & docs.** Pass-through verification for every claude flag listed above; expand README; write the integration + release tests; verify the agent name shows up correctly in `mngr list`; finalize changelog entry.

## Testing Strategy

### Unit tests (xdist-parallel `_test.py`)

- `arg_partition_test.py`:
  - partitions `mngr robinhood -p --output-format=json "hello"` correctly across forms.
  - rejects each flag in `REJECTED_FLAGS` with the expected error message.
  - separates pass-through args (`--model opus`, `-- --foo`) from simulated args.
  - validates `--replay-user-messages` requires both stream-json formats.
- `input_modes_test.py`:
  - text mode: positional wins over stdin; stdin used when no positional; empty input → `UserInputError`.
  - stream-json mode: valid line yields its content; non-text content/`control_request`/malformed JSON each raise with the offending line included.
- `output_modes_test.py`:
  - `build_result_envelope` produces every required claude-native field, with zeroed/null defaults.
  - `event_to_stream_json` converts assistant text events; drops events we don't surface.
  - text mode concatenates assistant text blocks across multi-turn transcripts.
- `orchestrator_test.py`:
  - mocks `api_create` / `send_message_to_agents` / `wait_for_state` / `read_event_content` to verify the call sequence (create → message → wait → harvest → destroy).
  - SIGINT during a run calls `destroy_run` exactly once.

### Integration tests (no marker, mock-agent based — `test_robinhood.py`)

- Spawn the click CLI in-process with `CliRunner`, using a mock claude agent type registered in the test harness (see `libs/mngr_claude/imbue/mngr_claude/conftest.py` for the existing pattern with `mock_claude_test.py`).
- Verify text output for a single prompt, json output shape, stream-json output framing, stream-json input multi-turn flow.
- Verify the agent is destroyed by the end of the run (regardless of success/failure).
- Verify `--ensure-clean` is *not* applied (dirty tree allowed).
- Verify each rejected flag exits with code 2 and the expected error string.

### Release tests (`@pytest.mark.release`, `test_robinhood_release.py`)

- One end-to-end test that invokes the real `claude` binary via the plugin with a fixed simple prompt and asserts a non-empty assistant reply comes back. Skipped automatically on hosts without `claude` installed; CI provides it.
- Repeat for each output format to verify shape matches the real `claude -p --output-format X` on the same prompt.

### Manual verification before declaring complete

- `mngr robinhood "say hi"` produces a short text reply, exits 0.
- `echo "say hi" | mngr robinhood` same.
- `mngr robinhood "say hi" --output-format=json | jq .result` returns the reply text.
- Multi-turn via stream-json: `printf '%s\n%s\n' '{"type":"user","message":{"role":"user","content":"hi"}}' '{"type":"user","message":{"role":"user","content":"again"}}' | mngr robinhood --input-format=stream-json --output-format=stream-json` emits two assistant turns.
- Ctrl-C during a run leaves no `robinhood-*` agent in `mngr list`.
- Rejected flags (`-c`, `--max-budget-usd`, ...) exit 2 with a clear message.

## Open Questions

- **Live streaming via transcript polling vs. tailing claude's own stream-json stdout.** Polling `common_transcript/events.jsonl` at ~100ms is simple but lags claude's native streaming by a poll interval (and the transcript writer runs as a background script in `mngr_claude` — its own latency is unclear). Should we measure and switch to tailing `logs/claude_transcript/events.jsonl` (the raw claude transcript that mngr_claude already captures) if latency is unacceptable?
- **`--bare` interaction with `sync_home_settings=True`.** With `sync_home_settings=True` we inherit `~/.claude/` (skills, plugins, MCP). If the user *also* passes `--bare`, the spawned claude ignores all of that anyway — but we've still done the sync work. Harmless but wasted. Worth detecting and skipping the sync, or fine?
- **Synthesized `session_id` in the json envelope.** mngr's agent ID is not a UUID (it's `agent-...`), but `claude -p --output-format=json`'s `session_id` is a v4 UUID. Do consumers actually parse this as a UUID, or is any opaque string fine? Today's plan emits the mngr agent ID; revisit if it breaks parsers.
- **`--max-turns` enforcement.** Pass-through to claude works for the agent's internal turns, but mngr also has its own concept of turns (each `mngr message` is one). In stream-json input mode, do we treat each `mngr message` as a separate `--max-turns` budget, or apply it globally across the whole run?
- **What happens if claude reaches WAITING because it hit a permission gate we *didn't* auto-approve?** `auto_allow_permissions=True` should cover this, but if a user passes `--permission-mode=plan` it intentionally pauses for review. In `-p` mode this is nonsensical; should we forbid `--permission-mode=plan` or just let it hang?
- **Stop hooks from user `~/.claude/settings.json` firing inside the ephemeral agent.** A misconfigured Stop hook (e.g. one that itself calls `mngr` recursively) could deadlock the run. `sync_home_settings=True` matches `claude -p` semantics but inherits this risk. Do we want a runtime warning or just trust the user?
- **Concurrent invocations sharing a cwd.** Allowed, but two claude agents editing the same files simultaneously is a foot-gun even if `claude -p` itself has the same property. Worth surfacing a one-time warning, or out of scope?
- **Telemetry / mngr event logs in `~/.mngr/events/`.** Each invocation will leave per-agent state behind (per `preserve_sessions_on_destroy=True`). For high-volume scripted usage this could accumulate. Should we add a `--ephemeral` opt-out flag, or rely on `mngr gc` to clean up?
