# Unabridged Changelog - mngr_grok

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_grok/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-11-11

Fixed: a host no longer idle-stops itself out from under an agent that is working.

An agent's activity file was refreshed only while a turn was in progress. An agent whose turn ended with work it started still running has no turn marker -- that is what `RUNNING_BACKGROUND` means -- so nothing refreshed the file, and the host's idle watcher saw an agent that had gone quiet. Every provider defaults to a 800-second idle timeout, so against a four-hour lease a host could stop itself about thirteen minutes into work that had hours left to run. The same stale file is what `mngr ls` reported as `idle_seconds`, so a listing showed an agent idling for hours beside a state saying it was working.

Work outstanding past a turn now counts as activity, so the host stays up for it and the idle time reads as zero. Only agents created from this version report it: the activity tracker and the work-lease library are both written into an agent's own area when the agent is made.

`HOOK_WORK_LEASE_TTL_SECONDS` now sets how long the work leases the hook handler writes hold for, the way it already did for claude; the handler had a fixed 4 hours that nothing could change. The default is now 24 hours, the same one claude's turn-end hook uses, and a value that is not a positive count of seconds is ignored rather than used to write a lease that is expired on arrival.

The README's Lifecycle section says the same: a lease nothing ends sooner expires a day after the last `Stop` that listed its task, or `HOOK_WORK_LEASE_TTL_SECONDS` after it for an agent whose environment sets that.

## 2026-11-09

Test-only (MIND-182): `test_ratchets.py` declares `pytest.mark.ratchet_scan(_DIR)` in place of the unused `xdist_group` marker, so this project's ratchet tests load their file scan during pytest setup instead of inside the first ratchet test's 10s timeout.

## 2026-11-05

Fixed: a grok agent now reports mngr's new `RUNNING_BACKGROUND` state while the work its turn left running is still outstanding, rather than `WAITING`.

grok reports its in-flight work on every end-of-turn `Stop`, but mngr kept only the subagents from that snapshot and discarded every other kind. A turn that ended with a background shell command running therefore read `WAITING` -- indistinguishable from an agent waiting on you -- right up until the command finished and woke it. The turn-end hook now declares one work lease per in-flight task whatever its kind, so that agent reads `RUNNING_BACKGROUND` and `mngr ls` names the work holding it.

A background subagent used to be covered a different way, by holding the turn marker open, so such an agent reported `RUNNING` as though a turn were in progress. It is leased like any other background work now and reports `RUNNING_BACKGROUND`, which says what is actually true: the turn has ended and something other than you will move the agent next. A subagent still running inside a turn keeps the marker open exactly as before.

A lease ends when its work does -- when the task is absent from the next turn's snapshot, when a subagent ends, fails or is cancelled, or when the session ends -- and otherwise expires on its own deadline. A permission dialog a background subagent is waiting on still outranks the work it left running, so such an agent reports `WAITING` on you rather than `RUNNING_BACKGROUND`.

The grok binary-contract release tests and five grok e2e release tests (a send while busy, a tool call, a subagent, a restart, clone and adoption) are marked `harness_internals(harness="grok")`, so `-m "release and harness_internals(harness='grok')"` runs every release test that pins a grok internal mngr reads.

The grok binary-contract release test for a background subagent now checks that the agent reports `RUNNING_BACKGROUND`, with a lease naming the subagent, once the turn that spawned it ends, `RUNNING` again from the subagent's end through its wake turn, and `WAITING` once that turn ends; it had still expected the turn marker to stay on throughout, which stopped being true when background subagents moved to work leases.

The README's Lifecycle section describes the same: a background subagent or background shell a turn leaves running makes the agent `RUNNING_BACKGROUND` through a work lease, and only a subagent running inside a turn keeps it `RUNNING`. A permission card such a background subagent shows once that turn has ended leaves the agent `RUNNING_BACKGROUND`, and `waiting_reason` reads `PERMISSIONS` only for a card open during a turn.

## 2026-11-03

New package `imbue-mngr-grok`: the `grok` agent type, which runs xAI's Grok Build CLI (`grok`) as an mngr agent, configured under `[agent_types.grok]`. It is tested with Grok Build 1.0.62; the README documents every field and behaviour.

