Antigravity agents accept the workspace's role templates. `output_style` and `append_system_prompt` are now config fields on the agent type, and their text is written to the per-agent `~/.gemini/GEMINI.md` rule file that `agy` auto-loads -- under the per-agent `$HOME` mngr already provisions, so it never touches the source repo. Without a field to route them to, `mngr create --type antigravity --template chat` was rejected before it launched anything.

No model is pinned on the agent type. `agy` reads its model from its own `settings.json`, but the workspace's model bar is uniform across harnesses now (each harness writes its live model to `model_state.json`), so a pinned slug here would be a second source of truth that goes stale the moment someone runs `/model` in the agent's terminal.

An antigravity agent also stamps an `antigravity_process_started` marker in its state directory on every launch and resume, matching the `claude_process_started` / `codex_process_started` / `pi_process_started` markers the peer plugins write.

Consumers use its mtime to bound transcript staleness. This matters more for agy than for the others because agy resumes from its own conversation store: after a mid-turn restart the previous process's steps are still present, including a dispatched tool call that never completed, and without a process-start timestamp there is no way to tell that tail apart from live work.

The statusline now mirrors the model agy reports into the uniform `model_state.json` the workspace's model bar reads. Only `model` is written: agy has no separate effort or fast axis, since the tier is baked into the model id. The parse is deliberately tolerant -- if agy renames or nests that field, nothing is written and the bar falls back to showing no model, rather than disturbing the lifecycle work this script exists for.

The model parse reads agy's actual payload shape: `model` is an object (`{"id": "Gemini 3.7 Flash (High)", "effort": "high"}`), not a string, so the earlier string-only match never fired and no model was ever recorded. `effort` is deliberately not written -- agy has no separate effort axis and the tier is already inside that display name.
