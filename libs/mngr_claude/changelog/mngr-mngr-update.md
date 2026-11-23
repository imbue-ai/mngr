`mngr upgrade` covers interactive claude agents, whether they share the user's Claude config dir or have their own. It rewrites the agent's `commands/` scripts and mngr's own hooks in the settings file it loads, keeping every other key, every hook mngr did not write and the agent's own options: the auto-allow and credential-sync hooks and the optional scripts are rewritten only where it has them.

- An agent created before mngr kept its hooks in a file of its own is skipped until `mngr create NAME --reuse --update` re-creates it; first remove the hooks mngr left in `.claude/settings.local.json` in its work dir.

- A launch command stored before 2026-07-28 now also clears the `claude_main_pid` claim when the agent starts.
