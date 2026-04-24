# Investigation: `env claude` vs bare `claude` hang (PR #1336)

## TL;DR

PR #1336's "bare claude hangs, env claude works" is **a race in mngr's
lifecycle detection**, not a kernel-level fork/exec difference in the claude
binary. Reproduced locally 8/8 bare → fail, 5/5 env → pass, and 5/5
bare-plus-half-second-sleep-in-mngr → pass. The `env` prefix is a timing
hack — it adds ~100ms of delay that lets the race resolve in the agent's
favor, but so does any other delay. Described as "exited without producing
output" in mngr and as "hangs indefinitely with 0 bytes" in the PR body,
the effect is the same and has nothing to do with claude's startup.

## What's actually happening

Timeline from instrumented `HeadlessClaude.stream_output` in my Modal/gVisor
sandbox, running `mngr ask "hi" --disable-plugin modal` without the env-wrap:

```
DBG[0.000s] before wait_for_stdout_file, exists=True,  size=0,    is_finished=True
DBG[0.069s] wait_for_stdout_file returned True, size=0, is_finished=True
DBG[0.122s] before tail_until_done, size=0, is_finished=True
DBG[0.235s] post-loop any_yielded=False, result_error=None, size=0, is_finished=False
DBG[0.795s] AFTER 0.5s sleep size=0,    is_finished=False
DBG[2.357s] AFTER 2.0s sleep size=4344, is_finished=True
```

Step by step:

1. **t=0.000s**: `stream_output` starts. The tmux pane still shows the *idle
   bash prompt* from before `send-keys` fired, so
   `_is_agent_finished()` returns `True` (lifecycle = DONE). The stdout
   file already exists and is empty — bash's `> stdout.jsonl` redirection
   opens the target file before the command starts running (verified: a
   `bash -c 'sleep 2; echo hi' > file` creates `file` at 0 bytes
   immediately). So `_wait_for_stdout_file` returns True on its very first
   poll, bypassing the `_startup_grace_seconds = 10.0` timeout.
2. **t=0.122s**: mngr constructs `_StreamTailState` and calls
   `tail_until_done`. The loop condition is
   `while not got_result and not self.is_finished()`. `is_finished()` is
   still returning True from the stale pane state → loop is entirely skipped.
3. Final drain block reads the stdout file. Size is still 0. No events parsed.
   `result_error=None`, `is_any_output_yielded=False`.
4. **t=0.235s**: `stream_output` hits
   `if not is_any_output_yielded: self._raise_no_output_error()` and raises.
   By this point the pane has actually started running the command so
   `is_finished()` now correctly reports False, but we're already past the
   check.
5. **t=2.357s**: claude has finished its 43ms of work, its stdio has flushed,
   the file now contains the full 4344 bytes including the
   `"type":"result","is_error":true,"result":"Not logged in · Please run /login"`
   line. Nothing reads this — mngr already raised.

The bug, in one line: `_StreamTailState.tail_until_done`'s loop entry check
uses the same `is_finished()` oracle that `_wait_for_stdout_file`'s
startup-grace guard was added to defend against, but *without* the startup
grace.

## Empirical verification

Ran `mngr ask "hi N" --disable-plugin modal` with `ANTHROPIC_API_KEY` set,
N from 1..8, in this Modal/gVisor sandbox:

