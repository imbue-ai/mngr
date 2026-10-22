# Unabridged Changelog - mngr_claude

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_claude/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-22

Test-only: the test stand-ins for the host file reader accept the new optional `max_byte_count` argument of `read_file_tail_from_offset`.

Changed: `imbue-mngr-claude` depends on `imbue-overlay` instead of `overlay`. The PyPI name `overlay` belongs to an unrelated third-party project, so the old pin could never be installed from PyPI.

## 2026-10-20

A Claude agent whose turn end another Stop hook blocks (exit code 2, which sends Claude back to work on the hook's feedback) now stays RUNNING through the continuation. Before, `wait_for_stop_hook.sh` cleared the `active` marker once the other hooks exited, and since no hook fires when the continuation starts, the agent read WAITING while it was still working. The script now reads the session transcript the same way it already does for queued prompts: Claude Code writes the block's `Stop hook feedback:` record as soon as the blocking hook exits and the turn's `stop_hook_summary` only after every hook finishes, so a feedback record newer than the latest summary means the turn end was blocked. An interrupt (SIGTERM, SIGINT) and a StopFailure turn end still mark the agent inactive.

`mngr list` no longer reports a `plugin.claude.waiting_reason` field on agents of other types. The claude field generator ran for every listed agent, so a `command` or `codex` agent showed `END_OF_TURN` under `plugin.claude` (the marker it reads is one such an agent never writes), at the cost of one remote `path_exists` call per agent per `mngr list`. The generator now returns nothing for an agent that is not a ClaudeAgent, as the codex generator already did. Reported by 19 mappers in the 2026-10-13 TMR run.

## 2026-10-19

The Claude submission-confirmation probes no longer print "No such file or directory" when the agent's session transcript does not exist yet (an agent that has not had a user turn). The missing file was always treated as "keep polling", but the stray error became the only stderr in an unconfirmed send's diagnostics and pointed the investigation at the wrong cause.

Sending a message to Claude Code is faster, most noticeably on Imbue Cloud workspaces where every process spawn is slow. The send now uses the pane capture taken by the dialog preflight for the readiness and leftover-input checks instead of capturing again, dialog keypresses reuse the pane ID resolved for the send, and submission evidence is checked in Claude's own session transcript before mngr's raw copy, since Claude writes its own transcript first. Combined with the mngr changes, a send goes from 16 process spawns to 6.

## 2026-10-16

A claude agent's background tasks (activity tracking, transcript streaming, the common transcript converter and the response-streaming watcher) keep running after the agent is renamed, including after it restarts. The supervisor tracks the agent's tmux session by its session id, which a rename does not change, and new launch commands no longer carry the session name.

- An agent created by an older mngr keeps its supervisor script, which follows the session name it is given. `mngr start` rewrites that name out of its stored launch command, so after its next restart its background tasks run for its current session (and survive restarts); a live rename still ends them until the next restart.

- The background-tasks and response-streaming pidfiles are keyed on the agent id, so a new agent that takes a renamed agent's old name gets its own watchers.

## 2026-10-15

Fixed mngr leaving a claude agent permanently stuck in Claude Code's shell mode after a bare `!`.

Claude draws a dim hint inside an empty input box: `Try "how do I log an error?"` on a fresh
agent, `Press up to edit queued messages` while a message is queued, or the model's suggested
next prompt in normal mode. mngr read the hint as typed text, so it mistook the stranded
shell-mode box for a human's unsubmitted command and refused to touch it.

mngr now reads Claude's pane with its styling and drops the input box's dim hint text before
deciding what is typed there. The strand is recovered whichever hint is showing, and the
pre-send check no longer warns about a hint as leftover text. A human's draft is never taken
for a hint, even one that reads `Try "how do I log an error?"` word for word.

With colour suppressed for the agent (`NO_COLOR`, `FORCE_COLOR=0`, `TERM=dumb`), the hint is
not dim and reads as typed text: mngr refuses the send rather than backspace, and the pre-send
check warns about it.

A new test fails when the claude version `dialogs.py` was read from differs from the one the
mngr Dockerfile installs, so the binary drift checks cannot silently skip in CI.

## 2026-10-14

Claude agents report when they are compacting their context, so consumers such as the workspace chat app can show a status while a compaction runs.

- A new `PreCompact` hook writes a `compacting` marker to the agent state dir, holding `{"trigger": "manual"|"auto"|"unknown", "started_at": "<utc iso>"}`, and emits an activity event. It fires for a `/compact` and for Claude Code's own auto-compaction.

- A new `PostCompact` hook writes `last_compaction.json` (`{"trigger": ..., "ended_at": ...}`) to the agent state dir, removes the `compacting` marker, and emits an activity event. The summary is not copied; it stays in the transcript.

- Both hooks always exit 0, so a missing `jq`, an unwritable state dir or a malformed payload can never block or interrupt a compaction. A payload without a recognised `trigger` records `"unknown"`.

- The startup/resume `SessionStart` hook also removes a stranded `compacting` marker. A compaction cancelled with Escape, or one whose summarization request fails, fires no `PostCompact`, so the marker can be left behind; consumers should also treat an old marker as stale.

- Hooks are written only when an agent is created, so an agent created before these hooks existed never writes these files.

- Autocompact does not compact a Claude agent a second time right after a compaction. A compaction writes no usage record, so the newest usage still reports the pre-compaction total, over `min_context_tokens`. A `compact_boundary` record that is newer, in file order, than every assistant usage makes `get_context_tokens()` return `None`, and the agent is left alone until its next reply reports a real size. The boundary's own `postTokens` estimate is not used as the size: on 42 real compactions the next turn's usage was 2.0 to 8.4 times `postTokens` (42k to 101k tokens more), because the estimate leaves out the session's fixed overhead. The idle timestamp is unaffected, and a suppressed size is logged at debug level with the agent and transcript path. The new `is_compact_boundary_record` identifies the boundary record.

- Sending a message to a Claude agent whose `/compact` was cancelled does not turn the message into compaction instructions. Claude Code puts a cancelled `/compact` back into its input box, where appending the next message would start another compaction and lose the message. The send clears a leftover `/compact` (bare or with instructions) with Ctrl+E, Ctrl+U, confirms the input row is empty, and records a `cleared_restored_input_text` delivery event. Any other leftover draft is kept and warned about, and so is a `/compact` the keys fail to clear.

- `ClaudeAgent.request_compaction` supports the new bounded message-lock wait and idle re-check: it takes the lock itself (raising `MessageLockTimeoutError` when the wait runs out), skips with `AgentNoLongerIdleError` when the agent is no longer idle since the expected moment, and only then sends `/compact`. A skipped compaction sends nothing and records nothing.

The autocompact eligibility check reads a Claude agent's idle timestamp and context token count from one backward scan of the transcript's tail, cached between checks, rather than parsing the whole transcript once per value. An unchanged transcript costs a size probe that re-reads none of its complete lines. A check reads tens of kilobytes, where whole-transcript parsing peaked at about 62 MB of memory for a 6.5 MB transcript and about 500 MB for a 52 MB one. The values found match a whole-transcript parse, except that a final line without a trailing newline is read once its newline arrives, and a record containing U+0085, U+2028 or U+2029, which `str.splitlines()` splits apart, is read whole. Removed the whole-text parsers `extract_latest_assistant_timestamp_from_jsonl` and `extract_context_tokens_from_jsonl`; added the per-record `extract_assistant_timestamp_from_record` and `extract_context_tokens_from_record` and `ClaudeCompactionTranscriptScanner`.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

Fixed the modal claude-provisioning release test, which looked for "Claude installed successfully" in the create output. The message interpolates the binary name, so it reads "claude" -- the capitalised form the test asserted on is never emitted, and the check could only fail.

Fixed the modal clone release test, which failed with "No agent type provided". `mngr clone` forwards its arguments to `create`, which resolves the agent type from an explicit flag, a positional, or a config default -- it never reads the type off the source agent. The test sandbox sets no default, so the type is now passed explicitly, matching the create helpers alongside it.

## 2026-10-12

The claude message-delivery release test no longer fails its unknown-slash-command step when Claude is still busy with an earlier turn. Claude queued the command, and the enqueue confirmed the send before Claude rejected it, so no rejection event was recorded; the step now waits for Claude to finish its earlier turns before sending the command. The test is also marked flaky.

Claude Code's offer to install a language-server plugin ("Would you like to install this LSP plugin?") is now switched off in the Claude config mngr seeds for every agent, in both the shared and the per-agent config modes. Claude Code makes that offer whenever it edits a file whose server binary is on PATH, a tmux-driven agent cannot answer it, and a person accepting it once installs the plugin for the whole config dir, which then starts a language server of several hundred MB behind every later session.

## 2026-10-07

Move the dialog registry's `CLAUDE_CODE_VERSION` snapshot from 2.1.280 to 2.1.293, the new pin in the shared mngr Dockerfile. Every dialog pattern and option label in `dialogs.py` was re-checked against the 2.1.293 binary, and all of them still appear in it, so no pattern changed.

Since 2.1.281, Claude Code shows its permission prompt for a recursive `rm` on command-substitution output (such as `rm -rf "$(pwd)"`) even under `--dangerously-skip-permissions`, and its first option approves the command. A new test pins how mngr handles that prompt, using a pane captured from a live 2.1.293 session. The prompt's `Esc to cancel` footer makes it a benign dialog, so mngr presses Escape, which denies the command. It never reaches the unknown-dialog fallback, which would press "1" and approve it, even when `sensibly_deal_with_dialogs` includes `ALL_KNOWN_AND_UNKNOWN_DIALOGS`.

## 2026-10-06

Claude's streaming output now reaches the stream buffer in terminals where Claude draws the content-block marker as U+23FA BLACK CIRCLE FOR RECORD rather than U+25CF BLACK CIRCLE.

The watcher recognised only U+25CF, so in those terminals no pane line was ever assistant text: region extraction returned nothing on every poll, the buffer kept its id line and never gained a body, and streaming was silently off for the agent's whole life. Which glyph Claude uses depends on the terminal it detects, not on its version, which is why this affected macOS while Linux was unaffected.

## 2026-10-05

Threads this plugin starts on a `ConcurrencyGroup` now go through `start_mngr_thread`, which destroys the thread's gevent Hub when it finishes. A thread that ran a host command used to leave its Hub, and the pipe pair it holds, open for the rest of the process's life. A new `test_prevent_raw_start_new_thread` ratchet keeps raw `start_new_thread` calls out.

## 2026-10-02

- The offline `on_before_host_destroy` test now builds its volume-backed host from mngr's generic mock provider instead of the docker provider, which moved into its own plugin. No behavior change.

## 2026-10-01

`anthropic` 1.3.0 adds a `toolset_name` field to `ToolUseBlock`, so mngr's stream-json dump now carries `"toolset_name":null` alongside the `"caller":null` it already emitted. Added to the list of known wire-shape departures from the real `claude` binary in the `stream_json` module docstring; the only consumer is mngr's own lenient parser, which accepts the field present, absent, or null.

pytest 9.1 no longer honours `pytest_plugins` outside the rootdir conftest, which is how this package borrowed mngr_modal's Modal test fixtures whenever the suite is run from the monorepo root. It now calls mngr_modal's `register_modal_test_fixtures`, mirroring the existing `register_plugin_test_fixtures`.

## 2026-09-30

Removed the 20 comment lines that had accumulated inside `libs/mngr_claude/imbue/mngr_claude/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

A Claude chat that has never been messaged no longer costs a `find` process every second. Claude only writes a session's transcript file after the first message, and until then the transcript poller (`stream_transcript.sh`) searched the whole `projects/` tree once per poll cycle, indefinitely. On a busy 4-vCPU workspace, two such idle chats had each been doing this for ~14 hours, at ~10% of a core apiece. The lookup is now a shell glob over `projects/*/<session-id>.jsonl`, the layout Claude uses, and it spawns no process. A session's file is still picked up on the first poll after it appears.

## 2026-09-27

Record agent compaction even if sending the `/compact` command raises an exception. This prevents `mngr autocompact` from repeatedly attempting compaction when compaction requests fail.

Marked `test_provision_prompts_for_all_dialogs_when_interactive` flaky: it runs a real local-provider provision and timed out once on an overloaded CI sandbox; no behavior change.

## 2026-09-25

A claude agent no longer reports WAITING for a whole turn that began with a queued prompt.

Claude Code fires UserPromptSubmit, which sets the `active` marker, when a prompt is queued. That happens mid-turn, or during the roughly 3 seconds the previous turn's Stop hook takes to finish. The Stop hook then cleared the marker, so the queued prompt's turn ran with the agent reporting WAITING, and consumers such as the workspace chat app's activity indicator showed it idle. This almost always hit a new chat's first message, which arrives right after the welcome greeting. The Stop hook now checks the session transcript for a queued prompt that is about to start the next turn, and leaves the marker in place when it finds one. The same holds when the previous turn ended on an API error (StopFailure), where Claude Code starts the queued prompt without waiting for the hook. A queued slash command such as `/clear` or `/compact` does not count, so the agent still goes idle when one of those is all that is queued. Claude Code queues a background task's completion notification the same way as a prompt, so an agent also stays RUNNING into the follow-up turn such a notification starts when it arrives before the turn ends.

## 2026-09-24

Move the dialog registry's `CLAUDE_CODE_VERSION` snapshot from 2.1.269 to 2.1.280, the new pin in the shared mngr Dockerfile. Every dialog pattern and option label in `dialogs.py` was re-checked against the 2.1.280 binary before the constant moved: all of them still appear in it verbatim, so no pattern changed.

The two drift guards that do that checking (`test_patterns_match_installed_binary` and `test_option_labels_match_installed_binary`) now also look for the pinned build where `claude.ai/install.sh` keeps it, `~/.local/share/claude/versions/<version>`. They named only the npm install path, so on every host provisioned by install.sh -- the mngr Dockerfile that offload CI runs them in, the workspace image and a developer laptop -- they silently skipped, which is the exact failure mode they exist to prevent. They still skip when the pinned build is not installed, rather than checking whichever claude is on PATH.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-17

Test doubles that implement the host file-write interface (`FakeHost` subclasses in the compaction and skill-agent tests) now accept `write_text_file`'s `is_atomic` parameter, matching the interface: `write_file` and `write_text_file` are atomic by default on every host and take `is_atomic=False` to write in place. No behavior change in this project's own code.

## 2026-09-15

Hardened `stop_watcher`, the helper in the `common_transcript.sh` tests that shuts down a
background watcher started by a test.

The watcher (`common_transcript.sh`) converts a Claude transcript by running a Python converter
as a child process, and the tests start it in its own process group so one signal reaches both.
A lock directory, the convert lock, stops two conversions from running at once. `stop_watcher`
used to send SIGTERM to the group, wait for the watcher's bash process, and then delete the lock
directory. It now sends SIGKILL instead. SIGKILL cannot be caught or ignored, so deleting the lock
directory afterwards no longer depends on nothing in the group handling SIGTERM. Nothing does
today, so this is a hardening rather than a bug fix: with the scripts as they are, the old
sequence also killed everything in the group.

The kill is sent before the wait. Waiting reaps the bash process and frees its pid for reuse, and
that pid is also the process group id, so a kill sent after the wait could hit an unrelated group.

Added a test that stops a stand-in watcher whose child ignores SIGTERM and checks that nothing in
the group is left running once `stop_watcher` returns.

Tests only. No change to the shipped script or to any agent's behaviour.

The mngr_claude behavior corpus is witnessed by `mngr witness` instead of `mngr tmr-behaviors`: the project's prompt variant moves from `tmr/behaviors_mapper.j2` to `witness/mapper.j2`, where the new command discovers it, and every corpus README's incipit now names the `behaviors` skill.

A claude agent whose turn ended on an API error -- a usage limit, a rate limit, a prompt too long -- now reports WAITING instead of staying RUNNING forever.

Claude Code has two mutually exclusive turn-end paths. A turn that finishes normally fires `Stop`; a turn that dies on an API error fires `StopFailure` and returns before the `Stop` pass ever runs. mngr registered only `Stop`, so the `active` marker that `UserPromptSubmit` created was never removed and the lifecycle probe kept reporting the agent as RUNNING until its claude process restarted. Nothing else cleared it: the usage-limit selector fires no `PermissionRequest`, and Claude Code suppresses its `idle_prompt` notification while a dialog is on screen or quota auto-resume is armed.

The effect was visible anywhere lifecycle state is read. In the minds app, a workspace whose update agent hit a usage limit sat on "Preparing update..." indefinitely, and the wedged run slot refused any retry.

`StopFailure` now runs the same turn-end script as `Stop`, so the transcript flush still lands ahead of the cleared marker and a consumer woken by the turn-end signal sees the error that ended the turn. Agents pick this up when they are next provisioned.

## 2026-09-14

Implement context compaction support for Claude Code agents, including `ContextCompactionConfig` settings, idle time tracking via stop hooks and assistant transcript timestamps, token usage extraction, filtering synthetic compaction summaries from common transcripts, and passing optional instructions to `/compact <instructions>`.

## 2026-09-13

Marked `test_running_watcher_defers_the_open_trailing_inference` as a known flake so a single
failure stops turning CI red while its cause is still open (tracked as MIND-263).

The cause is measured but not fixed. The test drives the real `common_transcript.sh` watcher and
then runs a turn-end flush through a helper that allows the flush 10 seconds. When the flush has
to wait for the convert lock, the script waits its lock timeout, gives up, waits its retry delay,
and tries once more -- 60.76s end to end on the shipped 30s default, measured. The helper kills it
long before that, so contention surfaces as `subprocess.TimeoutExpired` rather than the skip the
script performs.

Also corrected the `stop_watcher` docstring, which claimed the lock directory is only cleared once
the watcher's process group is dead. Only the bash parent is waited for; the converter shares that
group and can outlive it, so the lock can be cleared while a live converter still holds it.
Escalating to the group after the parent has been reaped is not the fix -- its pid is available for
reuse at that point, so the signal could reach an unrelated process group.

Tests only -- no change to the shipped script or to any agent's behaviour.

## 2026-09-12

The interactive claude agent now captures its own stderr to `stderr.log` in the agent's
state directory. Agents run under tmux rather than supervisord, so a startup error or
crash reached none of the workspace's service logs, and unlike codex, antigravity and
opencode this harness wrote no diagnostic file of its own -- a bug report had no way to
see it. The TUI still renders on stdout, so the pane is unchanged; the file is truncated
per launch, which bounds it without rotation. The bug-report collector picks it up with
the rest of the agent's logs.

Note the tradeoff: stderr now goes to the file instead of the pane, so a crash message
that used to stay in the pane's scrollback is no longer visible there. It is still
collected -- from the file, and durably, which the pane was not.

The capture wraps the whole resume/resume-uuid/create fallback chain rather than each
branch, so a branch that fails keeps its stderr instead of having it truncated by the
branch that follows -- that output is exactly why the fallback happened.

The interactive claude agent's stderr now reaches both the pane and the agent's `stderr.log`, instead of only the file. The previous change captured stderr to the file for bug reports but took it off the pane, so a crash message was no longer visible where someone looking at a dead agent expects it. The launch chain now redirects stderr through `tee` (via shell process substitution), which keeps stdout and the TUI untouched, preserves the resume/resume-uuid/create fallback chain's exit status, and truncates the file per launch as before. `tee -i` is used so a Ctrl-C the harness handles itself cannot kill the copy and leave claude writing to a broken pipe.

This makes the agent pane's shell a bash/zsh/ksh requirement (process substitution is not POSIX), which every shipped image and both desktop platforms already satisfy.

## 2026-09-11

Move the dialog registry's `CLAUDE_CODE_VERSION` snapshot from 2.1.227 to 2.1.269, the new pin in the shared mngr Dockerfile. Every dialog pattern and option label in `dialogs.py` was re-checked against the 2.1.269 binary before the constant moved: all of them still appear in it verbatim, so no pattern changed.

Claude agents no longer open Claude Code's diff sidebar: `diffSidebarOpen` is now written as `false` into the per-agent `.claude.json` that mngr provisions for every agent with an isolated config dir (remote, deploy, and local agents with `isolate_local_config_dir=true`, the default). Shared-mode local agents (`isolate_local_config_dir=false`) use the user's own Claude config and are unchanged.

Unattended (remote/deploy) Claude agents also get two more `settings.json` flags that stop Claude Code from interrupting automated input and output: `feedbackDrafts` is set to `"off"` and `feedbackSurveyRate` is set to `0`, which disables the session quality survey. Attended local agents are unchanged.

## 2026-09-01

The claude common transcript now emits the ATIF-shaped stream records described in `specs/atif-transcript-alignment/spec.md`, replacing the bespoke `user_message` / `assistant_message` / `tool_result` records. Every stream starts with a `header` line pinning the ATIF revision, and each following line is a `step` or an `observation`.

The stream is now full fidelity: tool `arguments` are the complete input objects (no more 200-char previews), tool outputs and stop-hook text are untruncated (no more 2000-char cap), and the model's thinking is captured as `reasoning_content`. Display truncation lives in `mngr transcript` instead, with `--full` to disable it.

Claude fans one API response out over several transcript lines; those lines are now grouped by their shared `message.id` into a single agent step, so one step is one LLM inference and its token usage is counted once instead of once per line. Because those lines arrive over several seconds, the converter holds back the last inference until a later line proves it finished; the turn-end flush (`--single-pass`) knows the turn is over and emits it.

Framework-injected `isMeta` records (stop-hook output, local-command caveats) are now real system steps rather than fake `meta` tool results, and a context compaction is recorded as a system step carrying the ATIF v1.7 `context_management` convention with the summary on its inline observation.

Each agent step also records `llm_call_count: 1` (one step is one inference by construction), and a native Task subagent's records -- which claude interleaves into the same session file with `isSidechain: true` -- are grouped in their own lane and marked `is_sidechain` in the step's (or observation result's) `extra`, so a subagent's turn can no longer split the main thread's inference in two.

The trade-off of holding the last inference back: mid-turn, the newest in-flight inference appears in the common transcript only once it is provably complete (the next record that closes it, or the turn-end flush), so a live `mngr transcript --follow` tail lags the agent by up to one inference.

The claude silent-decode ratchet entry now counts `common_transcript_convert.py`'s decode catches (with the justification recorded alongside them) instead of excluding the whole file, matching its codex and antigravity siblings. The file stays waived only for the rules that genuinely do not apply to a standalone stdlib-only script (bare print, `__init__` methods).

A claude release test that still counted user turns by the retired `user_message` record type now counts ATIF user steps.

The `emit_common_transcript` config field's description now describes the ATIF-shaped stream (user turns, agent turns, tool results) rather than the retired record vocabulary.

Trimmed historical framing from the converter's module docstring.

The ATIF stream header's `event_id` is now unique per agent stream: `header-<sha256(agent_id:emitter)[:32]>` instead of the fixed `"header"`. A fixed id repeats identically for every agent on every host, so analytics' fleet-wide event-id dedupe collapsed all header rows to one per account, destroying the per-agent (emitter, `schema_version`) mix.

The agent id is the agent state directory's basename (a UUID4-based value), so distinct agents never collide and a restart reuses the same id.

The claude common-transcript emitter now writes ATIF-shaped stream records (`header` / `step` / `observation`) per `specs/atif-transcript-alignment/spec.md`: full-fidelity tool arguments and outputs, thinking captured as `reasoning_content`, one step per LLM inference (multi-line API responses grouped by `message.id`), sidechain lanes for native Task subagents, compactions as `context_management` system steps, and a per-agent-unique header `event_id`. The per-layer `mngr-atif-*.md` entries in this PR carry the details.

## 2026-08-30

The Claude-agent behavior corpus's per-folder `overview.md` files are renamed to `README.md`, each now opening with the mandated incipit that points any reader to the tmr-behaviors skill. Their prose is otherwise unchanged.

Marked `test_assemble_command_is_posix_compatible` as `@pytest.mark.flaky` with a 60s timeout: like its mngr_codex sibling (observed timing out under CI sandbox load), it shells out to shellcheck and is exposed to the same default 10s pytest-timeout.

## 2026-08-26

Sends to a claude agent no longer fail when something is holding the TUI's input.

A dialog left open in the terminal — the model bar's switch confirmation, a plugin-install offer, anything claude puts in front of the prompt — used to abort every subsequent send. The mechanism that was supposed to clear it pressed Enter `auto_accept_prompt_depth` times, and that defaults to `0`: mngr detected the dialog and then did nothing about it.

`mngr_claude.dialogs` replaces it. Preflight now classifies whatever owns the pane and deals with it: Escape for anything Esc closes harmlessly, Backspace for a shell-mode strand, or — for a confirmation whose nickname the operator listed in the new `sensibly_deal_with_dialogs` — cycling the selector onto a *named* option before pressing Enter. Anything else refuses the send with an error naming what is in the way.

The new field takes nicknames from `mngr_claude.dialogs.SELECTABLE_NICKNAMES`, or the single wildcard `"*"`. It never presses Enter on whatever option happens to be highlighted: the pane is shared with anyone attached to the tmux session, so a stray arrow key — or a claude release that reorders the options — would otherwise turn "confirm the switch" into "buy credits". If the named option is not reachable, the send refuses rather than guessing.

Shell mode goes through the same loop rather than sitting beside it as a special case. A bare `!` strand self-clears with Backspace exactly as a dialog self-clears with Escape; a human's unsubmitted `!<command>` refuses the send with the actionable error PR #397 established. One classifier, one loop, no carve-outs.

Removed: `auto_accept_prompt_depth`, `auto_accept_preflight_prompt_depth`, `_accept_dialogs_up_to_depth`, and the `DialogIndicator` family. One of those indicators matched a string absent from every version of Claude Code we have ever shipped, which nothing noticed across two version pins — the new module carries a release test asserting each pattern still matches the installed binary.

Renamed `auto_dismiss_dialogs` to `auto_dismiss_dialogs_at_startup`, in every plugin that has it. The name described what it does — dismissing the dialogs that appear before the agent is up — but read as though it governed dialogs generally, which is now a different setting.

The dialog patterns are labelled with the claude build they were read out of, and every one was re-verified against the currently pinned 2.1.227 — including the four option labels the answerable dialogs cycle onto, since a stale label refuses a send rather than answering it.

Fixed dialog and shell-mode detection in conversations that have prior turns. Claude Code prefixes every echoed past user message with the same `❯` glyph that marks the live input box, so searching the whole pane for it reported "ready to type" however the pane was actually occupied -- a message sent while the settings window was open was typed into that window's search field, and a message sent while another surface held the pane was appended to a past turn and then failed 90 seconds later as unconfirmed. The input box is now identified by its position at the bottom of the pane, and dialogs and shell mode are recognised on their own evidence before the input box is looked for, so a short dialog with a user turn echoed just above it is still found.

Because detection had been silently failing, errors that were unreachable now appear: an open `/config`, `/model` or `/theme` at send time is reported instead of swallowing the message, and a human's unsubmitted `!<command>` is reported instead of timing out.

`ShellCommandPendingError` was removed. Nothing caught it and its recovery wording already lives on the shell-mode dialog itself, so there is now a single error for "something is holding the input".

Shell mode is recognised by the input row itself rather than by whether a prompt glyph appears near it. In a short conversation the echo of a past user turn falls close enough to the bottom that "a prompt is nearby" is true while shell mode is holding the input, which would have let a send walk into it. Shell mode is also checked ahead of the dialog surfaces, as it was before, and the error naming an unsubmitted command still tells you which keys resolve it and which command connects you to the agent.

Shell mode is now recognised by the agent's input row rather than by whether a prompt glyph appears anywhere near the bottom of the pane. In a short conversation the echo of a past user turn sits close enough to the input to be mistaken for it, which would have left shell mode undetected there for the same reason dialogs were undetected before this change. Shell mode is also checked ahead of the dialog surfaces again, as it was originally.

A pane that has not finished painting is now waited for rather than refused. A new agent's first message is delivered as soon as Claude Code signals that its session started, which happens before its interface has drawn, so checking once and giving up would have failed the opening message of every new agent -- including the workspace's welcome chat. A pane that stays unreadable is still refused.

A dialog found while an agent is starting is reported rather than dismissed, which is how it behaved before this work. Startup is not send time: the dialogs cleared at send time are ones a message has to get past, whereas one that stopped an agent from starting is worth surfacing, and pressing keys at an interface that never signalled readiness is guesswork. Known startup dialogs are still pre-dismissed by configuration before launch, unchanged.

`sensibly_deal_with_dialogs` now takes a named token in place of a wildcard. `ALL_KNOWN_DIALOGS` covers every dialog mngr can name, each answered on the option named for it, so it grows with the catalogue without ever becoming a guess. The `*` wildcard is gone: it invited being written to mean "all the ones you know", and a token that says which dialogs it covers cannot be misread that way. A dialog mngr cannot name is refused whatever this list says -- answering one would mean guessing at an option nobody has looked at, which would be a separate feature with a flag of its own. The module now says benign and non-benign throughout, since whether mngr dealing with a surface can be regretted is the axis the whole design turns on.

`sensibly_deal_with_dialogs` no longer accepts the token for answering surfaces mngr cannot name: nothing implements that yet, and accepting it promised behaviour that silently did not happen. The pre-rename `auto_dismiss_dialogs` name is rejected as an unknown field rather than aliased -- an alias could not have worked, since agent types are built with `model_construct`, which skips the validators an alias needs.

mngr no longer presses keys at a pane that is not showing a selector. `UsageLimitReached`'s caption is also the option it answers, so an agent merely explaining the usage limit put that text on screen -- and the option-cycling walk would then press Down a dozen times into whatever held the input, which for a live composer meant a dozen keystrokes nobody typed. Dismissing a dialog also leaves copy-mode first, since a pane in a mode shows scrollback rather than the live screen and would be read as stuck.

A blocked send now says what is wrong once. The error was assembled by four layers that each added a sentence, so a user reading a single modal was told "the send failed" three times and "go fix it in the terminal" three more: a generic preamble with the internal dialog nickname in parentheses, then the dialog's own advice, then a `mngr connect` command, then the client's own paraphrase. The dialog's advice is now the whole reason, verbatim. How to REACH the terminal is left to the client, since naming a CLI command is wrong in a window that has a terminal tab, and the sentence before it already said to use the terminal.

`sensibly_deal_with_dialogs` gains `ALL_KNOWN_AND_UNKNOWN_DIALOGS`. It is everything `ALL_KNOWN_DIALOGS` covers, plus a fallback for a surface mngr cannot name: press `1`. That last part is a guess and is meant to read as one -- an unnamed dialog's first option is unknown by definition, and among the four dialogs mngr does name it would have been the wrong answer for two, since the usage-limit prompt's first option buys credits and the LSP prompt's installs. It is a single digit rather than a walk onto "option 1" because locating that option means reading the highlight against a pattern pinned to `1.`, and a highlighted `11.` row contains `1.` -- so the walk could land on the wrong row and press Enter on it. Two things bound the guess: the digit is only sent to a pane that actually shows a numbered selector, so a "1" is never typed as text into whatever holds the input; and pressing is not succeeding, since the caller re-classifies afterwards, refuses the send if the pane did not change, and gives up after its pass budget. Opting in is only possible through the token -- listing the nickname `Unrecognized` grants nothing, because opting into guessing should be a deliberate act rather than something a broader-sounding setting quietly includes.

Deleted `specs/claude-send-path-input-ownership/escape-then-send.md`. It was the planned walkthrough written before the work, and `spec.md` covers the same subsystem in full; keeping both left two documents for one design, and the stale one said the input region was the last 8 non-blank lines where the code says 12.

## 2026-08-25

Comment-only terminology sweep: mngr-level prose speaks host/agent instead of the minds-level workspace vocabulary.

Merged the machine/workspace naming cleanup branch (`mngr/fix-machine-workspace-naming`, PR #436) into this branch and resolved the merge conflicts against main. The changes themselves are described in this project's `mngr-fix-machine-workspace-naming` changelog entry.

## 2026-08-19

Make the Claude plugin stop importing the heavy `anthropic` SDK at `mngr` startup, reducing CLI cold-start latency (MIND-179). `mngr`'s console script imports every plugin entry module on every invocation, including `imbue.mngr_claude.headless_claude_agent`, which transitively imported `imbue.mngr_claude.stream_json` -- and that module pulled in the `anthropic` SDK (~900 modules) just to register the agent type.

The `anthropic`-typed stream-json boundary (the emit-side event builders and the consume-side typed validators, including the static exhaustiveness tripwire over the stream-event union) now lives in a new `imbue.mngr_claude.stream_json_impl` module that imports `anthropic` at top level. `imbue.mngr_claude.stream_json` is now a thin, SDK-free facade: importing it never pulls `anthropic`, and every public function forwards to the implementation, which is imported lazily on first call. The SDK now loads only when a Claude stream is actually produced or consumed. Measured locally, `from imbue.mngr.main import cli` dropped from ~4860 to ~2690 imported modules with zero `anthropic` modules (previously ~890). This also removes the same SDK cost from the `robinhood` plugin's startup path, which reached `anthropic` through the same module.

No user-facing behavior change. The public functions keep identical signatures and behavior on `imbue.mngr_claude.stream_json`, except that the four consume-side functions returning or accepting `anthropic` types (`validate_stream_event`, `classify_stream_event`, `parse_stream_event`, `parse_assistant_message`) are annotated `Any` on the facade; import them from `imbue.mngr_claude.stream_json_impl` when the precise static types are wanted.

## 2026-08-17

The claude agent type gains two settings, `output_style` and `append_system_prompt`, that a
create template can set so a role describes claude's behaviour without spelling out argv.

`output_style` becomes the `outputStyle` setting in the managed settings claude launches with,
applied after `settings_overrides` so a role's style wins over a configured one without
disturbing the other resolved keys (model, fastMode, and the rest). The name is validated during
provisioning against `.claude/output-styles/` in the work dir -- the same directory claude itself
reads -- so a misspelled name or a broken symlink fails the create instead of producing an agent
that launches silently unstyled.

`append_system_prompt` is a list, so `append_system_prompt__extend = [...]` in a template makes
each stacked role contribute a block. The blocks are joined into ONE `--append-system-prompt`
launch flag: claude's flag is last-wins (verified against 2.1.220), so passing it per block
would deliver only the final one and silently drop every role stacked before it. Create
templates that previously spelled this out as `agent_args = ["--append-system-prompt", "..."]`
can use the setting instead.

The `model_state_hook.py` Claude Code hook (and its SessionStart/UserPromptSubmit/PostToolUse/Stop
registrations) is removed. The chat model bar's live model/effort/fast now comes from the
workspace's statusline command, which Claude Code re-runs on every session start, assistant
message, and refresh tick -- strictly more reactive than the hook (which could not see idle
`/model` switches) and immune to the `<synthetic>` model ids the hook could record from
framework-generated transcript messages. The statusline writes the harness-uniform
`$MNGR_AGENT_STATE_DIR/model_state.json` snapshot that the system interface reads for
every harness.

Claude provisioning now installs a Chat-only `meta+q` -> `chat:cancel` keybinding that the
Minds workspace UI uses to flush a claude agent's queued messages into its live turn without a
SIGKILL-restart. `ensure_chat_cancel_tap_keybinding` merges the chord into the user-scope
`keybindings.json` (creating the file/entry when absent); it is idempotent and never clobbers a
`meta+q` already bound in the Chat or Global context, so a user's own binding wins and the tap
simply reports itself unavailable. In shared-config mode claude reads the file directly; in
isolated mode the per-agent config dir inherits it via the existing keybindings sync.
`is_tap_binding_active` reports whether the chord is live for the running claude process (bound
on disk, and written no later than the process-start marker, since claude reads keybindings only
at launch).

Added `mark_claude_agent_idle`, a small primitive that clears a claude agent's `active` and
`permissions_waiting` markers and emits one activity event, out-of-band. It runs the exact shell
snippet the Notification / SessionStart / Stop hooks already run, so the marker semantics and the
activity-event format have a single source of truth. The Minds workspace stop button calls it
after a native (chord) interrupt: claude fires no hook when the user interrupts a turn, so the
`active` marker is otherwise stranded and the agent keeps reporting RUNNING for ~60s; clearing it
drops the activity indicator immediately and the emitted event pokes `mngr observe` to re-probe.

Renamed the agent's live model-state file from `minds_model_state.json` to `model_state.json`. The plugin has no reason to be aware of Minds -- Minds is one client of this file, not what it is for -- and the name is now the plain description of its contents. The pi lifecycle extension's copy is renamed to match. Agents created before this write the old name until their next model change, so a client reading the new name sees no selection until then; nothing else depends on it.

Seed the `mngr_claude` behavior corpus (`libs/mngr_claude/behaviors/`) with the message-delivery area: the `stays-ready` invariant and the `shell-input.feature` scenarios specifying how `!`-prefixed (bang) messages behave when delivered to a Claude agent.

Fix a `!`-prefixed message stranding a Claude agent in shell mode. mngr delivers a leading `!` as a literal keystroke, which flips Claude Code into shell (bash) mode -- the input row's `❯` prompt becomes `!` and the footer reads "! for shell mode". Two problems followed: a `!` command was confirmed under the strict policy (whose evidence probes never match a shell-mode submission, so the send hung for the full window and then failed), and a bare `!` left the agent stuck in shell mode (submitting an empty shell line is a no-op), hiding the `❯` prompt that readiness keys off.

The Claude plugin now recognizes `!` shell-command messages: it confirms them under the relaxed policy (they leave no durable submission record, like slash commands), and after delivery it leaves an empty shell-mode line (a single Backspace) so the `❯` prompt returns. As a result, `!echo <text>` runs the command and its output appears in the conversation, and a bare `!` is inert and leaves the agent ready for the next message.

The plugin also distinguishes a non-empty shell command a human left unsubmitted from that auto-recovered empty case: because mngr always submits its own sends with Enter, a pending `!<command>` on the input row can only be one a human typed directly and did not submit. Rather than finish or discard it, mngr refuses the send at preflight with an actionable error (connect and press Enter to run it or Escape to cancel) instead of a generic readiness timeout. This is specified by the `pending-shell-command-blocks` behavior and its witnessing test.

Add the witnessing tests for the `mngr_claude` behavior corpus (`libs/mngr_claude/behaviors/`, the message-delivery area), generated by a `mngr tmr-behaviors` map-reduce run with the new `libs/mngr_claude/tmr/behaviors_mapper.j2` prompt variant (an additive extension of the packaged `behavior_mapper.j2`, mirroring the minds variant). Both `shell-input.feature` scenarios (`runs-command`, `bare-bang-inert`) are witnessed to FULL by e2e release tests driving a real Claude Code TUI; the `stays-ready` invariant is witnessed across four message instances with an honest partial note (the universal quantifier is untestable in kind).

- `ensure_chat_cancel_tap_keybinding` no longer crashes on a keybindings.json whose Chat entry has a non-object `"bindings"` value: the malformed value is replaced with a fresh dict (with a warning logged), mirroring the existing handling of a non-list top-level `"bindings"`. Added a unit test for the non-dict case. This also fixes the type error the old `setdefault` call produced.

- `is_tap_binding_active` now logs a warning when keybindings.json is malformed JSON instead of silently returning False (still never raises), satisfying the silent-decode-error ratchet.

- Sorted imports and reformatted `claude_config.py` / `claude_config_test.py` with ruff.

- Provisioning seeds minds_model_state.json from the launch settings (model, fastMode) so the chat model bar is populatable the moment readiness fires; the statusline's later writes reconcile the seed to claude's self-reported values.

- Claude common-transcript converter: tool results converted in a later pass than their call are now labeled with the real tool name instead of "unknown" (the call-id-to-name map is built from the full input before the cross-pass dedup skip).

- Claude common-transcript converter: assistant messages containing only thinking/redacted_thinking blocks no longer emit empty assistant records (which readers rendered as "(no content)" turns).

- Claude common-transcript converter: slash-command plumbing records (`<command-name>`/`<command-message>` expansions, `<local-command-stdout>` output, and the isMeta `<local-command-caveat>` wrapper) are filtered out instead of leaking into the transcript as fake user turns and "meta" tool results. Genuine turns that merely quote the markup mid-text are unaffected. All three fixes are covered by tests driven by a scrubbed fixture captured from real claude-code 2.1.207 sessions.

- New U5 enforcement test (`resources/common_transcript_diff_test.py`): a native-vs-common transcript diff that independently enumerates every user-visible turn in the real claude-code 2.1.207 session fixture -- with every legitimately-excluded shape (bookkeeping record types, slash-command plumbing, thinking-only messages, isMeta framework injections) named in explicit allowlists -- and asserts the converted common transcript contains exactly those turns, exactly once, in order, with tool results paired to their calls under the real tool names. Unlisted native shapes fail the enumeration, so a future binary's new record shape (or a converter filter that broadens to swallow genuine turns) fails loudly instead of shipping green on schema validity alone. Also asserts reconverting the same session appends nothing.

## 2026-08-13

`mngr list` now reports a Claude agent blocked on a permission dialog or an `AskUserQuestion` as `WAITING` rather than `RUNNING`.

The plugin already treated such an agent as waiting, but only for the reader `mngr wait`/`message`/`find` use; the agent listing reads the lifecycle probe directly and never saw it, so `mngr list` showed `RUNNING` for an agent that was making no progress.

The plugin now answers `is_blocked_on_dialog()` and the probe folds that into the state it builds, so both readers agree.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Reduced the idle CPU cost of the per-agent transcript watchers, which polled continuously whether or not the agent was producing output.

`common_transcript.sh` now skips a conversion pass entirely when the raw transcript has not changed since the last completed pass. Each pass spawns a Python interpreter that re-reads and re-parses both the whole input transcript and the whole output file, so on an idle agent that work was repeated every 5 seconds for no result. Measured on a real 12MB transcript, steady-state idle cost dropped by roughly 93% (56 to 4 CPU ticks per 20 seconds) with identical converted output. The change-detection state is held in memory only, so a restart still performs one full reconciling pass exactly as before, and a pass skipped because a concurrent `--single-pass` flush holds the convert lock is retried on the next cycle rather than being lost.

`stream_transcript.sh` now counts the session-history file and every resolved session file in a single `wc` invocation per poll, instead of one invocation per session per poll. The previous cost grew with the number of sessions an agent had been resumed through and never shrank, since the session history is append-only. Idle cost is now roughly flat in session count: measured over 20 seconds, 5 sessions dropped from 53 to 18 CPU ticks and 15 sessions from 133 to 38.

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-29

Fix a class of claude agent restart deadlocks where a stale `claude_session_id` marker stranded the launch: the tmux window died with "No conversation found" followed by "Session ID ... is already in use", and the agent could never start again.

- The launch command's resume gate now checks the exact session file claude consults on `--resume` (the project dir encoded from the agent's work dir) and requires it to be non-empty, instead of matching any same-named file anywhere in the config dir (which passed for sessions claude cannot actually resume, e.g. one started from another cwd, and for blank session files). A `find` over `projects/` remains as a backstop in case the project-dir encoder ever diverges from Claude Code's.

- The launch chain gained a self-healing middle branch: when the tracking-file session is unresumable, the agent explicitly resumes its own first session (`--resume <agent-uuid>`) instead of falling straight through to `--session-id <agent-uuid>`, which always fails with "already in use" once that session exists. Fallback branches re-export `MAIN_CLAUDE_SESSION_ID` so the SessionStart hook heals the tracking file.

- The mngr readiness hooks no longer accept writes from nested claude processes. A `claude` run from inside the agent (e.g. a Bash tool call) inherits the agent's env and config dir, so its hooks previously fired against the agent's state dir -- overwriting `claude_session_id` with a foreign session and clearing the `active` marker mid-turn. Hooks now exit early when the firing claude was started outside the agent's work dir (via `CLAUDE_PROJECT_DIR`), or when a different still-live claude process has claimed the agent's state (via a new `claude_main_pid` marker recorded from `CLAUDE_PID`). All checks fail open when the signals are unavailable. The launch command clears the `claude_main_pid` claim before each claude launch, so a stale claim (e.g. a recycled pid after a host reboot that now belongs to another live claude) cannot block the hooks of a freshly launched agent.

Existing agents pick up the new launch command and hooks on re-provisioning; agents created before this change keep their stored command until then.

Combined-fixes integration PR (carries mngr/fix-remote-workspace): fixes claude agent restart deadlocks caused by stale `claude_session_id` markers -- the resume gate now checks the exact session file claude consults, the launch chain self-heals via `--resume <agent-uuid>`, and readiness hooks ignore writes from nested claude processes. See `mngr-fix-remote-workspace.md` in this directory for details.

## 2026-07-28

Sending an unrecognized slash command (a typo like `/zzz`) to a Claude agent via `mngr message` now returns in about 2 seconds with an accurate warning naming the rejection evidence ("rejected ... nothing was executed") instead of polling the full 15-second relaxed window and reporting plain success. Claude records a structured warning the instant it rejects an unknown command; two new rejection probes watch for that record (in both the raw and native transcripts) past the pre-Enter baseline, keyed on Claude's own runtime verdict rather than any list of known commands -- so built-in, plugin, and user-defined commands are never affected, and a typo of any of them is still caught.

The Claude message-delivery journey release test now sends an unknown slash command and asserts the rejection event is recorded, so a future Claude Code change to the "Unknown command" warning record is caught by the release suite rather than silently degrading typo sends back to best-effort success.

Added a transcript record contract release test (`test_claude_transcript_record_contract`): drives a live claude through an idle prompt, a queued busy send, and an unknown command, then asserts the raw native session JSONL contains each record shape mngr consumes (user records, enqueue records, the dequeue-or-remove delivery outcome, the queue-operation vocabulary, the Unknown-command warning, the projects/<encoded-cwd>/<session-id>.jsonl layout, and top-level record uuids). Assertions parse the raw file directly rather than going through mngr's probes, so when a Claude Code release reshapes a record the failure names the drifted record, the mngr consumer to update, and the claude version that broke it.

## 2026-07-23

Hardened the Claude agent against interactive TUI selectors (e.g. the `/model` "Switch model?" confirmation) that could previously leave an agent silently stuck after a message was already delivered.

Three new `[agent_types.claude]` settings. Two depth knobs, both defaulting to `0` (off):

- `auto_accept_prompt_depth`: after a message is delivered, if it opened a blocking numbered selector, auto-accept the highlighted default (press Enter) up to this many times, clearing chained dialogs. At `0`, or if a selector persists, `mngr message` now reports the message as delivered-but-blocked (a distinct outcome) instead of hanging with no signal.

- `auto_accept_preflight_prompt_depth`: if a blocking dialog is already present when a send starts (or while the agent is coming up), auto-accept its default up to this many times before aborting. Independent of `auto_accept_prompt_depth` and of `auto_dismiss_dialogs`. Permission prompts (the `permissions_waiting` marker) are never auto-accepted -- they remain a hard error.

- `post_submit_dialog_observe_seconds` (default `2.0`): how long to keep watching the pane after a message is delivered before concluding no blocking selector appeared, and the per-accept re-check window while clearing chained dialogs. Raise it on slow or high-latency hosts where dialogs take longer to render.

Detection is structural (a rule line followed by an indented `❯`-arrow numbered option), so it also catches new/unknown confirmation dialogs, including in the pre-send preflight check. It recognizes both selector styles Claude Code uses: confirmation dialogs ruled with box-drawing dashes (`────`) and the interactive `/model` picker ruled with an upper-eighth block (`▔▔▔▔`). Each auto-accept is logged and recorded as an agent event capturing the selector text.

Also fixed a latent bug: the Claude readiness/leftover-input checks matched the `❯` glyph anywhere, so an open selector's indented option line could be mistaken for the input prompt. Both are now anchored to a line that begins with `❯` at column 0. Because of that anchoring, agent startup now relies on Claude's own `session_started` signal (skipping the generic TUI-ready wait) so that a selector blocking startup is auto-accepted (up to `auto_accept_preflight_prompt_depth`) instead of hanging on the create path.

## 2026-07-14

Add `auto_disable_questions` to `ClaudeAgentConfig`: when True, appends `--disallowed-tools AskUserQuestion` to the agent invocation so unattended agents can't block on questions (recent Claude Code releases no longer treat AskUserQuestion as a permission prompt, so `auto_allow_permissions` alone no longer covers it). Appended as a separate flag; Claude accumulates repeated `--disallowed-tools` flags, so a list already supplied via `cli_args` is preserved.

# Claude: content-verified message delivery

- Changed: `mngr message` to a claude agent now confirms submission by finding the message's own content in claude's durable transcripts -- checking BOTH the native session JSONL (resolved from `$CLAUDE_CONFIG_DIR` and the `claude_session_id` marker at poll time) and mngr's raw copy (`logs/claude_transcript/events.jsonl`), so a dead transcript watcher or unexpected config-dir layout cannot alone cause a false delivery failure. This replaces waiting on the `mngr-submit-<session>` tmux `wait-for` signal, whose latch-on-unconsumed-signal semantics caused sends to report success for messages that were never submitted.

- Changed: messages whose normalized content is too short to carry identity (emoji-only, punctuation) confirm on any newly appended enqueue/user record instead; slash commands (`/clear`, `/compact`, ...) use a relaxed policy (session-id change, `active`-marker touch, or any new record counts; nothing is required and the send never hard-fails).

- Changed: existing agents (created by older mngr versions) are fully supported without reprovisioning -- every artifact the probes read already exists on them. Newly created agents still fire the (now-unlistened) `mngr-submit` hooks so older mngr senders keep confirming; the hooks are marked for removal in a future release.

- Added: a warning plus a structured agent event when Claude's input box already contains leftover text before a send (detected via the `❯` prompt row).

- Added: release tests -- a message-delivery journey (idle -> busy/queued -> rapid sequential -> long message -> `/clear`, each delivered exactly once) and a concurrent-delivery test with two claude agents on one tmux server.

## 2026-07-06

Give `test_provision_preserves_existing_remote_project_config` the same 30s pytest-timeout that its sibling `test_provision_adds_trust_for_remote_work_dir` already carries. Both drive `provision()`, which runs the real local `claude --version` subprocess check; that check can exceed the default 10s timeout under CI load, so this test was flaking on the shorter limit.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-26

Added scope docstrings to this package's release tests so the TMR (test
map-reduce) harness can anchor each test's intended scope on its docstring
rather than on a tutorial block. Docstring-only; no test logic changed.

## 2026-06-24

**Bug fix:** in shared mode (`isolate_local_config_dir = false`), mngr no longer forces `CLAUDE_CONFIG_DIR=~/.claude` into the agent environment when your shell did not already have it set. A previous fix injected `CLAUDE_CONFIG_DIR` unconditionally, but exporting it -- even to claude's own `~/.claude` default -- is not equivalent to leaving it unset: claude reads its global `.claude.json` (onboarding state, theme, trust, history) from `$CLAUDE_CONFIG_DIR/.claude.json` when the variable is set, but from `~/.claude.json` (beside the dir) when it is unset. The forced value pointed claude at an inner stub file lacking your onboarding state, so every new shared-mode agent re-showed the theme/onboarding screen.

mngr now propagates `CLAUDE_CONFIG_DIR` in shared mode only when your own shell already exported it (in which case your config already lives inside that dir and sharing stays consistent). When unset, it stays unset so claude resolves its real `~/.claude.json`. The launch command's session-file lookup no longer depends on the variable being exported; it falls back to `$HOME/.claude` via `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`.

Renamed the claude agent type's `use_env_config_dir` option to `isolate_local_config_dir` and flipped its meaning: it now defaults to `true` (each local agent gets its own per-agent Claude config dir, so mngr never touches your default config). Set `isolate_local_config_dir = false` to share the user's `$CLAUDE_CONFIG_DIR` instead (the old `use_env_config_dir = true` behavior). The flag only affects local agents; remote agents always use an isolated config dir and ignore it.

The old `use_env_config_dir` key is still accepted as a deprecated option (it is the inverse of `isolate_local_config_dir`), so existing config files keep working -- mngr emits a deprecation warning when it is set. Setting both keys to contradictory (non-inverse) values is an error.

**Bug fix:** in shared mode (`isolate_local_config_dir = false`), mngr now injects `CLAUDE_CONFIG_DIR` into the agent environment, pointing at the user's shared config dir (`$CLAUDE_CONFIG_DIR`, or `~/.claude` when unset). Previously it was left unset, so the launch command's session-file lookup saw an empty `$CLAUDE_CONFIG_DIR` and claude could not find the user's config or sessions.

**New warning:** when creating a local claude agent on macOS, if mngr detects that you authenticate Claude Code with a claude.ai subscription (OAuth credentials) and config-dir isolation is enabled, it now warns that the isolated agent's copy of your credentials will go stale as the subscription refreshes them, and prints the exact command to disable isolation (`mngr config set agent_types.claude.isolate_local_config_dir false --scope user`).

Shared Claude config mode (`isolate_local_config_dir = false`) now dismisses the
cosmetic startup dialogs (trust, onboarding, effort callout, cost threshold)
directly in your default Claude config so they no longer intercept automated
input. Previously shared mode left the config untouched, so a fresh `~/.claude.json`
re-triggered the trust/onboarding screens on every agent.

mngr writes these dismissals into the file claude actually reads
(`$CLAUDE_CONFIG_DIR/.claude.json`, or `~/.claude.json` when the var is unset), and
honors `auto_dismiss_dialogs` in this mode. It never accepts bypass-permissions
mode via the global config -- that remains governed by `settings.json` -- and still
does no per-agent settings.json or keychain provisioning.

Also fixed: in shared mode, mngr's hooks are now installed when shared mode is set
via the current `isolate_local_config_dir = false` flag (previously they were only
installed when set via the deprecated `use_env_config_dir = true` alias).

## 2026-06-19

Fixed mngr's Claude hooks leaking into "normal" (non-mngr) Claude config. mngr previously wrote its readiness/credential/permission hooks into the project's `.claude/settings.local.json`, which plain `claude` runs in that directory also read -- so the hooks (e.g. an activity-event `mkdir`) fired outside mngr where `$MNGR_AGENT_STATE_DIR`/`$MNGR_HOST_DIR` are unset, producing errors like `mkdir: cannot create directory '/events': Permission denied`.

mngr now bakes all of its own hooks into the per-agent config-dir `settings.json` (`$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/settings.json`) -- the "user" settings layer Claude reads from `$CLAUDE_CONFIG_DIR`, which a plain `claude` run in the work dir never reads (it reads `~/.claude`). The hooks are built fresh on every provision, so there is no cross-version accumulation and nothing lands in a file plain `claude` reads.

`settings_overrides` is now applied as a **config-consistent patch** folded onto that `settings.json` (replacing the previous deep-merge-by-default). A bare key assigns (and the narrowing guard hard-errors if it would silently drop a non-empty sibling aggregate from the home base, unless `allow_settings_key_assignment_narrowing` is set); a `key__extend` merges onto the home value (lists concatenate, sets union, dicts merge), and nesting `__extend` merges deeper. So a `permissions__extend = {allow__extend = [...]}` override merges its `allow` while preserving a `permissions.defaultMode` from the home settings (#1647), whereas a bare `permissions = {allow = [...]}` that would drop `defaultMode` now warns instead of silently dropping it. The narrowing guard is recursive: even a bare key nested inside an `__extend` value (e.g. `permissions__extend = {allow = [...]}` over a non-empty home `permissions.allow`) is now caught, not just top-level bare keys. A stray `__extend` in your own home `settings.json` is normalized away (it has nothing below it to extend), and because the fold's `finalize` collapses every node into a plain value, no `__extend` marker can survive into the final file by construction.

`settings_overrides` now **accumulates across config scopes and `parent_type` inheritance** rather than a higher/child scope replacing the entire lower/parent value: per-key, non-overlapping keys from every scope survive and same-key `__extend`s combine (a higher bare key still wins over a lower marker for the same key). For example, a user scope setting `permissions__extend.allow__extend` and a project scope setting `model` now both apply, instead of the project scope wiping out the user scope's permissions.

A `settings_overrides` key can now use `key__assign` to assign a value **without** triggering the narrowing guard -- the per-key opt-out (e.g. `permissions__assign = {allow = [...]}` deliberately replaces the home `permissions`, dropping `defaultMode`, with no warning and without the global `allow_settings_key_assignment_narrowing` flag). It also works nested inside an `__extend` (`permissions__extend = {allow__assign = [...]}`). A bare `key` and `key__assign` for the same field in one layer raises a clear error. A `Static*`-wrapped override value (`StaticList` / `StaticDict` / `StaticTuple`) is likewise treated as a whole-value replacement and exempt from the narrowing guard.

A user-supplied `--settings` (in an agent type's `cli_args` or passed through on the `mngr create` command line) now passes through to `claude` verbatim. mngr injects no `--settings` of its own, and Claude natively layers the user's `--settings` over the config-dir `settings.json` (deep-merging dicts, concatenating same-event hooks), so the user's hooks and mngr's both fire with no mngr merge code and nothing to collide.

Reduced-support limitation in `use_env_config_dir` mode: there is no per-agent config dir to bake hooks into, so mngr loads its hooks (and the resolved `settings_overrides` patch) from the private managed `--settings` file (`$MNGR_AGENT_STATE_DIR/plugin/claude/mngr_managed_settings.json`), which Claude layers (highest precedence) over the user's shared config. `settings_overrides` is applied here too (folded onto mngr's hooks as the base, so its narrowing guard only catches an override dropping mngr's own hooks, not the shared-config values Claude layers itself). Because mngr already passes its own `--settings` in this mode, a user-supplied `--settings` (in `cli_args`/`agent_args`) is now rejected at provision with a `UserInputError` (mngr can't reliably merge a second `--settings` -- its value may be inline JSON, not a file); put those settings in `settings_overrides` or set `use_env_config_dir=False`. This mode is not yet used in production.

Note: this stops *new* leaks; it does not remove hooks already written into existing `settings.local.json` files by a prior mngr -- clean those up manually if present.

`mngr create` no longer requires the project's `.claude/settings.local.json` to be gitignored across the board. mngr writes its own hooks to the per-agent config dir, so that requirement now applies only when the `claude_subagent_proxy` plugin (PROXY mode) actually needs to rewrite user-defined Stop hooks in `settings.local.json` -- enforced by that plugin, at the point it writes.

Internal: the `settings_overrides` fold now imports the config-merge primitives from the new standalone `overlay` library rather than from `imbue.mngr.config`, and the fold itself now runs on `overlay`'s typed-node algebra (`lift`/`merge_narrowing_allowed`/`finalize`) instead of the string-suffix engine. The user-visible behavior is unchanged with one intentional fix: a deferred `key__assign` inside `settings_overrides` now reliably suppresses the narrowing guard at provision (previously the no-warn intent could be silently dropped during config-load, leaving it narrowing-checked).

Cross-scope `settings_overrides` narrowing is now caught at config-load. A claude agent type's `settings_overrides` accumulates across config scopes (user < project < local) and env-var layers, so previously a higher scope whose `settings_overrides` *bare* key replaced a non-empty aggregate set by a lower scope dropped the lower scope's entries silently (the provision-time guard only caught drops of the *home* `settings.json` base, not of a lower config scope). The config loader now surfaces these cross-scope drops through the same flag-gated narrowing error used for every other field: e.g. a local-scope `[agent_types.my_claude.settings_overrides.permissions] allow = ["B"]` that drops a project-scope `allow = ["A"]` raises, escapable via `allow_settings_key_assignment_narrowing = true` or by using `allow__extend` (accumulate) / `allow__assign` (replace without warning). Purely additive cross-scope `settings_overrides` (adding new keys) loads unchanged.

Internal (no user-facing behavior change): deduped `resolve_shared_claude_config_dir` to delegate to `get_claude_config_dir` (identical resolution), and `_claude_json_has_primary_api_key` now reads via the shared `read_claude_config` helper instead of hand-rolling the JSON read (corrupt-config tolerance unchanged). The managed-settings write in `_configure_agent_hooks` now routes through `write_json_dict_via_host`.

Trimmed the README to user-relevant content (removed internal implementation details and roadmap notes) and tightened it for concision.

`settings_overrides` now expresses merge intent with a Claude-compatible `__mngr_merge` map instead of the `__extend` / `__assign` key suffixes, so the same overrides behave consistently whether mngr or vanilla Claude Code reads them.

The suffixes leaked into the generated `settings.json` as keys Claude does not recognise; they are now rejected in `settings_overrides`. Declare merge intent in a top-level `__mngr_merge` map (`__mngr_merge = {"permissions.allow" = "extend"}` -- or `"assign"`) that vanilla Claude ignores. A bare key assigns with the narrowing guard, and the narrowing error prints the exact `__mngr_merge` patch to add -- the full nested patch in one error (a dict that would drop a sibling key is suggested as `extend`, a replaced list/value as `assign`). A `__mngr_merge` key found in your home `settings.json` is stripped (it is a no-op there).

## 2026-06-18

Added an `update_policy` field to the claude agent type that governs Claude Code's background auto-updater. `NEVER` sets `DISABLE_AUTOUPDATER=1` in the agent environment so the installed (optionally `version`-pinned) binary stays put; `AUTO` leaves the auto-updater enabled; `ASK` behaves like `AUTO` (claude has no interactive update flow). When unset, it defaults to `NEVER`.

**Behavior change:** claude agents now disable Claude Code's auto-updater by default (local and remote). Previously mngr did not disable it on local agents -- the per-agent config inherited your `~/.claude.json` `autoUpdates` value, so local agents typically auto-updated. Set `update_policy = "AUTO"` to opt back into the auto-updater. The policy is ignored in `use_env_config_dir` (shared) mode, where mngr leaves your claude environment alone.

Pin a specific version with `version` to control exactly what gets installed.

- The Claude response-streaming snapshot watcher now captures the agent's tmux pane by the configured primary window name (`tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so response streaming works regardless of the user's tmux `base-index`.
- Internal refactor: the Claude agent now builds its tmux session name via the shared `AgentInterface.session_name` helper instead of hand-rolling the `prefix + name` string, keeping it consistent with mngr's centralized session-name construction.

Internal: renamed the example custom agent type in an `on_before_create` test from a personal-config name to a neutral `coder`. No behavior change.

## 2026-06-17

`ClaudeAgent` now declares the `HasStreamingSnapshotMixin` capability mixin (it already implemented `get_stream_buffer_path`), so the live in-progress response-streaming view is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact.

`ClaudeAgent` also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config).

`ClaudeAgent` also declares `HasVersionManagementMixin` (version pin, else auto-update).

The auto-allow permission apply-path now reads through the `is_unattended_enabled()` contract instead of the `auto_allow_permissions` config field directly, making that method the single source of truth for unattended mode. Behavior is unchanged.

`ClaudeAgent` now declares `CliBackedAgentMixin` (marking it as wrapping a specific external CLI, which scopes the CLI-only capability-matrix rows) and `HasSessionAdoptionMixin`. Its session-adoption logic moved from `on_after_provisioning` into an `adopt_session` method (called by `on_after_provisioning`), so `--adopt-session` / `--from` session resumption is now a code-detectable `session_resume` capability. Behavior is unchanged.

Split the Claude agent into a shared `ClaudeCoreAgent` base and the interactive `ClaudeAgent(ClaudeCoreAgent, InteractiveTuiAgent, ...)` subclass. The core holds everything not tied to the interactive TUI (config-dir setup, credentials, transcripts, session preservation, auto-install, version management, the provisioning flow); the TUI subclass holds the keystroke send/readiness pipeline, the streaming snapshot (`HasStreamingSnapshotMixin`), session adoption (`HasSessionAdoptionMixin`), and start-dialog dismissal + pre-provisioning validation. `HeadlessClaude` extends `ClaudeCoreAgent` directly, so the headless variant no longer structurally inherits those interactive-only capabilities -- in the capability matrix `headless_claude` is now `n/a` (not `Y`) for `session_resume`. One user-visible change: `--adopt-session` is now rejected for `headless_claude` with a clear error, instead of being silently accepted and never resumed (headless runs `claude --print`, not `--resume`).

Post-split cleanup of now-redundant scaffolding: dropped the empty `NoPermissionsClaudeAgent` intermediate base (its no-op overrides became core defaults; `HeadlessClaude` extends `ClaudeCoreAgent` directly), the no-op `ClaudeCoreAgent.on_before_provisioning` (it just restated `BaseAgent`'s no-op default), and `HeadlessClaude`'s vestigial `_preflight_send_message` override (headless claude is not an interactive agent: the send-message flow, including `_preflight_send_message`, now lives only on the interactive `SendKeysAgent` side, and messaging a headless agent is refused at the call layer by `require_interactive_agent`). `HeadlessClaude` now carries one explicit `is_unattended_enabled` override that makes its `ClaudeCoreAgent` (config-driven) vs `BaseHeadlessAgent` (always-True) diamond choice deliberate (behavior unchanged: config-driven, as before the split).

The entire start-dialog-dismissal concern is now TUI-only: the shared `ClaudeCoreAgent.provision()` calls a single `_dismiss_start_dialogs` seam that is a no-op on the core and carries the real block (auto-dismiss or interactive prompt) only on the interactive `ClaudeAgent`. Previously the interactive prompt was already skipped for headless, but the `auto_dismiss_dialogs=True` path still ran for headless; now a headless claude does no start-dialog handling at all (it has no TUI pane to protect from intercepted input).

`ClaudeCoreAgent` now installs claude through the shared `ensure_cli_installed` helper (consent-gated locally, config-gated remotely; claude's `get_install_command` pins the version) instead of its own bespoke install block, then calls `reconcile_installed_version` to verify the present binary matches any pinned `version` (raising on mismatch). User-visible change: install / version-mismatch failures now raise `AgentInstallationError` (the shared installer's error type) rather than `PluginMngrError`.

The session-adoption create option and its agent-agnostic validation moved out of the claude plugin into core, since session adoption is now a capability shared by every interactive agent. The option is now `--adopt` (with `--adopt-session` kept as an accepted alias). Claude's `register_cli_options` no longer declares the option, and its `on_before_create` retains only the claude-specific fail-fast pre-resolution of named session ids. Claude now reads the adopted session ids from the first-class `CreateAgentOptions.adopt_session` field instead of the old `plugin_data["adopt_session"]` namespaced key (in both `on_before_create` and the `adopt_session` method). Claude's session-store scanner now routes through the shared `iter_agent_session_paths` helper. No change to claude's user-facing adoption behavior other than the option rename.

Claude now ships a release test (`test_claude_agent_e2e.py`) built on the shared agent release-test harness, so it is held to the same end-to-end lifecycle arc as the other agent types (create -> WAITING -> message -> RUNNING -> transcript -> stop/start resume -> destroy -> preserve -> adopt the preserved session into a fresh agent -> recall). The profile turns on the richer assertions (RUNNING marker, forced tool call, token usage), resolves adoption by the preserved session JSONL's path, and pins the agent to the `haiku` model tier (the seed/recall turns don't need a frontier model). Skipped unless `claude` is on PATH and `ANTHROPIC_API_KEY` is set.

Removed the now-redundant `test_adopt_session_brings_context_from_mngr_claude_agent_session` release test (and its `_create_mngr_claude_session` helper): adopting an mngr-managed agent's own preserved session into a fresh worktree is now exercised by the shared harness arc above. `test_adopt_session.py` retains the vanilla-`claude`-CLI adoption case, which the harness does not cover.

Fixed `--adopt A --from X` (combined explicit-adopt plus clone): the explicit session is copied into the destination's encoded project dir first, so the clone's rekey now merges the source-encoded subdir's files into that pre-existing dir instead of refusing on a whole-directory clobber. The merge is non-destructive and refuses (raising `AgentStartError`) only on a genuine per-file collision (the same session-id filename present in both), so distinct sessions coexist cleanly in one project dir while real data loss is still prevented.

A `--from` clone whose source has no resumable session now warns and adopts nothing (rather than raising): `--from` is a workspace clone, so carrying the source's conversation forward is a bonus, not a requirement. The agent still starts (falling back to the last `--adopt` session, or a fresh start). Explicit `--adopt` failures and the per-file merge collision remain hard errors.

`mngr create --yes` now dismisses claude's first-run *dialogs* (onboarding, effort callout, work-dir trust) in the per-agent config -- previously these were only auto-dismissed for a remote/unattended agent or the explicit `auto_dismiss_dialogs` config, so a local `--yes` create relied on the global `~/.claude.json`. `--yes` deliberately does *not* accept bypass-permissions mode (tool auto-allow stays governed by `auto_allow_permissions`/unattended): it auto-approves prompts, it does not silently widen tool permissions.

The claude release test now gitignores `.claude/settings.local.json` in its own `.gitignore` in both the seed and adoption worktrees, which mngr's claude preflight requires (a repo-local rule, not a global one) before it will write per-agent hooks. Without it the test could not get past `create`.

The claude release test no longer seeds a `~/.claude.json` to dismiss first-run dialogs and pre-approve the API key. It now relies entirely on the product behavior: `mngr create --yes` dismisses the dialogs and trusts the work dir, and the plugin's `approve_api_key_for_claude` pre-approves `ANTHROPIC_API_KEY`. The test builds its subprocess env with the plain shared `get_subprocess_test_env`, matching the sibling agent release tests.

Fixed resuming a Claude agent and immediately sending it a message. The TUI-ready indicator is now the input-prompt glyph (`❯`) instead of the "Claude Code" welcome banner. The banner only renders on a fresh start, not when resuming a saved session, so resumes previously skipped the readiness wait and could drop the message into a still-replaying transcript.

Adapted the Claude agents to the unified live-output contract.

`ClaudeAgent` (TUI) now inherits `SupportsLiveOutputMixin` directly (instead of the removed `HasStreamingSnapshotMixin`), exposes its streaming snapshot via `get_live_output_path()`, and supplies a `SnapshotDeltaReader` from `make_live_output_reader()`. The stream_buffer snapshot parsing/diffing (`compute_stream_delta` and friends, previously in `mngr_robinhood`) moves into the new `imbue.mngr_claude.stream_buffer` module alongside that reader, since it is the Claude watcher's format.

`HeadlessClaude` keeps streaming `claude --print` stream-json output via `stream_output()`, but the tail loop is now the shared one in mngr; the agent only supplies a `StreamJsonReader` plus its startup-grace "finished" check and stderr-augmented error reporting. No user-visible behavior change.

The claude common-transcript converter now emits `finish_reason` (was `stop_reason`, aligning with the OpenTelemetry GenAI vocabulary) and an ordered `parts[]` array on assistant records that preserves the source interleaving of text and tool-use blocks (`parts_ordered` true, since Claude's native content blocks are ordered).

## 2026-06-16

The common-transcript converter's event-conversion logic moved out of an inline `python3` heredoc in `common_transcript.sh` into a standalone `common_transcript_convert.py` (provisioned alongside the shell script), so it is type-checked, linted, and unit-tested directly. Malformed raw-transcript lines, unreadable existing-output lines, and transcript lines whose `message` is `null` (rather than an object) are dropped silently rather than aborting the conversion run.

The common-transcript watcher no longer echoes converter errors to the agent's pane: a genuine conversion error is recorded in the structured log only, instead of also being written to the watcher's stderr.

Internal refactor (no behavior change): the claude plugin's session-preservation-on-destroy now uses the shared `preserve_agent_state` / `preserve_host_agents_on_destroy` helpers in mngr core instead of its own inline copy. The preserved file set, the `preserve_sessions_on_destroy` config option, and the online/offline behavior are unchanged. The offline host-destroy path now also filters discovered agents by agent type.

Fixed: the synchronous transcript flush at turn end (which keeps a WAITING-signal consumer
from outrunning the common-transcript converter) now runs on *every* turn-end path.
Previously it lived in `wait_for_stop_hook.sh`'s `run_post_completion`, which is skipped on
the no-`/proc` fast path (macOS / local agents, where the Claude-ancestor PID lookup fails)
and on the SIGTERM/SIGINT handler -- so on those paths the marker was cleared without
flushing and the converter race remained. The flush now lives in `mark_inactive`, which
every path calls before clearing the `active` marker.

The flush's lock-acquire wait -- its only potentially-slow step -- is now bounded by an
explicit per-call timeout, so the SIGTERM/SIGINT handler can't block on it: interrupts cap
the wait at 2s (`HOOK_FLUSH_LOCK_TIMEOUT_SIGNAL`) while normal turn-end paths use 30s
(`HOOK_FLUSH_LOCK_TIMEOUT`). The bound is a portable `MNGR_CONVERT_LOCK_TIMEOUT` handed to
each converter pass rather than a `timeout(1)` wrapper, which macOS lacks.

## 2026-06-15

Hardened the turn-end signal so consumers that read the common transcript on the WAITING
transition (e.g. an orchestrator harvesting the agent's final message) can no longer
outrun the converter. `wait_for_stop_hook.sh` now flushes the transcript pipeline (a
synchronous `--single-pass` of the raw streamer and common-transcript converter, in
pipeline order) before clearing the `active` marker -- so by the time the agent reports
WAITING the common transcript already reflects the final assistant message.

The flush and the converter's convert lock now come from the shared
`mngr_common_transcript_lib.sh` (see the `mngr` changelog) rather than being duplicated
per agent. The convert lock keeps the on-demand flush from racing the background 5s
daemon into duplicate events; its timeout is tunable via `MNGR_CONVERT_LOCK_TIMEOUT`.

The `waiting_reason` field in `mngr list` is now more robust against a stranded `permissions_waiting` marker. The `PERMISSIONS` reason is gated on the agent's `active` (in-turn) marker: a `permissions_waiting` file that outlived its turn (e.g. a denied/cancelled dialog whose cleanup hook was missed) now reports `END_OF_TURN` instead of wrongly showing `PERMISSIONS`. This matches the behavior of `ClaudeAgent.get_lifecycle_state`, which only consults the permission marker when the base state is RUNNING.

## 2026-06-14

`mngr create --adopt-session` now validates the session ID up front, before any host or worktree is created. Passing an unknown (or ambiguous) session ID fails fast with a clean `Error: ...` message instead of crashing mid-provisioning with a full "Unexpected error" traceback.

The "session not found" message is also concise now: it no longer enumerates every searched directory (which included one per local mngr agent, often hundreds of paths).

Internal: the existence/ambiguity check (`_resolve_adopt_session`) now also runs in the `on_before_create` hook, which executes outside `provision_agent`'s `ConcurrencyGroup`. Previously the only check happened in `on_after_provisioning` (inside that group), where the group's exit wrapped the `UserInputError` in a `ConcurrencyExceptionGroup` -- no longer a `ClickException` -- so it was reported as an unexpected error. The session source is always local, so the early result matches the provision-time resolution.

# Shared, typed Claude stream-json envelope

Added `imbue.mngr_claude.stream_json`, a single typed boundary for the Claude partial-message
stream-json envelope (`message_start` / `content_block_start` / `content_block_delta` /
`text_delta` / `content_block_stop` / `message_delta` / `message_stop`, plus the `assistant`
summary's inner message). It is defined against the `anthropic` SDK's discriminated
`RawMessageStreamEvent` union and `anthropic.types.Message`, so the protocol vocabulary is owned
upstream instead of hand-rolled as bare string literals. The consume side validates into the union
and dispatches with an exhaustive `assert_never` match, so a future `anthropic` release that adds an
event variant fails the type check and names exactly what we must handle.

- `mngr ask`'s headless reader (`headless_claude_agent.py`) now parses partial-message events and
  the `assistant` summary through this boundary. Behavior is unchanged for well-formed `claude`
  output; an event variant or content-block type newer than the installed `anthropic` package
  degrades gracefully (it is skipped / falls back to a lenient text scan rather than dropping the
  response).
- Added `anthropic` as a dependency (kept unpinned; imported for its typed models only -- mngr
  still drives the `claude` CLI and makes no API calls).

## 2026-06-12

`mngr create --adopt-session <session-id>` now resolves a bare session ID against more locations. In addition to the current and user-scope Claude config dirs (`$CLAUDE_CONFIG_DIR/projects/` and `~/.claude/projects/`), it now also searches every live local mngr agent's per-agent config dir and the preserved session files of destroyed agents (see `preserve_sessions_on_destroy`). Passing a full `.jsonl` path is unchanged. Only the local host dir is scanned for mngr agent and preserved sessions.

Clarified the `--adopt-session` help text and behavior: the option is repeatable, but when multiple sessions are named, every named session is made available in the new agent while only the last one is resumed on startup (Claude can only resume one session at a time).

Internal: routed the plugin's `host_dir / "agents"` path constructions through the shared `get_agents_root_dir` / `get_agent_state_dir_path` helpers (now defined in `imbue.mngr.hosts.common`). No behavior change.

The `.claude/settings.local.json` gitignore preflight/provisioning check now delegates to the shared `check_path_gitignore_status` helper in `mngr.api.git` rather than implementing the git-check-ignore logic inline. No user-visible behavior change.

Added a conformance test asserting that claude's real emitted common-transcript records
validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`), so the claude emitter and the shared
contract cannot drift apart.

## 2026-06-11

### Fixed

- Fixed: Provisioning a local Claude agent no longer creates self-referential symlink loops inside the user's shared `~/.claude/` (e.g. `~/.claude/skills/skills -> ~/.claude/skills`, `~/.claude/commands/commands`, `~/.claude/plugins/cache/cache`). `_sync_user_resources` used plain `ln -sf` as an idempotent command; on the second and later provisions the destination was already a symlink-to-directory, so `ln` dereferenced it and nested a new link inside the shared source. All sync symlinks (directory, child, and individual-file, including credentials and `keybindings.json`) now use `ln -sfn` (`--no-dereference`), which replaces the existing destination symlink instead of following it.

### Changed

- Changed: A skill-provisioned agent's primary skill (e.g. `code-guardian`, `fixme-fairy`) is now installed into that agent's own per-agent config dir (`$CLAUDE_CONFIG_DIR/skills/<name>/SKILL.md`) instead of the user's global `~/.claude/skills/`. Previously the local install wrote to global `~/.claude/skills/`, which `_sync_user_resources` then symlinked into every local agent's config dir, so a `code-guardian`/`fixme-fairy` skill leaked into the skill list of every local agent. To support this, `skills/` is now synced via child-level symlinks (one symlink per skill, mirroring how `plugins/` is already handled) so the agent's own skill can live as a real file alongside the symlinked user skills without leaking back into the shared source. The local install is now silent (no interactive install/update prompt), matching the always-silent remote install.

## 2026-06-10

Test-quality hardening across the mngr_claude test suite (no user-visible behavior change). Replaced assertions that passed without verifying correctness with ones that check real effects:

- Seven `on_before_provisioning` tests that asserted nothing ("did not raise") now assert observable effects (config left untouched, missing-credentials warning present/absent, untrusted worktree rejected).
- `does-not-extend-trust` provisioning tests now assert the exact set of trusted projects instead of the presence of a key the test itself wrote.
- Transcript-converter truncation tests now assert exact lengths and the ellipsis marker (not just an upper bound), and the "skips event" tests now prove only the bad event is dropped (a known-good event survives) plus cover the missing-`timestamp` branch.
- Command-assembly and install-command tests now assert on shlex-parsed tokens / load-bearing flags instead of hand-rebuilt exact shell strings.
- Skill-install skip tests assert file content is unchanged instead of relying on mtime equality; added remote-install coverage. Custom-agent-type resolution test now sets a non-default field to prove it survives the merge.
- Grace-period headless test asserts the poll actually re-checked; removed dead `_patch_agent_as_stopped` calls and a fragile wall-clock timing assertion.
- claude_config no-op tests assert content is byte-for-byte unchanged; effort-callout check test isolated to the effort dialog.
- Removed an introduced `unittest.mock.patch` of the function under test in favor of the real no-credentials environment, and a duplicate local `temp_source_dir` fixture (now inherited from the shared modal conftest).
- Release/acceptance tests: the Modal provisioning test destroys the agent and asserts a non-empty preserved session JSONL; the adopt-session and modal tests drop brittle "Done." log-string checks; the no-dialog send_message test matches the specific downstream timeout; the background-tasks prefix-collision test asserts the script reached its gone-session exit; magic `sleep` literals replaced with a named constant.

- `ClaudeAgent` now supplies its own "message accepted" probe to the shared submission-confirm path: a shell command that reads the latest `enqueue` event from Claude's transcript event log (`logs/claude_transcript/events.jsonl`) and prints its ISO-8601 timestamp. This is the Claude-specific knowledge that previously lived (hardcoded) in the shared `tui_utils` module; moving it into the plugin keeps `tui_utils` agent-neutral while preserving the existing fast-confirm-on-enqueue behavior for Claude agents.

Raised the stale coverage floor from 24% to 80% to match the coverage CI already measures (~83%), and removed the now-obsolete comment referencing per-package offload coverage drift (the offload bug that caused that drift has since been fixed).

## 2026-06-09

- Readiness hooks: a Claude agent restarted/resumed mid-turn no longer reports the RUNNING lifecycle state forever. The `active` marker is set on UserPromptSubmit and only removed by the Stop / idle Notification hooks, so a turn abandoned by an abnormal exit (container restart, OOM, crash) left it stale and the agent stuck at RUNNING. The SessionStart hook now clears `active`/`permissions_waiting` on `startup`/`resume` (a fresh, not-mid-turn process), so the lifecycle state self-heals on the next (re)start; `compact` is excluded because auto-compaction fires mid-turn while Claude is genuinely active. The same hook touches a new `claude_process_started` marker whose mtime gives consumers a restart boundary to compare transcript timestamps against (any transcript event older than it belongs to a turn the current process did not run). The shared "clear active markers and emit an activity event" shell snippet is extracted into a constant reused by the Notification idle hook and the new SessionStart hook so the two stay byte-identical.

Claude session preservation on destroy was rewritten onto the new shared
`preserve_agent_data` machinery in core mngr. Behavior is unchanged in substance -- session
JSONLs, the raw and common transcripts, and the session-id history are still preserved before
the agent state directory is deleted, and `projects/` is still skipped in `use_env_config_dir`
mode -- but the implementation is now a single declarative list of files preserved through one
code path for both online and offline (volume-backed) hosts, replacing the previously
duplicated SSH and Volume implementations.

The on-disk layout of preserved sessions changed: files now live at
`<local_host_dir>/preserved/<agent-name>--<agent-id>/` and mirror the agent state directory
verbatim (e.g. `plugin/claude/anthropic/projects/...`, `logs/claude_transcript/...`,
`events/claude/common_transcript/...`, `claude_session_id_history`), instead of the old
`<local_host_dir>/plugin/mngr_claude/preserved_sessions/<agent-name>--<agent-id>/` location
with renamed subdirectories. This is a switch-forward change; previously preserved sessions in
the old location are left in place.

## 2026-06-08

Internal refactor (no behavior change): the `claude` agent's `assemble_command` now shell-quotes its extra `agent_args` via the shared `quote_agent_args` helper instead of an inline `shlex.quote` loop. This is the same quoting the plugin already performed; it now shares one implementation (and one explanatory comment) with `BaseAgent`, so the two cannot drift.

Standardized mngr_claude's test setup on `register_plugin_test_fixtures(globals())`
for HOME isolation (matching every other mngr plugin), keeping
`pytest_plugins = ["imbue.mngr_modal.conftest"]` only to share mngr_modal's test
fixtures (`modal_subprocess_env`, etc.). Removed the now-redundant
`enabled_plugins` override. Internal test-infrastructure change only; no
user-facing behavior change.

## 2026-06-06

Added approximate response streaming for Claude agents, driven by watching the agent's tmux pane.

- New `streaming_snapshot_interval_seconds` (float, default `0.0`) on the `claude` agent type config. When `> 0`, a background watcher polls the agent's tmux pane every N seconds and writes the in-progress assistant text to `$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer`. When `<= 0` (the default) the watcher is neither provisioned nor run, so existing behavior is unchanged.
- `stream_buffer` format: line 1 is the `uuid` of the last *complete* assistant message (empty string if none yet, read from `logs/claude_transcript/events.jsonl`), and lines 2+ are the in-progress assistant text reverse-mapped from the terminal rendering back into markdown. It is written atomically (temp file + `mv`), cleared on watcher startup, and emptied (body cleared, id line kept) when the agent goes idle.
- New self-contained watcher script `resources/stream_snapshot.py` (stdlib-only, like `sync_keychain_credentials.py`). It captures the pane with `tmux capture-pane -e -J` (ANSI codes preserved, soft-wraps rejoined), identifies the latest assistant-text block by the `●` marker's color (assistant markers are the achromatic default text color; chromatic tool-call and mid-gray status markers are ignored), and reverse-maps bold/italic, inline code, links (OSC 8 hyperlinks), blockquotes, lists, code blocks, and tables (box-drawing back to pipe syntax). The pure parsing functions are unit-tested directly against real captured fixtures.
- The body is strict-append within a message: successive visible-pane snapshots are overlap-stitched (longest suffix/prefix line match) so the full message is reconstructed as it scrolls; a stale shorter snapshot that is a prefix of the accumulated body is ignored, and a genuinely non-overlapping snapshot resets to a new message. A trailing table is held back until its raw form is stable across polls, then rendered, and each successive render is a superset of the previous one so the rendered body never shrinks or duplicates.
- The poll interval is provisioned to a per-agent file (`plugin/claude/stream_interval`) that the watcher reads at runtime, rather than relying on env-var propagation into the background-tasks subshell. `claude_background_tasks.sh` launches and restarts the watcher whenever the script is present (the provision-time presence check is the single gate, like the common-transcript converter). Provisioning fails fast if streaming is enabled but the host lacks `python3`.
- Added `ClaudeAgent.get_stream_buffer_path()` so other code (e.g. `mngr robinhood`) can locate and read the buffer.

Not in scope: perfect markdown fidelity, heading-level or code-block-language recovery, reasoning/"thinking" block streaming, streaming for non-claude agent types, and any new top-level `mngr` CLI command to read the buffer.

## 2026-06-05

Internal refactor (no behavior change): `ClaudeAgent._find_git_source_path` now delegates to the shared core helper `imbue.mngr.utils.git_utils.find_git_source_path` instead of inlining the `find_git_common_dir` + parent logic, which was duplicated in the `antigravity` plugin.

Updated changelog references following the `mngr_uncapped_claude` plugin rename:
mentions of the `mngr uncapped-claude` command in this project's changelog now
read `mngr robinhood`. No code changes.

## 2026-06-04

Replaced the module-local `_get_local_host` helper with the shared `get_local_host` from `imbue.mngr.api.providers` (deduplication; no behavior change).

## 2026-06-02

- pyproject.toml: align `imbue-mngr*==` pin stragglers with the satellites bumped in main's `e22e7010e` release commit. Several `imbue-mngr-*` libs still pinned to older versions even though `libs/mngr` had moved to 0.2.10; building the apps/minds ToDesktop bundle from main today would fail at `uv lock` in `apps/minds/scripts/build.js` because the workspace constraint graph is unsatisfiable. Day-to-day dev hides this because `[tool.uv.sources]` redirects every `imbue-mngr-*` to its workspace path, bypassing the `==` pin.

## 2026-06-01

Fixed `--adopt-session` rejecting valid Claude agent subtypes. It now accepts any agent type that resolves to a Claude agent (including config-defined templates like `write-plus` whose `parent_type` chain reaches `claude`), instead of only the literal `claude` type name. The check routes through the centralized `resolve_agent_type` registry rather than a string comparison.

# Simplify `--adopt-session` agent-type validation

- Now that `CreateAgentOptions.agent_type` is always set (it became a
  required field), the `--adopt-session` `on_before_create` validation no
  longer special-cases an unset type: it simply requires the agent type
  to be `claude`. No behavior change for users, since the CLI already
  requires a concrete agent type.

Updated the `on_before_create` hook implementation (used for `--adopt-session` validation) to accept the new `mngr_ctx` parameter now passed by mngr.

## 2026-05-28

# Adopt-session test opts into the pytest config guard

`mngr`'s `is_allowed_in_pytest` config field now defaults to `False`, so a
config loaded during a pytest run must opt in. The `mngr_claude`
adopt-session tests hand-roll a trusted-subprocess profile and load it, so the
`trusted_subprocess_env` fixture now writes `is_allowed_in_pytest = true` into
that profile's settings.local.toml. Test-only change; no user-facing behavior
change.

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

- `ClaudeAgentConfig.merge_with` follows mngr's new assign-by-default semantics: an override's `cli_args` replaces the base's (rather than concatenating). To opt back into additive layering, use the `__extend` operator with an explicit list value, e.g. `cli_args__extend = ["--verbose"]`; the string-shorthand form that the bare `cli_args` field accepts (which the validator splits via shlex) is not accepted by the `__extend` resolver. See the `mngr` changelog entry for the full breaking-change writeup.

Update Claude plugin to use the structured `TmuxWindowTarget` type for tmux
pane targeting. `_send_enter_and_validate` and `_preflight_send_message` now
take `tmux_target: TmuxWindowTarget` instead of a bare string, matching the
`BaseAgent` API change in `libs/mngr` that fixes stale `WAITING` lifecycle
state caused by tmux session-name prefix matching.

Fix `claude_background_tasks.sh` to use the `=` exact-match prefix in its
`tmux has-session` polling loop. Without `=`, the loop would never exit
when a Claude agent's session was killed but a sibling session whose name
shares this name as a prefix was still alive, leaking the transcript
streamer and common-transcript converter for stopped agents.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

`resolve_shared_claude_config_dir()` (used when a claude agent opts into `use_env_config_dir=True`) now falls back to `~/.claude/` when `$CLAUDE_CONFIG_DIR` is unset, instead of raising. The fallback matches claude's own default, so callers can treat that flag as a pure "don't touch the config dir" knob even on machines where the user never sets `CLAUDE_CONFIG_DIR`. Also drops `ORIGINAL_CLAUDE_CONFIG_DIR` from the agent env in the `mngr robinhood` flow so credential sync reads from the live `$CLAUDE_CONFIG_DIR` (matters when robinhood is invoked from inside another mngr claude agent).

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

`ClaudeAgent` now satisfies the new `HasTranscriptMixin` and
`HasCommonTranscriptMixin` mixins on `AgentInterface` (introduced to give every
agent type a shared transcript-capture contract). The user-visible behavior of
`mngr transcript <claude-agent>` is unchanged.

## 2026-05-14

- Fixed: a cloned claude agent now actually resumes the source agent's conversation (the model sees and acts on the source's history), not just inherits the session JSONL on disk. Previously, after #1598's cross-host plugin/ rsync, claude on the destination would still start fresh because the JSONL was filed under the *source's* encoded work_dir, the rsynced ``sessions-index.json`` pointed at source paths, and ``claude_session_id`` was wrong. ``_adopt_cloned_session`` now renames the project subdir to the destination's realpath-resolved encoding (handles the ``/mngr/projects/agent-X`` → ``/__modal/volumes/<vol-id>/projects/agent-X`` symlink on Modal), drops the stale index, writes ``claude_session_id`` to the JSONL filename's stem (the ground truth — the source's own ``claude_session_id`` file holds the agent UUID from the SessionStart hook default rather than the real id), and carries forward ``claude_session_id_history``. The ``--adopt-session`` flow shares the same finalize step.

Add a `use_env_config_dir` option on the `claude` agent type config. When set
to `true`, local Claude agents share the user's `$CLAUDE_CONFIG_DIR` instead of
provisioning a per-agent config dir, and mngr does not write to the user's
Claude config (no trust additions, dialog dismissal, per-agent settings, or
keychain provisioning). Only supported for local hosts; `$CLAUDE_CONFIG_DIR`
must be set. The user is responsible for one-time interactive `claude` setup.
See `libs/mngr_claude/README.md` for details.

## 2026-05-06

- Stop the `claude plugin update` SessionStart hook from hanging Modal-launched
  agents at an `ssh` first-contact (TOFU) prompt for github.com. The plugin
  updater shells out to `git pull`, which uses `ssh` -- on a fresh sandbox
  with no `~/.ssh/known_hosts` entry, ssh blocks on a "Are you sure you
  want to continue connecting" prompt that Claude Code's bypass-permissions
  setting does not cover. `scripts/claude_update_plugin.sh` now prefixes
  the update with `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes'`, which writes the first-seen host key to known_hosts
  and exits non-interactively if anything goes wrong (matching the
  script's existing `2>/dev/null || true` failure tolerance).
