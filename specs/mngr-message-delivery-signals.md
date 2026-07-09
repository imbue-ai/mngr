# mngr message: delivery signals for interactive TUI agents

## Overview

`mngr message <agent> -m "<text>"` must answer one question before it returns: **did the agent receive the message?** For a Claude agent, mngr answers this by pasting text into a tmux pane, pressing Enter, and then watching for evidence that the text was consumed.

There is no IPC channel. mngr drives a terminal UI over a pty. `tmux send-keys` reports success once tmux has written bytes into the pty; a pty carries no acknowledgement frame, so that success says nothing about what the program on the far end did. Every "did it land" answer must therefore be **reconstructed from side effects** that Claude Code happens to leave behind.

This is the root of the complexity, and it is worth stating precisely:

> Almost every side effect Claude Code emits is **semantic**. It records what the input *meant* -- a prompt for the model, a message added to a queue, a session restart, an unknown command -- and different classes of meaning are recorded at different times (some at accept, some only at completion). None of them, except one, records the fact that *input arrived*.

Today's success condition is a race between two signals, and **both are model-bound**: the `UserPromptSubmit` hook fires only when a prompt reaches the model, and the `enqueue` transcript event is written only when a message is placed in the queue (which happens only when the agent is busy). Input that never reaches the model satisfies neither. That is why a typo, a `/login`, and a `/cost` all hang for the full 90-second timeout despite being delivered, and why `/clear` and `/compact` each needed a bespoke hook to rescue them.

Two distinct facts are also conflated into a single boolean. "The agent consumed my keystrokes" and "the agent is ready to consume the next message" are different properties, and modal commands like `/login` satisfy the first while permanently violating the second.

**Audience:** developers working on `mngr message`, `InteractiveTuiAgent`, or the Claude agent plugin.

**Scope:** the Claude agent (`mngr_claude`). The `tui_utils` strategies are agent-neutral and shared with `mngr_codex` and `mngr_antigravity`, so signal-layer changes must stay agent-neutral; only the probe commands are Claude-specific.

**Related specs:** [common-transcript-standard](common-transcript-standard/spec.md), [agent-plugin-parity](agent-plugin-parity/spec.md).

**Status:** problem statement and measured findings. The design section is a proposal, not a decision.

## Terminology

| Term | Meaning |
|---|---|
| **Delivered** | Claude consumed the keystrokes. It has not necessarily acted on them, and may reject them. |
| **Settled** | Claude is ready to accept the next message: the input buffer is empty and the input row exists. |
| **Executed** | The input produced its intended effect (the model replied, the local command ran). |
| **Accept time** | The instant Enter is consumed. |
| **Completion time** | The instant the resulting action finishes. For a modal command, this may be never. |

`mngr message` should return on **delivered**, must guarantee **settled** before the next send, and cannot in general observe **executed**.

## How mngr message works today

The call chain is `mngr message` -> `send_message_to_agents` -> `_send_message_to_agent` -> `InteractiveTuiAgent.send_message` (`libs/mngr/imbue/mngr/agents/tui_agent.py`).

1. Reject the agent if its lifecycle state is `STOPPED`.
2. Acquire an exclusive per-agent file lock.
3. `_preflight_send_message` -- check the `permissions_waiting` marker and match the pane against `_DIALOG_INDICATORS`. Fail fast on a hit.
4. `wait_for_tui_ready` -- poll the pane for the prompt glyph. Timeout **30s**.
5. Paste the text (`tmux send-keys -l` under 1024 chars, `load-buffer` + `paste-buffer` at or above).
6. `wait_for_paste_visible` -- poll the pane until the text appears. Timeout **15s**.
7. `_send_enter_and_validate` -- the submission wait. Timeout **90s**.

Step 7 (`send_enter_via_tmux_wait_for_hook`, with `accept_marker_command` supplied by `ClaudeAgent`) builds a single remote bash script that:

- baselines the accept marker;
- registers a background `timeout 91 tmux wait-for mngr-submit-<session>` waiter that writes a sentinel file on success (registered **before** Enter, because a tmux signal with no registered waiter is lost);
- sends Enter from a second background subshell after 0.1s;
- polls every 0.25s, exiting 0 as soon as **either** the sentinel exists **or** the marker prints a token strictly greater than the baseline.

The marker token must be **lexicographically monotonic**; the comparison is a plain shell string `>`, and an empty baseline sorts before any real token.

## Categories of input

