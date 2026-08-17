Add an overridable `_determine_confirmation_policy` hook to the shared TUI agent so a port can widen the relaxed-confirmation set beyond slash commands (the default classification -- slash commands relaxed, everything else strict -- is unchanged). The Claude plugin uses this to confirm leading-`!` shell commands under the relaxed policy.

Add a `send_key_keystroke` helper alongside `send_enter_keystroke` for sending a single named tmux key (e.g. `BSpace`).
