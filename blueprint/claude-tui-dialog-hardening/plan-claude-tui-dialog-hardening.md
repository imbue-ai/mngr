# Harden Claude agent against blocking TUI dialogs

Ensure that interactive Claude Code selectors/dialogs (e.g. the `/model` "Switch model?" prompt) never leave the client silently blocked after a message that mngr already delivered successfully.

## Overview

- **Problem:** A slash command (`/model fable`) or even a normal message can make Claude Code open a `❯`-style confirmation selector *after* the message is confirmed-delivered (enqueued). mngr's delivery contract is "the TUI accepted my input," so the send returns success (exit 0) and nothing detects that the agent is now blocked on a selector, waiting forever.
- **Core decision:** Add a new step that runs *after* delivery is confirmed to detect a blocking selector, and either auto-accept its default (up to a configurable depth) or raise a clear error — so a delivered-but-blocked agent never hangs silently.
- **Two independent depth knobs** on the Claude agent config, both defaulting to `0` (off) and both set to `5` in `default-workspace-template`'s `.mngr/settings.toml`: `auto_accept_prompt_depth` (post-submit selectors created by our send) and `auto_accept_preflight_prompt_depth` (dialogs already present when a send/start begins).
- **Three-way, machine-readable `mngr message` outcome** so the minds app can tell the cases apart: `0` = delivered and clean; `7` = delivered but a blocking dialog could not be resolved; any other non-zero = not delivered. `7` is a new, non-conflicting entry in mngr's central exit-code table.
- **Detection is pattern-based and generic** (a `────` rule line followed by an indented `❯` numbered option), so it catches new/unknown dialogs too — not just the current hard-coded caption list. It reuses the color-free `capture-pane -p` output (no ANSI stripping needed).
- **Also fixes a latent bug:** the readiness/leftover-input checks currently match the `❯` glyph anywhere, so an open selector (`  ❯ 1. …`) is mistaken for the input prompt. Detection is re-anchored to a line that *begins* with `❯` at column 0.
- **Motivation:** robust unattended operation (autonomous sandbox agents in minds) — dialogs that "we really just don't care about" should keep working instead of wedging the client, while interactive users (depth `0`) get an explicit error instead of a silent hang.

## Expected behavior

### Post-submit selector handling (governed by `auto_accept_prompt_depth`)
- Runs on **all** interactive Claude sends: slash commands, normal messages, and the initial `mngr create` message — only after `submit_message_and_confirm` has already confirmed delivery.
- After delivery is confirmed, mngr observes the pane for a fixed window (**2s**, a module-level constant) before concluding no dialog appeared — selectors can render a beat after submission.
- A blocking selector is recognized by the pane pattern: a horizontal-rule line (`────…`) followed by an indented numbered-arrow option line (leading whitespace + `❯` + a number, e.g. `  ❯ 1. Yes`).
- With `auto_accept_prompt_depth > 0`: mngr accepts the highlighted **default** by sending Enter, then re-observes; it keeps accepting while a selector is present, one Enter per unit of depth (so chained dialogs clear). Each acceptance logs at `info` and records a structured agent event.
- **Terminal condition:** seeing the bare text-input marker — a line beginning with `❯` at column 0 (no leading spaces) — means dialogs are done; stop early.
- With `auto_accept_prompt_depth = 0`, or when depth is exhausted and a selector is still up: mngr fails with a **distinct** "delivered-but-blocked" error (a dedicated `DialogDetectedError` subtype), which the `mngr message` CLI maps to **exit code `7`**. The message was delivered; the error includes the captured selector block. This is the ONLY path that yields exit `7`.
- **Busy-agent semantics:** success = no blocking selector detected during the window. The bare input marker is only an early-exit fast path and is **not required** — a busy, selector-free agent passes. If the window ends with neither a selector nor the input marker (an unexpected/weird state), mngr still marks success but emits a `logger.warning`.
- No global wall-clock cap; the depth counter plus the per-iteration 2s window bound the work.

