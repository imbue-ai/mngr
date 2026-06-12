Extended `specs/agent-plugin-parity/spec.md` (dimension D, "subagent-aware idle gating")
with a note on a related premature-idle failure mode: the RUNNING/WAITING marker tracks the
agent's turn/loop, not work it detaches from that loop. Documents how a CLI's
`run_in_background`-style tool (or a `cmd &`) can make the agent report WAITING while a
launched task still runs; that claude does not solve this for backgrounded bash (its Stop
hook waits only for sibling stop-hook processes and *excludes* `CLAUDECODE=1` bash-tool
tasks); and that the `CLAUDECODE=1` tag is nonetheless the discriminator that *would* make a
descendant-liveness wait safe. Distinguishes in-loop pending work, which the CLIs' idle
signals do gate correctly (agy's `fullyIdle:true`-plus-root-match clears only on the root's
final Stop, not interim Stops or a subagent's own idle; pi's foreground tools block the turn
so `agent_end` waits for them), from detached work, which is loop-scoped for claude, agy, and
pi alike. Adds a matching investigation-checklist question.

Refreshed `specs/agent-plugin-parity/spec.md` with the lessons from the pi-coding port now
that it is a real, near-`antigravity`-parity plugin (not a stub):

- Updated the state matrix and intro (pi is no longer framed as a stub; its rows flip to Y for
  lifecycle marker, subagent gating, readiness, transcripts, resume, and trust).
- Added a new dimension F, "Input delivery & submission confirmation" (renumbering the later
  dimensions): the tmux paste+Enter path is fragile (pi swallowed the first Enter), a CLI may
  expose a better programmatic input channel (pi injects via `pi.sendUserMessage`), and you
  must confirm a message actually started a turn (the marker), not scrape the pane.
- Added a "Your lever: shell hooks vs an in-process extension" section, including the
  in-process-extension hazard class (unhandled promise rejection crashing the host, jiti
  bare-specifier traps, emit-don't-tail transcripts).
- Sharpened existing dimensions with bugs hit during the port: the readiness "gating on an
  early banner loses the first message" failure mode; the trust "verify empirically what
  triggers the dialog -- pi triggers on `.pi`/`.agents/skills`, not CLAUDE.md/AGENTS.md, and
  trust guards config-loading, not prompt injection" warning; and the transcript
  derived-from-raw (claude/agy) vs independent-emission (pi) distinction.
- Extended the investigation checklist: a mechanism/input-delivery group, a
  packaging/distribution group (`PLUGIN_CATALOG`, signal check, `is_recommended`,
  publishability), and a "verify each answer against the running binary, not docs/source" note.
