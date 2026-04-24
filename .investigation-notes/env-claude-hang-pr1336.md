# Investigation: `env claude` vs bare `claude` hang (PR #1336)

## Background

PR #1336 ("upgrade offload to 0.8.1 + fix remaining release test failures") landed
two independent changes that it attributes to one root cause:

1. `ClaudeAgent.modify_env_vars` now propagates `ANTHROPIC_API_KEY` from
   `os.environ` into the agent env file (commit `fdc68196e`).
2. `HeadlessClaudeAgent.assemble_command` and `ClaudeAgent.assemble_command`
   wrap the `claude` invocation in `env` (commit `b2036ed20`).

The PR body and commit message for `b2036ed20` claim that bare `claude --print …`
(and equivalently `/root/.local/bin/claude …`) hangs indefinitely with 0 bytes
on stdout/stderr inside offload-release Modal sandboxes under gVisor, while
`env claude …` and `timeout 60 claude …` both pass. The mechanism is declared
"unexplained; empirically robust."

## What this investigation looked at

Whether the env-wrap is actually load-bearing, and if so, why.

## Setup

- Running in the same Modal/gVisor sandbox type as offload uses.
- The offload-release image adds dockerd/iptables/runc on top, but the test in
  question (`test_ask_simple_query`) is not a docker test, so the delta shouldn't
  matter for the claude invocation itself.
- Patched `libs/mngr/imbue/mngr/cli/headless_runner.py` locally to gate
  `_destroy_on_exit` on `MNGR_KEEP_AGENT=1` with a 600s sleep, so the tmux
  session could be inspected live. Patch has been reverted; tree is clean.

## Reproduction attempts

### Related but different failure

`MNGR_KEEP_AGENT=1 uv run mngr ask "hi" --format json --disable-plugin modal`
reliably produces `Error: claude exited without producing output`. Inspection
of the kept-alive tmux session shows:

- The `claude --print …` line was typed into the pane and executed.
- `$MNGR_AGENT_STATE_DIR/stdout.jsonl` contains 4344 bytes of real stream-JSON:
  `hook_started`/`hook_response` for SessionStart hooks, plus a final
  `"type":"result","is_error":true,"result":"Not logged in · Please run /login"`.
- `$MNGR_AGENT_STATE_DIR/stderr.log` is empty.
- Total elapsed time: <5 seconds.

This is not a hang — it's a fast auth-failure result that mngr misreports as
"no output." The misreport is itself a bug (see "Secondary bug" below), but it
is **not** the 0-bytes-for-60s hang the PR describes.

### Cannot reproduce the 0-bytes hang

Direct invocation of `claude --print "say hi"` from bash (both inside and
outside tmux) in this sandbox completes successfully or fails fast with an
auth error. It does not hang for 60+ seconds with 0 bytes on either stream.

### strace runs

Both `strace -f bash -c 'claude --print x > out 2> err'` and the env-wrapped
equivalent eventually appeared stuck, but the stuck-ness was clearly a ptrace
slowdown combined with nested-Claude-Code side effects (the traced claude
found the running claude as the holder of `/root/.local/state/claude/locks/2.1.119.lock`
via `kill(<lock-pid>, 0)` and proceeded into plugin-update subshells that
themselves spawn claude). The non-strace run of the same command succeeded.
Not evidence of the PR bug.

## Child-process state comparison: `bash → exec(claude)` vs `bash → exec(env) → exec(claude)`

For each item below I either verified empirically (small C program, /proc
inspection, or strace) or derived from POSIX/Linux semantics.