Classified by what Claude Code does with the text, because that is what determines which signals fire.

| ID | Category | Examples |
|---|---|---|
| **A** | Prompt for the model | plain text; skills; plugin commands; `.claude/commands/*.md` -- anything that expands into a prompt |
| **B** | TUI-local command, completes immediately | `/effort high`, `/add-dir`, `/mcp` |
| **C** | TUI-local command, modal | `/login`, `/cost`, `/model` -- opens a panel and waits for further input |
| **D** | TUI-local command, session-restarting | `/clear`, `/compact` |
| **E** | Invalid command | `/zzznotacommand` |
| **F** | Any of A-E while the agent is busy | queued at accept, interpreted at dequeue |
| **G** | Empty input | Enter on an empty box |

Categories B, C, D, and E are the ones users cannot define: they are Claude Code built-ins. A user-defined skill or command is always category A, which is why user-defined commands have never exhibited this bug.

## Signal inventory

### Produced by Claude Code; mngr only reads

| Signal | Location | Written when | Notes |
|---|---|---|---|
| **`history.jsonl`** | `$CLAUDE_CONFIG_DIR/history.jsonl` (falls back to `~/.claude/`) | **At accept, before interpretation** | `{display, pastedContents, timestamp (epoch ms), project (cwd), sessionId}`. Global file; `project` is the only per-agent scoping. Dedups a `display` identical to the immediately preceding entry. |
| **`enqueue` / `dequeue`** | session JSONL, `type=queue-operation` | At accept, **only if the agent is busy** | `enqueue` carries the raw `content` before validity is checked. |
| **Rejection entry** | session JSONL, `type=system, subtype=informational, level=warning`, `content="Unknown command: /x"` | At accept | Structured. The English string is for rendering only; match on `type` + `level`. |
| **Local-command entry** | session JSONL, `type=system, subtype=local_command`, `content="<command-name>…"` then `"<local-command-stdout>…"` | **At completion** | An older shape (`type=user`, preceded by an `isMeta` `<local-command-caveat>`) also occurs; match the `<command-name>` substring, not the `type`. |
| **Hook events** | dispatched to configured hooks | varies | Full set in the binary: `CwdChanged, Elicitation, ElicitationResult, FileChanged, MessageDisplay, Notification, PermissionDenied, PermissionRequest, PostToolBatch, PostToolUse, PostToolUseFailure, PreToolUse, SessionStart, Setup, Stop, SubagentStart, SubagentStop, UserPromptExpansion, UserPromptSubmit, WorktreeCreate`. **There is no `SlashCommand` hook.** See "Which hooks fire" below. |
| **Pane rendering** | tmux `capture-pane` | continuously | Prompt glyph, echoed messages, modal panels. See ISSUE-10: not a reliable oracle for buffer contents. |

### Produced by mngr

Installed into Claude's settings by `build_readiness_hooks_config` (`libs/mngr_claude/imbue/mngr_claude/claude_config.py`), or written by provisioned scripts.

| Signal | Producer | Fires when | Author |
|---|---|---|---|
| `tmux wait-for -S mngr-submit-<session>` | `UserPromptSubmit` hook | the prompt reaches the model | (original) |
| the same signal | `SessionStart` hook, gated on `source in {clear, compact}` | `/clear` or `/compact` restarts the session | runtime-backup, 2026-05-09 |
| `active` / `permissions_waiting` markers | `UserPromptSubmit`, `PermissionRequest`, `PostToolUse`, `Notification`, `Stop` hooks | lifecycle transitions | (original) |
| `claude_session_id_history` (append `"<sid> <source>"`) | `SessionStart` hook | every session start, with its source | (original) |
| `logs/claude_transcript/events.jsonl` | `stream_transcript.sh` tailing Claude's session JSONLs | continuously | Evan Ryan Gunter |
| host activity events | `UserPromptSubmit` / idle hooks | lifecycle transitions | (original) |

### Which hooks fire

Measured by registering a logging handler for **all twenty** hook events via `claude --settings`, then submitting one input of each category:

| Input | Hooks that fired |
|---|---|
| normal prompt | `UserPromptSubmit`, `MessageDisplay`, `Stop` |
| `/zzztypohook` (invalid) | **none** |
| `/cost` (modal) | **none** |
| `/model` (modal) | **none** |
| `/effort high` (local) | **none** |
| `/clear` | `SessionStart` only |