- Lifecycle: mngr's hooks in the agent's own grok home mark the agent RUNNING from a prompt's submission until grok records that turn's `turn_completed`, and while any subagent runs; the transcripts are flushed before the agent reports WAITING (a turn cancelled with Ctrl+C can report WAITING first, at grok's `StopCancelled` hook), a turn grok ends without reporting it (such as one cancelled with Ctrl+C before its first reply, whose prompt grok puts back in the input box) ends at grok's idle notification about a minute later, or, when mngr pressed that Ctrl+C while only one top-level session had a turn running, as soon as mngr has cleared what grok put back (5 seconds after the press when grok first shows the input box empty), and `mngr list` shows a `waiting_reason` (`PERMISSIONS` while a permission card is open). mngr finds a running agent by its `grok` process, also in batched remote listings.

- Messages: a message sent while grok is busy is queued by grok, and `mngr message` confirms it from evidence that names that message, or, for a slash command that starts a session such as `/new`, from that session's start; a message starting with `!`, which grok runs as a shell command without firing a hook, is reported sent after a short wait, with a warning. mngr never types into grok's login, folder-trust, consent or paywall screens or a permission card, and fails with an error naming the screen instead (a `mngr create --reuse --update` stopped by one at startup keeps the agent); it refuses key presses that would save always-approve, cut a turn short or quit grok, and presses Ctrl+C only to cancel a running top-level turn with an empty composer, then, when only one top-level session has a turn running, waits up to 5 seconds for grok to end it; a turn grok drops instead (one cancelled before its first reply) mngr ends itself, at once after clearing what grok put back in the input box (the prompt, or its `[Pasted: N lines]` chip) or at the 5-second mark when grok first shows the input box empty, and fails the press, leaving the turn recorded (the agent reads RUNNING), when that clear or mngr's end of the turn does not take. The cancel leaves background subagents running.

- Sign-in: with `share_auth` (on by default) every agent uses the user's grok credential through `GROK_AUTH_PATH`; `XAI_API_KEY` also works, and a remote host needs its own `grok login --device-auth`. With `share_auth = false` no stored login reaches the agent, which then needs `XAI_API_KEY`; without it the create fails on grok's login screen.

- Isolation: each agent has its own `GROK_HOME`, and by default (`isolate_from_claude_config`) grok imports none of the user's Claude Code or Cursor configuration; the user's Claude `permissions.deny` rules still apply.

- Folder trust: mngr records grok's trust for the work dir before launch, only with consent (an existing grant, `--yes`, `auto_dismiss_dialogs_at_startup`, or an interactive prompt; otherwise the create fails), and writes a new grant to the user's own trust store.

- Privacy: grok's telemetry and analytics follow grok's defaults and the xAI account's settings; mngr turns off codebase upload and workspace teleport. Every agent's config records grok's privacy banner as acknowledged. Grok's memory stays off unless `memory = true`.

- Sessions: `mngr stop` and `mngr start` resume the agent's session; `--from` and `--adopt` copy a session in and fork it; `mngr destroy` preserves the transcripts and sessions under `preserved/`.

- Installation and updates: provisioning installs `@xai-official/grok` with npm when it is missing and fails unless the installed `grok --version` runs, fails early when `grok --version` reports another program (such as the community `grok-cli`), `version` pins and verifies a version (`version_mismatch = WARN` only warns on a mismatch), and grok's own updater stays off unless `update_policy` is `AUTO` (or `ASK`, which behaves like it).

- Configuration: `model`, `reasoning_effort`, a role from `append_system_prompt` and `output_style` (passed with `--rules`), `auto_allow_permissions`, and `settings_overrides`, a patch onto the agent's `config.toml` whose `[permission]` table is the agent's permission policy.

Grok agents no longer turn off grok's telemetry, trace upload, analytics, feedback and campaigns: they follow grok's defaults and the xAI account's settings, as the other agent types' CLIs follow theirs. The `pin_privacy_settings` field is gone, and so are the agent's `requirements.toml` and `GROK_MANAGED_CONFIG=0`, which only existed to enforce those pins. mngr still turns off grok's codebase upload and workspace teleport.
