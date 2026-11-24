`mngr upgrade` covers antigravity agents. It rewrites mngr's `commands/` scripts and shared shell libraries, and mngr's entries in the agent's agy `settings.json` (the `statusLine`) and `hooks.json` (the `mngr` hook), keeping every other key and hook; the common-transcript scripts are rewritten only where the agent has them. `GEMINI.md` and every other file stay as they are. An idle agent restarts and resumes its conversation.

- An agent whose launch command does not start mngr's background tasks and run agy under the agent's own home is skipped as unsupported until `mngr create NAME --reuse --update` re-creates it.

- The upgrade is `ready` once the restarted agy reports a state past initializing and authenticating to mngr's statusLine; otherwise it fails at `ready`.
