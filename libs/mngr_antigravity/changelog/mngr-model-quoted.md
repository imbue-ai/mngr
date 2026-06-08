Fixed: passing a model name (or any value containing spaces or parentheses) as an `agy` argument no longer breaks `mngr create`.

Passing `--model "Gemini 3.5 Flash (Medium)"` to an `antigravity` agent previously produced `agy --model Gemini 3.5 Flash (Medium) ...` in the shell-evaluated launch command, so bash word-split the value and parsed `(Medium)` as a subshell (`syntax error near unexpected token '('`). The underlying fix is in `mngr` (`agent_args` are now shell-quoted in `BaseAgent.assemble_command`); the `antigravity` plugin inherits it.

Note: the model is normally set via `settings_overrides` (a `model` key in the per-agent `settings.json`), which is the supported path and is unaffected. This fix covers the case where a model is instead passed explicitly as a CLI argument.