| State | Bare | Env-wrapped | Identical? |
| --- | --- | --- | --- |
| `argv[0]` (bare vs env) | `"claude"` | `"claude"` | Yes |
| `argv[0]` (abs path) | `/root/.local/bin/claude` | n/a | n/a |
| `environ` | inherited from bash | same | Yes |
| File descriptors | inherited; fd 255 has FD_CLOEXEC | same | Yes |
| Signal mask | inherited | inherited | Yes |
| `SIG_IGN` dispositions | inherited | inherited through env | Yes |
| Function signal handlers | reset to `SIG_DFL` | reset to `SIG_DFL` | Yes |
| Pending signals | empty (fork clears) | empty | Yes |
| sigaltstack | reset by exec | reset by exec | Yes |
| PGID / SID | set by bash before exec | same | Yes |
| Controlling TTY | inherited | inherited | Yes |
| `tcgetpgrp(tty)` | child's PGID | same | Yes |
| Termios state | cooked (bash restores before exec) | same | Yes |
| CWD, umask, rlimits | inherited | inherited | Yes |
| CPU affinity | inherited | inherited | Yes |
| Personality | reset to default per exec | reset to default per exec | Yes |
| seccomp filter | inherited if any (none in this case) | same | Yes |
| AT_EXECFN auxv | `/root/.local/bin/claude` | same | Yes |
| AT_RANDOM auxv | fresh random bytes | fresh random bytes | Different bytes, but semantically equivalent |
| rseq syscall | `ENOSYS` under gVisor | `ENOSYS` | Yes |
| Number of execve()s | 1 | 2 | **Differs** |
| Wallclock between bash fork and claude `_start` | T | T + ~1ms (env startup) | **Differs by ~1ms** |

`env` from coreutils calls `execvp` and nothing else of substance — no signal
manipulation, no environ manipulation, no termios, no setpgid. Verified via
strace and empirically: `bash -c 'trap "" SIGPIPE; env /tmp/sigh'` shows
SIGPIPE still `IGN` in the child, i.e., env is transparent to dispositions.

**Conclusion of this state comparison**: the only differences are the extra
execve() call and a ~1ms timing delay. Neither has a plausible mechanism to
cause a deterministic 60s hang in one case and success in the other.

## The PR's evidence, re-examined

The ablation table in the PR body presents ablation 6 as "env claude only, no
other probes or flags — pass 3/3." The ablation commit
(`6feb469c6 DIAGNOSTIC ablation 6: env wrapper instead of timeout`) actually
kept the `probe_before` block, which runs:

```
claude --version
claude --help
timeout 20 claude --print "just say hi"
```

before the real `env {cmd_str} …` call. That `timeout 20 claude --print "hi"`
primer is itself a successful claude invocation (wrapped in timeout, which
per the PR fixes the hang). By the time the real call runs, DNS, TLS
sessions, CA bundle loads, and the per-version lock file (`.../locks/2.1.119.lock`)
have all been touched. A "pass 3/3" after a passing primer is very different
evidence from "pass 3/3 with env as the only intervention."

Ablation 8 (absolute path, PR claims "fail 1/1") — one run. Baseline had 15/15
confidence; env had 3/3; absolute path had one datapoint. The table presents
this as equal-weight evidence. It isn't.

The final `b2036ed20` commit is the first one that strips the probe bundle
and *only* adds env-wrap. Its "14/14 passing" number in the commit message
includes earlier runs that did have probe-bundle content.

## Most likely explanation

The deterministic fix is `fdc68196e` (ANTHROPIC_API_KEY propagation). Before
that fix:

- `mngr ask`'s tmux pane sourced an env file missing `ANTHROPIC_API_KEY`.
- Bare `claude --print` called with no credentials falls through to the
  "Not logged in" path and exits quickly with an `is_error:true` result.
- mngr's `HeadlessClaude.stream_output` treats a stream-JSON stream that
  emits system/hook events + a final error result but **no `text_delta`
  events** as "no output yielded" and raises `_raise_no_output_error()`
  — which surfaces as the misleading "claude exited without producing
  output" error.
- Depending on timing of pane teardown, the `stdout.jsonl` as inspected
  post-failure can be truncated (the kept-alive-session version I saw
  had 4344 bytes; a just-killed pane could plausibly have less), which
  looks even more like a "0 bytes stdout" hang.