`/login` is `type:"local-jsx"` in the binary, the same class as `/cost` and `/model`, so it fires nothing either. **No hook exists that observes a TUI-local command.** `MessageDisplay` fires only for model-bound prompts, so it is not a receipt signal.

### Modality is a property of the invocation, not the command name

Claude Code classifies commands as `local-jsx` (75, renders a panel), `local` (37), and `prompt` (18). But whether an invocation *wedges the input row* depends on its arguments:

| Invocation | Input row after submit |
|---|---|
| `/effort` | gone (modal) |
| `/effort high` | present |
| `/model` | gone (modal) |
| `/model opus` | present |
| `/add-dir` | gone (modal) |
| `/status`, `/usage`, `/mcp`, `/plugin` | gone (modal) |
| `/context`, `/agents` | present |

**Warning:** a hardcoded reject-list keyed on the command *name* would therefore reject `/effort high` and `/model opus`, which work correctly today. Any rejection must be keyed on the observed effect (no input row after a bounded settle window), not on the name.

### Consumed by the send path

| Consumer | Reads | Author |
|---|---|---|
| `send_enter_via_tmux_wait_for_hook` | tmux wait-for channel + accept marker, concurrently | Gabriel Guralnick, 2026-06-08 (`_build_signal_or_marker_command`); refactored out of `InteractiveTuiAgent` by Evan Ryan Gunter, 2026-05-13 |
| `_build_accept_marker_command` | newest `enqueue` timestamp in `events.jsonl` | Gabriel Guralnick, 2026-06-09 |
| `wait_for_tui_ready`, `wait_for_paste_visible` | pane | Evan Ryan Gunter, 2026-05-13 |
| `_preflight_send_message`, `_DIALOG_INDICATORS` | `permissions_waiting` marker + pane | (original) |

**Note:** `history.jsonl` is currently read by nothing in mngr.

## Signal-to-case matrix

Measured against a real local Claude agent (`mngr` at `9c6368a49`, Claude Code 2.1.205, macOS). "-" means the signal never fires.

### Idle agent

| Case | UserPromptSubmit | SessionStart(clear/compact) | `enqueue` | `history.jsonl` | Transcript entry at accept | Input box clears |
|---|---|---|---|---|---|---|
| A prompt | fires | - | - | **+1** | user/assistant events | yes |
| B local, completes | - | - | - | **+1** | none (only at completion) | yes |
| C local, modal | - | - | - | **+1** | none (never, if not dismissed) | no input row at all |
| D `/clear`, `/compact` | - | fires | - | **+1** | none (only at completion) | yes, but see ISSUE-8 |
| E invalid | - | - | - | **+1** | `system/level=warning` | yes |
| G empty | - | - | - | 0 | none | n/a |

### Busy agent

| Case | UserPromptSubmit | `enqueue` | `history.jsonl` |
|---|---|---|---|
| F any | at dequeue, if it turns out to be a prompt | **at accept** | **+1 at accept** |

**The only column that is 1:1 with "input consumed" is `history.jsonl`**, and it is the only signal written before Claude interprets the text. Its single deviation is the consecutive-duplicate dedup (ISSUE-7). Its `timestamp` is a 13-digit epoch-ms integer, so it satisfies the existing marker contract's lexicographic-monotonicity requirement without any change to `tui_utils`.

## Issues found

Ordered by severity. Each was reproduced against a real agent unless marked otherwise.

### ISSUE-1: `timeout` is missing on macOS, and the failure is silent

**Status: fixed in this branch.**

`_build_signal_only_command` and `_build_signal_or_marker_command` both ran `timeout <n> tmux wait-for …`. GNU `timeout` is not present on a stock macOS. The command returns 127, `>/dev/null 2>&1` swallows it, `&&` short-circuits, and -- critically -- **the tmux waiter is never registered**, so when the `UserPromptSubmit` hook fires `tmux wait-for -S`, the signal wakes nobody.

Consequence: on any host without GNU coreutils, the hook half of the race is dead. Combined with ISSUE-3, **every** message to an idle agent timed out at 90s, including a plain prompt.

Evidence: `command -v timeout` -> missing; the exact construct writes no sentinel (`rc=127`), and writes one when a `timeout` shim is placed on `PATH`. tmux signalling itself round-trips correctly. A plain prompt that Claude demonstrably answered (34 transcript events, `assistant` reply) was reported as `Failed to send message` after 93.6s.

