Support for the `grok` agent type, provided by the separately installed `imbue-mngr-grok` plugin.

- The plugin catalog lists `imbue-mngr-grok` (entry point `grok`) as an independent agent-type plugin, not recommended by default, so the install wizard offers it unchecked among the extras. Its detection signal, `GrokSignalCheck`, accepts only Grok Build's `grok X.Y.Z (<hash>)` version output; the wizard checks signals only for recommended plugins, so it does not run this one.

- Docs: `grok` joins the external plugin agent types in `all_supported_agents.md`; the plugins and agent types concept pages name the separately installed agent-type plugins; the config README lists grok's `settings_overrides` among the patches that accumulate across config layers; `mngr message`'s help names grok, and the evidence that confirms its sends, among the agents its delivery guarantee covers, and says that a `!` shell command sent to claude or grok is best-effort like a slash command (so does the warning logged when such a send goes unconfirmed); and the generated capability matrix gains a `grok` column.

- `imbue.mngr.agents.tui_utils` gains `is_shell_command_message`, the check for a leading-`!` shell-mode message, shared by the Claude and grok plugins.

- Test support: the all-agent-types plugin-isolation fixture now loads `codex` and `grok` too, the common-transcript conformance meta-test expects a `grok` emitter, and the release harness's `agent_state_dir_by_name` is public for plugin release tests that look an agent's state dir up by name, as are its seed-command, recall and message-journey busy prompts (`SEED_TOOL_COMMAND`, `RECALL_PROMPT`, `MESSAGE_JOURNEY_BUSY_PROMPT`) for stub model servers that script replies to them.
