Hardened the Claude agent against interactive TUI selectors (e.g. the `/model` "Switch model?" confirmation) that could previously leave an agent silently stuck after a message was already delivered.

Two new `[agent_types.claude]` settings, both defaulting to `0` (off):

- `auto_accept_prompt_depth`: after a message is delivered, if it opened a blocking numbered selector, auto-accept the highlighted default (press Enter) up to this many times, clearing chained dialogs. At `0`, or if a selector persists, `mngr message` now reports the message as delivered-but-blocked (a distinct outcome) instead of hanging with no signal.

- `auto_accept_preflight_prompt_depth`: if a blocking dialog is already present when a send starts (or while the agent is coming up), auto-accept its default up to this many times before aborting. Independent of `auto_accept_prompt_depth` and of `auto_dismiss_dialogs`. Permission prompts (the `permissions_waiting` marker) are never auto-accepted -- they remain a hard error.

Detection is structural (a `────` rule line followed by an indented `❯`-arrow numbered option), so it also catches new/unknown confirmation dialogs, including in the pre-send preflight check. Each auto-accept is logged and recorded as an agent event capturing the selector text.

Also fixed a latent bug: the Claude readiness/leftover-input checks matched the `❯` glyph anywhere, so an open selector's indented option line could be mistaken for the input prompt. Both are now anchored to a line that begins with `❯` at column 0.