| Variant | Runs | Result |
| --- | --- | --- |
| `{cmd_str}` (bare, as on main) | 8 | 8/8 `NO-OUTPUT` ("exited without producing output") |
| `env {cmd_str}` (PR #1336) | 5 | 5/5 `GOT-ERROR` ("claude returned an error: Not logged in") |
| `{cmd_str}` + `time.sleep(0.5)` injected before `_StreamTailState(...)` | 5 | 5/5 `GOT-ERROR` |

The 0.5-second sleep is not an exec, not a process group change, not an FD
manipulation. It's just time. It wins the race for the same reason env does.
Conclusion: the fix is timing, the mechanism is the lifecycle race, and the
exec layer has nothing to do with it.

## Why the PR's "0 bytes" framing was misleading

`mngr ask` emits `"claude exited without producing output:\n<tmux pane
capture>"` when `is_any_output_yielded=False` and `result_error=None`. The
pane capture typically shows only the typed command line with no output
after it (because the agent got destroyed by the CM's `finally` block before
claude's stdio flushed and the pane redrew). That visual — command typed,
nothing after — is what got described as "hangs indefinitely with 0 bytes."
It's not a hang; it's mngr tearing down the pane faster than claude flushed.

The stdout and stderr files really are 0 bytes *at the moment mngr inspects
them*, so the PR's claim isn't wrong — but attributing it to claude is
incorrect. Claude wrote its output, mngr just never read it.

## Why the ablation table in the PR body is misleading

| Ablation | What it actually tested | What the PR summary said | Why the framing misleads |
| --- | --- | --- | --- |
| 6 (env only) | `probe_before` (includes `timeout 20 claude --print "hi"` primer) + `env` wrap on real call | "env claude only, no other probes or flags — pass 3/3" | The primer is itself a full claude run that can warm the lifecycle state; "env only" at the PR description level is inaccurate per commit `6feb469c6` |
| 7 (diagnostics + env) | `type claude` / `hash -t claude` probes + env wrap | "no shell alias, no PATH shadowing" | Correct, but irrelevant — the bug was never about shell lookup |
| 8 (absolute path) | `/root/.local/bin/claude` with `probe_diag` | "fail 1/1" | One run. Race timing varies; absolute path vs bare adds ~0 delay; this doesn't prove anything |
| final `b2036ed20` | `env` prefix, probes removed, `--debug all --debug-file` removed | "14/14 passing" | Pass count aggregates earlier run windows with different configurations |

The "any intermediate fork+exec fixes the hang" conclusion is correct in
effect but wrong in mechanism. It's not the fork+exec — it's the ~100ms of
wall time that env or timeout contributes.

## Child-process state comparison (done earlier, still relevant context)

Exhaustively verified that `bash→exec(claude)` vs `bash→exec(env)→exec(claude)`
produce identical child process state at claude's `_start` moment: argv,
environ, FDs (bash's fd 255 has FD_CLOEXEC), signal mask, signal dispositions,
pending signals, PGID, SID, controlling TTY, termios, cwd, umask, rlimits,
CPU affinity, personality, auxv (minus AT_RANDOM random bytes),
seccomp filter, rseq (`ENOSYS` under gVisor in both), argv[0] (both
`"claude"`, since execvp does PATH→path lookup but keeps argv[0] = the short
name bash passed). There is no kernel-visible state difference that could
make claude hang in one path and succeed in the other.

This state comparison rules out every mechanistic theory the PR's commit
message raises — "signal mask, controlling-TTY session, an FD lingering
between the fork and the exec" — leaving only timing, which is what the
reproduction confirmed.

## Why the ANTHROPIC_API_KEY fix (`fdc68196e`) is real

Separately from the race, before `fdc68196e` the env file didn't contain
`ANTHROPIC_API_KEY`, so claude actually was unauthenticated and would exit
with "Not logged in" in whatever time it took (~43ms in my sandbox). Post-fix,
the key is present and claude succeeds. That's a legitimate correctness fix.
But the PR bundles it with the env-wrap as if both are addressing the same
underlying issue; they aren't. The env-wrap masks a different mngr bug
(the race) that would cause test_ask_simple_query to be flaky even *with*
valid auth, because the race is about mngr's polling vs tmux's state, not
about what claude does.

## The actual fix

Move the startup grace protection from `_wait_for_stdout_file` into
`_StreamTailState.tail_until_done` (or equivalently, into the `is_finished`
check it uses). The loop should not treat `is_finished()==True` as
authoritative until either the file has grown past 0 bytes *or* the startup
grace period has elapsed.

Sketch:

```python
# base_headless_agent.py or headless_claude_agent.py
class _StreamTailState(MutableModel):
    ...
    startup_deadline: float  # monotonic time after which is_finished() is trusted

    def _authoritatively_finished(self) -> bool:
        if time.monotonic() < self.startup_deadline:
            # During grace: only trust is_finished() if the file has some
            # content already (i.e., we know the command definitely started).
            try:
                return self.is_finished() and self.host.read_text_file(self.stdout_path) != ""
            except FileNotFoundError:
                return False
        return self.is_finished()

    def tail_until_done(self) -> Iterator[str]:
        got_result = False
        while not got_result and not self._authoritatively_finished():
            ...
```

The headless claude's `_startup_grace_seconds = 10.0` is already threaded
through — just reuse it. With this fix, the env-wrap becomes unnecessary,
and so does any reliance on timeout as a wrapper.

Additionally: `HeadlessClaude.stream_output` should, when it hits the
`_raise_no_output_error()` path, first do one more re-read of the stdout
file and parse any trailing result event. This catches the case where the
file content does arrive during the grace window but after the tail loop
has exited.

## Recommendations for PR #1336

1. Drop the `env` prefix from `HeadlessClaudeAgent.assemble_command` and
   `ClaudeAgent.assemble_command`.
2. Land a fix for the tail-loop lifecycle race along the lines above.
3. Keep the ANTHROPIC_API_KEY propagation fix — it's correct and unrelated.
4. Remove the big multi-paragraph commentary about the unexplained mechanism
   from `assemble_command`; the real mechanism is documented here.

## Reproduction recipe

- Branch from `main` in mngr (no env-wrap, no API key propagation).
- `ANTHROPIC_API_KEY=... uv run mngr ask "hi" --format json --disable-plugin modal`
  in a Modal/gVisor sandbox (e.g. any forever-claude container).
- Expect `Error: claude exited without producing output: [tmux pane]
  <command line only>`.
- Inspect the agent's `stdout.jsonl` a few seconds later — you will find a
  full 4344-byte stream-JSON transcript including an `is_error:true` result.
  The file was there all along; mngr just raised before reading it.
