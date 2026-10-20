Fixed intermittent "Sending…/Queued…" hangs (and eventual send failures) when
messaging a codex agent after a turn that produced substantial output. The
pre-send readiness poll looked for codex's `/model to change` header, which sits
at the top of the TUI and scrolls out of the visible pane once a turn renders
enough output -- so the poll could not confirm the composer was ready and
withheld the paste until it timed out. Readiness now keys off the composer
prompt glyph (`›`), which is pinned at the bottom input line and never scrolls
off -- the same approach `mngr_claude` already uses with `❯`.

The codex agent type gains two settings, `output_style` and `append_system_prompt`, that a
create template can set so a role describes codex's behaviour without spelling out argv.

Codex has no output-style concept, so both land in the top-level `developer_instructions` key of
the per-agent `config.toml` -- the key that appends to codex's built-in instructions (unlike
`model_instructions_file`, which replaces them). The `append_system_prompt` blocks go in first,
in stack order, then the style file's body verbatim, frontmatter block included, so a style
reads the same whichever harness runs it. The style comes last so it is the nearest instruction
to the model, matching how a harness with a real output-style setting applies the style over the
prompt. Style names are resolved from `.agents/output-styles/` in the work dir.

Known limit: a style that suppresses a harness's built-in prompt cannot behave identically here,
because `developer_instructions` can only append.

A message submitted to a codex agent while a turn is running is now confirmed as accepted the
instant it is queued, rather than hanging until the running turn ends. codex queues such a
message without firing `UserPromptSubmit`, so the `active` marker -- the previous sole evidence
of submission -- does not advance until the turn finishes. Send confirmation now also watches the
queued-input sidecar the patched codex binary appends to on every enqueue, so a queued message
confirms immediately (the two probes are OR-ed; a started message still trips the marker). Agents
on an older codex binary without the sidecar are unaffected: that probe simply never fires.

Codex agents now enforce the same PreToolUse policy guards claude does. `build_codex_hooks_config`
adds a `PreToolUse` entry that runs the workspace's existing guard scripts from the work dir
(`$MNGR_AGENT_WORK_DIR/system/scripts/`): block a command that pipes into `tail`/`head`, block
`git rebase` / `git commit --amend|--fixup` / `git pull --rebase`, and rewrite every bash command
with the OOM self-tag + the agent's git identity. Codex speaks claude's hook protocol (same
`PreToolUse` payload and the exit-2+stderr block convention, verified live under code mode), so it
reuses the exact same scripts with no new logic. See `system/scripts/POLICY_HOOKS.md`.

Fixed the command-rewrite guard failing under recent codex (verified against codex-cli 0.146.0),
which surfaced as repeated `PreToolUse hook returned updatedInput without permissionDecision:allow`
errors and meant the OOM self-tag + git-identity rewrite never applied on codex. Newer codex
requires a hook that returns `updatedInput` to also carry an explicit `permissionDecision: "allow"`;
`build_codex_hooks_config` now invokes the rewrite script with a `--codex` flag that adds that
decision. The two block guards are unaffected (they return no `updatedInput`), and the rewriter's
`allow` does not weaken them -- codex honors an earlier block over a later allow (verified live).

Codex agents now also run the tk workflow-discipline guards, matching claude: `build_codex_hooks_config`
adds the require-steps soft reminder and the non-standalone `tk start`/`close` block to `PreToolUse`,
the open-steps carryover reminder to `UserPromptSubmit` (with a `--codex` flag so it is emitted as
`additionalContext` JSON -- codex rejects UserPromptSubmit stdout that starts with `[`), and the
open-steps stop nudge to `Stop`. All are the same dwt scripts claude runs, from the work dir. Verified
live against codex-cli 0.146.0. See `system/scripts/POLICY_HOOKS.md`.

Deleted the retired marker-lifecycle files that the app-server rewrite had already
orphaned: `set_active_marker.sh`, `clear_active_marker.sh`, `codex_marker_state.sh`,
`subagent_started.sh`, `subagent_stopped.sh` and their four test files. They tested
helpers (`run_codex_hook`, `install_common_transcript_flush_stub`,
`SUBMIT_WAIT_CHANNEL_PREFIX`) that no longer exist, so they failed at collection.
