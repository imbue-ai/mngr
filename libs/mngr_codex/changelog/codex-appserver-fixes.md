The codex app-server daemon sidecar window now inherits the agent environment (the same env the
visible `--remote` TUI already sources). Turns run in the daemon, so it is where codex fires its
hooks; the detached `tmux new-window` previously started with a bare environment, so every hook
resolved `$MNGR_AGENT_STATE_DIR/...` / `$MNGR_AGENT_WORK_DIR/...` to a nonexistent path and died
with exit 127 -- silently disabling the transcript-marker writer and the PreToolUse safety guards.

The visible `codex --remote` terminal now launches with `--dangerously-bypass-hook-trust`. codex
parks newly-discovered command hooks as untrusted ("in review") until a human approves them in the
TUI, and an untrusted hook never runs -- not for a typed turn and not for a programmatic
(web/CLI) `turn/start`. The flag clears that review gate so the workspace's safety hooks (the
pipe/rebase blockers, the OOM + git-identity command rewrite, and the tk workflow guards) run on
every turn regardless of who sent it. It is consent-gated: it only takes effect on a workspace the
user has already trusted.
