mngr no longer writes hook commands of its own choosing into a codex agent's `hooks.json`.

`build_codex_hooks_config` now emits only mngr's own bookkeeping hook -- `UserPromptSubmit` -> `record_session_pointers.sh`, which records the rollout session id and transcript path. The fixed list of guard commands it used to carry is gone.

Codex loads hooks from every active config layer and does not let a higher-precedence layer replace a lower one, so a repo that wants guards on its own agents puts them in its `.codex/hooks.json`; they run alongside mngr's. Nothing is needed from mngr for that: the plugin already marks the work dir trusted and passes `--dangerously-bypass-hook-trust`, which is what a project layer's hooks require.