### Preflight dialog handling (governed by `auto_accept_preflight_prompt_depth`)
- The existing preflight check (run at the start of a send) gains the same generic `────`+`❯`-number pattern, in addition to the current fixed-caption indicators (trust / API-key / theme / effort / cost) — so pre-existing and unknown dialogs are caught.
- With `auto_accept_preflight_prompt_depth > 0`: a dialog already present at send start (pattern- or caption-detected) is auto-accepted by default, same mechanics/terminal-condition as the post-submit step, using its own depth counter; raises `DialogDetectedError` (with the captured block) if it can't be cleared. This is a **not-delivered** outcome — the new message was never pasted — so it maps to a non-zero, **non-`7`** exit code (the existing generic error code), never exit `7`.
- **Exception — permission prompts stay a hard raise:** the `permissions_waiting` marker file remains an unconditional `DialogDetectedError` and is **never** auto-accepted by the depth knob. Permission prompts are a distinct class we are not *trying* to accept — that they happen to be acceptable is incidental; `auto_allow_permissions` (the hook) remains their intended path.

### Create/start readiness
- `auto_accept_preflight_prompt_depth` **also** applies during `wait_for_ready_signal`: an unexpected selector blocking startup is auto-accepted instead of hanging.
- If startup auto-accept can't clear the dialog (depth exhausted / persists), mngr raises `DialogDetectedError` (consistent failure type across the send and start paths, with the captured block) rather than the generic readiness timeout.
- Independent of `auto_dismiss_dialogs`: that knob still pre-dismisses known startup dialogs via config flags; this is an always-on runtime fallback for any that slip through.

### Readiness / leftover-input correctness
- "TUI ready" is only satisfied by a line that *begins* with `❯` at column 0 — an open selector (`  ❯ …`, indented) no longer counts as ready, so mngr never pastes into a dialog.
- Leftover-input detection is likewise anchored to a column-0 `❯`, so a selector line is never misreported as stranded input text.

### Exit-code contract (so the minds app can separate the cases)
- `mngr message` gains a three-way outcome, keyed off *whether the message was delivered* and *whether a post-submit dialog remained*:
  - **`0`** — delivered and no unresolved blocking dialog (includes the case where dialogs were auto-accepted and cleared).
  - **`7`** — delivered, but a blocking dialog could not be resolved (post-submit, `auto_accept_prompt_depth` off or exhausted). New named exit code.
  - **any other non-zero** — not delivered (preflight-blocked, start failure, timeout, no such agent, send/paste failure, etc.).
- The system_interface (which sends via `mngr message` as a subprocess and reads the return code) is updated to recognize `7` as a distinct, non-fatal "delivered-but-blocked" state — mirroring the existing `EXIT_CODE_PROVIDER_INACCESSIBLE` (6) pattern it already special-cases — rather than collapsing everything to a success/failure boolean.
- The desktop client surfaces "delivered but the agent is stuck on a dialog" distinctly from "message failed to send," so the user learns the agent needs attention instead of seeing a generic error or a false success.
- Multi-agent `mngr message` precedence: exit `7` only when every targeted agent was delivered and at least one is blocked with none genuinely failing; any real non-delivery outranks `7`.

### Unchanged
- The delivery/confirmation contract itself is untouched: a `/model fable` still confirms via its `enqueue` transcript record and reports delivered. The new behavior is purely additive hardening that runs afterward.
- Non-Claude agents (codex, antigravity) are unaffected — they get a no-op default for the new step.

## Changes

### `default-workspace-template` (`.mngr/settings.toml`)
- Under `[agent_types.claude]`, set `auto_accept_prompt_depth = 5` and `auto_accept_preflight_prompt_depth = 5` (alongside the existing `auto_dismiss_dialogs` / `auto_allow_permissions`). Separate PR against the dwt repo.