After `fdc68196e`, the key is present, claude authenticates, and the test
passes. The env-wrap added later in `b2036ed20` is very likely cargo: it
doesn't hurt (an extra execve is ~1ms) but it is not the mechanism that
fixes the test. The 14/14 pass rate after both changes landed is attributable
to the key fix; the ablation runs that showed "bare hangs" likely predate
reliable propagation of the key into the per-agent env file or were confounded
by Modal-sandbox state variation.

Alternative (less likely but possible): a real race in bun's multithreaded
startup under gVisor's scheduler, where the main thread needs to win a
race against a worker thread, and the ~1ms delay from an intermediate
execve consistently lets the main thread win. There is no direct evidence
for this, but I cannot rule it out.

## Secondary bug (independent of the env question)

`libs/mngr_claude/imbue/mngr_claude/headless_claude_agent.py` —
`_StreamTailState.tail_until_done` breaks out of the tail loop when it sees
a `result` event and stores the error in `self.result_error`. The caller
`HeadlessClaude.stream_output` then:

```python
if state.result_error:
    raise MngrError(f"claude returned an error:\n…")
if not is_any_output_yielded:
    self._raise_no_output_error()
```

This is correct IFF the tail loop reaches the `result` event. But the loop's
outer condition is `while not got_result and not self.is_finished()`. If the
agent's lifecycle says "finished" before `got_result` flips (e.g., the pane's
bash reaps claude fast, the lifecycle detection catches up, and the poll loop
exits on `is_finished()` before re-reading the file), then the final-drain
block at lines 160-177 is the only place the result event is parsed — and
that block is only entered when `not got_result`. It does parse the result
and set `result_error`, so this should work. But the timing of pane tear-down
could truncate the file between the last `read_text_file` and the final drain.

The *fix* would be: in `stream_output`, before calling `_raise_no_output_error()`,
re-read `stdout_path` one more time and parse the last line for a result event,
raising "claude returned an error" with that content. That way an `is_error:true`
result never gets silently reclassified as "no output."

Regardless of whether this specific code path fires, the user-facing failure
message "claude exited without producing output" is misleading when the
underlying cause is usually "claude produced output, which was an error."

## Recommendations

1. **Remove the `env` prefix** from `HeadlessClaudeAgent.assemble_command` and
   `ClaudeAgent.assemble_command`. Re-run `test_ask_simple_query` in
   offload-release at least 10 times with the ANTHROPIC_API_KEY fix still
   in place. If it passes, the env-wrap is confirmed cargo. If it fails,
   proceed to step 2.

2. If step 1 fails, **instrument with `strace -f -ttt`** wrapping the tmux
   pane's bash (not claude directly, to avoid the ptrace confound) and dump
   the syscall stream for a hanging bare-claude run. Compare against a
   passing env-wrap run. That will distinguish "claude never enters main" from
   "claude enters main but hangs on something" and give a real diagnostic.

3. **Fix `HeadlessClaude.stream_output`** to always surface result_error when
   present, regardless of whether text deltas were yielded, and to make one
   final read of the stdout file before raising "no output produced" so that
   late-written result events aren't lost. The current code's behavior makes
   every silent claude failure look like a hang.

4. Consider whether the ANTHROPIC_API_KEY propagation should be expanded to
   cover other auth mechanisms (OAuth credentials in `CLAUDE_CONFIG_DIR/.credentials.json`)
   that headless agents may legitimately not have — so failures surface as
   "no credentials" instead of fast silent exits.

## What I could not do

- Reproduce the PR's specific 0-bytes-for-60s hang. I can't run offload
  from this sandbox, so I cannot directly test step 1 above myself.
- Confirm or refute the "bun scheduler race" theory. Would need a
  reproducing environment plus bun-internals instrumentation.
