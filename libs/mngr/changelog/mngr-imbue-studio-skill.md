- Added `libs/mngr/skills_plugin/`: the `imbue-mngr-skills` Claude Code and Codex plugin (`mngr-help`, `find-agent`, `message-agent`, `wait-for-agent`) now lives in this repo and is exported to the new `imbue-ai/imbue-skills` marketplace on every push to `main`, replacing the hand-maintained `imbue-ai/mngr-claude-skills` repo, which is frozen. A test holds every `mngr <subcommand>` the skills name to the live CLI.

- `mngr extras claude-plugin` offers three plugins: `imbue-code-guardian`, `imbue-mngr-skills@imbue-skills`, and the new `imbue-studio-skills@imbue-skills`.

- Added `start_pid_death_watcher` and `is_pid_alive` to `imbue.mngr.utils.parent_process`, so a child can exit when a named process (its embedder) is gone; the grandparent death watcher is built on the same poll.

- Added `imbue.mngr.utils.agent_skills`, the reader for published skill files that the drift tests use.