### `mngr_claude` plugin
- Add two `NonNegativeInt` fields to `ClaudeAgentConfig`: `auto_accept_prompt_depth` and `auto_accept_preflight_prompt_depth`, both default `0`.
- Add a generic selector detector: recognizes a `────`-rule line followed by an indented `❯`-number option line in `capture-pane -p` output; extracts the selector "block" (from the rule line through the last option) for logging/errors.
- Add a shared accept-loop helper: given the agent + tmux target + a depth budget, repeatedly detect a selector, accept the default (send Enter), and re-observe within the 2s window; stop on the column-0 input marker, on depth exhaustion, or when no selector remains; return whether a selector still blocks. Emits an `info` log + a structured agent event per acceptance.
- Implement the Claude override of the new shared post-submit hook (see below) using `auto_accept_prompt_depth`; when it cannot clear, raise the new **delivered-but-blocked** exception subtype (see mngr core) with the captured block — distinct from the preflight/not-delivered `DialogDetectedError`.
- Extend `_preflight_send_message`: keep the `permissions_waiting` hard raise first; then add generic-pattern detection alongside `_DIALOG_INDICATORS`, and run the accept-loop under `auto_accept_preflight_prompt_depth` before raising.
- Extend `wait_for_ready_signal` to run the same preflight accept-loop when a selector blocks startup, raising `DialogDetectedError` on failure to clear.
- Add a new `DialogIndicator` (or equivalent) for the generic `────`+`❯`-number pattern so it participates in the existing indicator flow.
- Re-anchor `TUI_READY_INDICATOR` to match only a line beginning with `❯` at column 0 (e.g. a line-anchored pattern), and update `_detect_preexisting_input_text` to the same column-0 anchoring so a selector line isn't treated as leftover input.

### `mngr` core (`agents/tui_agent.py`, `agents/tui_utils.py`, `errors.py`, `cli/exit_codes.py`, `cli/message.py`, `api/message.py`)
- Add a post-submit extension point on `InteractiveTuiAgent.send_message` (default no-op) that runs after `submit_message_and_confirm` succeeds; `ClaudeAgent` overrides it. Non-Claude agents keep today's behavior.
- Ensure `wait_for_tui_ready`'s matching honors a line-anchored indicator (the Claude indicator becomes column-0-anchored); confirm the shared substring/pattern matching supports this without regressing other agents.
- Introduce the 2s observation window as a named module-level constant (user-configurable later).
- Add a new "delivered-but-blocked" exception subtype (subclass of `DialogDetectedError`/`SendMessageError`) so it is distinguishable from a not-delivered failure while inheriting the existing message/handling.
- Add `EXIT_CODE_MESSAGE_DELIVERED_BUT_BLOCKED: Final[int] = 7` to `cli/exit_codes.py` (next to the existing 0-6 codes; avoids 10/11 in `api/connect.py`).
- In `cli/message.py` (and the JSONL path), when the only failures are delivered-but-blocked, exit `7`; otherwise keep the existing non-zero code. Ensure `api/message.py`'s `failed_agents` preserves enough of the exception (type or a flag) for the CLI to make that determination per agent.
- Update the `mngr message` help/contract text to document the three exit codes.

### `default-workspace-template` — system_interface (`apps/system_interface/.../agent_discovery.py`, `agent_manager.py`, `server.py`) + desktop client
- Teach the message path (`MngrMessenger.send_to_agent` / `_send_to` / `AgentManager.send_message_to_agent`) to distinguish return code `7` from other non-zero codes, via a named `EXIT_CODE_*` constant (mirroring `claude_auth.py`'s existing `EXIT_CODE_PROVIDER_INACCESSIBLE` handling) — returning a richer result than a bare success/failure boolean.
- Surface the "delivered but blocked on a dialog" state through the send endpoint and into the desktop-client UI, distinct from a send failure.
- (System_interface is vendored from this mngr checkout, so this rides the same `minds-v*` bake/release; note the cross-repo coordination.)

### Scope / placement
- All selector-pattern knowledge, config fields, and accept logic live in `mngr_claude`; the shared pipeline only gains the no-op hook point and the anchored-readiness support. Codex/antigravity are untouched.

### Tests + changelog
- Unit tests for the detector (positive: live `/model` layout; negatives: input row, ordinary output containing `❯`), the accept-loop (depth 0 raises; depth N accepts/clears; chained selectors; busy-agent no-selector success; ambiguous-state warning), the column-0 readiness/leftover anchoring, and the `permissions_waiting` hard-raise exemption.
- Exit-code tests: `mngr message` returns `7` only for delivered-but-blocked (post-submit), `0` when cleared, and a non-`7` non-zero for preflight-blocked / not-delivered; multi-agent precedence.
- Changelog entries per touched project (`libs/mngr`, `libs/mngr_claude`) plus the separate `default-workspace-template` PR (settings + system_interface + desktop client).