No single binary bounds a command's runtime on both platforms: `timeout` is GNU-only, and macOS's `gtimeout` requires `brew install coreutils` -- unacceptable for a tool installed from PyPI. The fix uses plain bash, which these commands already run under: the waiter is backgrounded and raced against a `sleep`-then-`kill -9`, and `wait` reports the outcome. See ISSUE-1b for why the kill must be `SIGKILL`, and why the waiter must not be wrapped in a subshell (killing the subshell would orphan the `tmux wait-for` client, which stays registered on the channel and steals a later submission's signal).

Measured after the fix, on the same macOS host where every case previously failed at ~93s:

| message | before | after |
|---|---|---|
| `reply with exactly the word ok` | exit 1, 93.6s | exit 0, 2.1s |
| `/clear` | exit 1, 93.7s | exit 0, 2.1s |
| `/clear` then a prompt | exit 1 | exit 0, 4.0s, no concatenation |

### ISSUE-1b: tmux latches `wait-for` signals, so a stale signal can confirm the wrong send

**Status: fixed in this branch.**

The code asserted the opposite. `_build_signal_only_command`'s docstring read "signals wake exactly one waiter; a signal with none registered is lost". Measured:

```
tmux wait-for -S chan     # no waiter registered
tmux wait-for chan        # returns immediately, rc=0
```

tmux **remembers** the signal and the next `wait-for` consumes it. A signal left on the channel by an earlier submission -- one that timed out, or whose hook fired after mngr gave up -- would therefore confirm the *next* `mngr message` instantly, before Enter is even processed. Because ISSUE-1 stops the waiter from ever registering, this hazard is currently masked; fixing ISSUE-1 alone would expose it.

The fix drains the channel before registering the waiter. A regression test (`test_send_enter_via_hook_ignores_stale_latched_signal`) latches the channel and asserts the send still times out.

**Note:** the drain has a cost. A legitimate signal that lands inside the drain window is consumed. This cannot happen for the send's own message (the drain completes before Enter is sent), but a *concurrent* submission's signal can be eaten -- turning a false success into a false timeout, which is the safer failure.

**Warning:** `tmux wait-for` handles `SIGTERM` and exits **0**, which is indistinguishable from being woken. Any deadline enforcement must use `SIGKILL` (exit 137), or a timed-out waiter reads as a successful submission.

### ISSUE-2: `tac` is missing on macOS; its stderr is injected into the agent's input box

**Status: fixed in this branch.**

`mngr_transcript_lib.sh` ran `done < <(tac "$session_file")` as part of transcript offset reconciliation (Evan Ryan Gunter, 2026-05-18). `tac` does not exist on macOS.

Three consequences. First, the error text `mngr_transcript_lib.sh: line 82: tac: command not found` is printed into the agent's terminal and **paints over the Claude input row**. Second, that pollution makes `wait_for_paste_visible` fail, so the *next* `mngr message` dies after 16.7s -- a distinct failure from the 90s submission timeout. Third, offset reconciliation is what prevents re-emitting lines; duplicated `Unknown command` entries were observed in `events.jsonl`, consistent with reconciliation failing (**suspected, not proven**).

`events.jsonl` is the file the current accept marker reads.

The fix is a `mngr_transcript_reverse_lines` helper that prefers `tac` and falls back to `tail -r` (BSD). Both stream, so neither buffers the file. This follows the precedent set when `tail --follow=name` was replaced with the portable `tail -F` in `mngr_lima`.

### ISSUE-3: the accept marker cannot fire on an idle agent

`_build_accept_marker_command` greps for `"operation":"enqueue"`. Claude only writes a `queue-operation` when a message must wait in line, i.e. when the agent is busy. Across the entire life of a probe agent, the marker returned an **empty token**: it never fired once.

This is by design, and correct on its own terms -- it is a *fallback* for a slow hook. It becomes fatal only when combined with ISSUE-1, which kills the primary.

### ISSUE-4: an invalid command produces no model-bound signal (the original report)

A typo reaches neither the model nor the queue, so neither the hook nor the marker fires: 90s timeout.

**The premise of the original Slack thread is wrong.** There *is* a structured transcript entry:

```json
{"type":"system","subtype":"informational","level":"warning",
 "content":"Unknown command: /zzznotacommand","timestamp":"..."}
```

It is written at accept time and `stream_transcript.sh` already tails it into `events.jsonl`. No pane-scraping is required to detect a rejection.

### ISSUE-5: modal local commands wedge the agent, and preflight does not notice

`/cost`, `/model`, and `/login` open a panel. The panel occupies the pane, so **there is no input row**. The command never completes headless, so no completion entry is ever written.

Worse, the wedge outlives the send:

```
mngr message -m "/cost"        -> exit 0   (with a delivery marker)   no input row
mngr message -m "/zzzblocked2" -> exit 1   after 31.6s                never delivered
```

31.6s is `wait_for_tui_ready`'s 30s timeout. The agent stays wedged until someone presses Escape. `_preflight_send_message` does not catch it because these panels are not in `_DIALOG_INDICATORS`, so the send dies in a generic timeout rather than failing fast with a reason.

This is the strongest argument that **delivered** is not a sufficient postcondition for `mngr message`.

Rejecting these commands up front is a net improvement over timing out and wedging -- but it cannot be keyed on the command name, because modality depends on the arguments (`/model` wedges, `/model opus` does not). The reject condition must be the observed effect: no input row after a bounded settle window. Recovery is a single `Escape`, which is what a human does.

### ISSUE-6: busy and idle disagree about the same input

Identical input, opposite verdicts, decided only by whether the agent happened to be busy:

| agent state | `mngr message -m "/zzzbusytypo"` | mechanism |
|---|---|---|
| busy | **exit 0 in 2.6s**, "Successfully sent" | `enqueue` fired |
| idle | **exit 1 after 93.7s** | nothing fired |

On dequeue, the queued typo is rejected (`dequeue`, then `Unknown command: /zzzbusytypo`) and never reaches the model. So mngr **already** treats "queued, even if a typo" as sent. The inconsistency is that the idle path does not agree.

### ISSUE-7: `history.jsonl` dedups consecutive identical messages

Sending the same short message twice in a row appends only one entry. Reproduced end-to-end with the marker swapped in:

```
send #1 of '/zzzdedup777'  -> exit 0,  2.3s
send #2 of '/zzzdedup777'  -> exit 1, 93.0s
transcript: 2x "Unknown command: /zzzdedup777"   (both delivered)
history:    1 entry                              (second deduped)
```

Dedup is **consecutive-only** (`X, Y, X` appends three entries) and does not apply to the paste path: messages at or above 1024 chars render as `[Pasted text #N]` with an incrementing counter, so their `display` is always unique, and two *different* long pastes never collide.

Any marker built on `history.jsonl` must handle this. The two candidate guards are (a) read the last entry before Enter and, if `display` equals the message, fall back to another condition; or (b) force the paste path for all messages so the `#N` counter guarantees uniqueness.

**Unverified:** whether dedup compares against the previous entry of the whole file or per-project. In shared-config mode `history.jsonl` is global, so if the comparison is global, an interleaved write from another agent or the user's own terminal would make the hang **flaky rather than deterministic**.

### ISSUE-8: `mngr message` can return before the input buffer is clear

After `/clear`, a subsequent send can be concatenated onto residue in the input buffer. Reproduced through `mngr message` in **1 of 3** iterations: the message `/zzzrace1` was submitted as `/clear /zzzrace1`.

Raw `tmux send-keys` never reproduced it at any tested delay (3s, 6s; with and without an intervening Escape; Enter gaps of 0.1s, 0.2s, 0.5s), so it is specific to mngr's send path racing the session restart, not to the slash-command autocomplete popup eating Enter.

This is a live bug today; delivery semantics make it more likely by returning sooner.

### ISSUE-9: `wait_for_paste_visible` is close to vacuous for short slash commands

`_check_paste_content` normalizes the message's last 60 characters and substring-matches them against the **whole pane**. On an idle pane, before anything is typed:

- `/model` matches, because the startup banner renders `· /model`.
- `/compact` matches, because the worktree path contains `compact`.

The check passes before the paste has happened.

### ISSUE-10: the pane is not a reliable oracle for the input buffer

Two independent failure directions, both observed:

- The rendered input row read empty while the buffer still held `/clear ` (this is what makes ISSUE-8 invisible to a naive check).
- Hook stderr (ISSUE-2) paints text into the input row that is **not** in the buffer: a message typed over it submitted alone.

Additionally, Claude echoes submitted messages into the scrollback with the same prompt glyph, so "the first glyph line" is not the input row. Any pane-based buffer check must scope to the box between the final pair of rule lines, and must not be trusted as a sole postcondition.

### ISSUE-11: comments and docstrings assert behavior that does not hold

- `claude_config.py:617-619` states that without the `SessionStart` hook, `mngr message agent -m /clear` "would time out … even though /clear actually executed". Measured: `/clear` times out anyway on macOS (93.7s), because ISSUE-1 destroys the signal the hook fires into.
- `plugin.py:2206` docstring states the marker confirms submission "the moment the message is accepted rather than waiting on the (possibly slow) UserPromptSubmit hook". Measured: on an idle agent it never fires at all.

Logged in [uncertainties.md](../uncertainties.md).

### ISSUE-12: in shared-config mode, `history.jsonl` is global

With `isolate_local_config_dir=false` (the default for local agents), `CLAUDE_CONFIG_DIR` is unset and every agent, plus the user's own terminal, appends to `~/.claude/history.jsonl`. The `project` field is the only scoping. Any probe must filter on `project == $MNGR_AGENT_WORK_DIR` and must be bounded (`tail -n N`) rather than reading the whole file, which was already 3.5 MB on the test machine.

**Unverified:** whether `project` follows a `cd` inside the agent. Claude Code has a `CwdChanged` hook, which suggests it might.

## Design direction (proposal)

Not a decision. Recorded so the tradeoffs are visible.

### Return on delivery, guarantee settlement

Two facts, two checks:

1. **Delivered** -- the return condition. Replace the `enqueue` probe with a `history.jsonl` probe. It fires for every category A-F, before interpretation, so no category needs a special case. The token is epoch-ms, so `tui_utils` needs no change. The `SessionStart` `clear|compact` hack can then be deleted.

2. **Settled** -- a bounded postcondition before returning, so back-to-back sends are safe. Modal commands (category C) will fail to settle. That is the correct verdict, and it turns `/login` from a 90s timeout into an immediate, accurate "delivered; the agent is showing a modal".

Measured cost of the swap: every category returns in under 3s where all previously timed out at 90s.

| Case | before | after |
|---|---|---|
| normal prompt | exit 1, 93.6s | exit 0, 3.0s |
| invalid command | exit 1, 93.7s | exit 0, 1.9s |
| `/cost` | exit 1, 93.8s | exit 0, 1.9s |
| `/effort medium` | exit 1, 93.9s | exit 0, 1.9s |
| `/clear` | exit 1, 93.7s | exit 0, 2.0s |

### Reporting the outcome

Delivery is not execution. Once delivery is confirmed, a second probe over `events.jsonl` can classify the newest post-baseline event as `submitted` / `executed_locally` / `rejected` (ISSUE-4 gives the rejection its own structured entry). This runs **after** the return condition and never gates it.

### Open questions

1. What is a sound **settled** predicate? "Input row stably empty for N consecutive polls" is cheap but rests on the pane, which lies (ISSUE-10). For category D specifically, `claude_session_id_history` gains a new `"<sid> <source>"` line on restart -- a structured, mngr-owned restart-complete signal. Whether categories A, B, and E need anything beyond the delivery marker is untested.
2. Which dedup guard for ISSUE-7, and is the dedup comparison global or per-project?
3. Should category C fail (`delivered but not settled`) or succeed with a warning? A distinct exit code seems right, and `mngr message -m "/login"` arguably should be rejected upfront, since a headless OAuth picker can never be driven.
4. Does `history.jsonl` exist and stay reachable for docker and remote agents, where the config dir is always isolated?
5. Would an upstream **input-consumed** hook, fired once before interpretation, remove the need for all of this? `UserPromptExpansion` and `MessageDisplay` exist in Claude Code's internal hook schema; whether they are reachable from `settings.json` is untested.

## Test plan

Any implementation should be able to reproduce, and then fix, each of these against a real agent:

- Every category A-G on an **idle** agent returns promptly and with the correct verdict.
- Category F (busy) agrees with the idle verdict for the same input.
- The same short message sent twice in a row does not hang (ISSUE-7).
- `/clear` followed immediately by a message does not concatenate, over at least 10 iterations (ISSUE-8 reproduced at 1-in-3).
- `/cost` followed by a message either fails fast with a modal-specific error or succeeds after dismissal -- never a 30s generic timeout (ISSUE-5).
- A host lacking GNU coreutils behaves identically to one that has them, or fails loudly at startup (ISSUE-1, ISSUE-2).

**Warning:** the probe agent must be created and destroyed by the test. `history.jsonl` is global in shared-config mode, so a test that writes recognizable messages pollutes the developer's own history file.
