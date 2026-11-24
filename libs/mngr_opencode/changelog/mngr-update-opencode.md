`mngr upgrade` covers opencode agents. It rewrites the launch script (`commands/opencode_launch.sh`) and the lifecycle plugin (`mngr_opencode_plugin.ts`); `opencode.json`, `AGENTS.md`, other plugins in the agent's `plugin/` dir and every other file stay as they are. An idle agent restarts and resumes its session.

- An agent whose launch command does not run `commands/opencode_launch.sh` is skipped as unsupported until `mngr create NAME --reuse --update` re-creates it.

- The upgrade is `ready` once the restarted launch script writes `opencode_ready`, after opencode's server is up and the agent's session exists; otherwise it fails at `ready`.
