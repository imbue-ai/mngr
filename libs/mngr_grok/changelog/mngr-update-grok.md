`mngr upgrade` covers grok agents. It rewrites mngr's `commands/` scripts and shared shell libraries, and mngr's hooks file (`hooks/mngr.json`) and hook handler in the agent's grok home; the common-transcript scripts are rewritten only where the agent has them. `config.toml`, the launch settings, the role, the session and every other file stay as they are. An idle agent restarts and resumes its session.

- An agent whose launch command does not run `commands/grok_launch.sh` is skipped as unsupported until `mngr create NAME --reuse --update` re-creates it.

- The upgrade is `ready` once grok's SessionStart hook runs in the restarted agent; otherwise it fails at `ready`, naming the screen grok shows instead (login, consent notice, folder trust, paywall) when there is one.
