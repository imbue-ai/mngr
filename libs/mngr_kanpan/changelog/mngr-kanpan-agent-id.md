Kanpan now identifies an agent by its globally unique id, not its name, whenever it acts on one, selects one, caches its data, or carries data across a refresh -- so two agents that share a name on different hosts are always kept apart. Names are still what the board displays.

Agent names are unique only within a single host, so anything kanpan did by name could hit the wrong agent (or several), or merge two agents' data, once two hosts held same-named agents. In particular, marking one such agent for deletion ran a single `mngr destroy <name> --force` that destroyed *both* of them, with no confirmation. Kanpan now:

- keys dired-style marks by agent id, so two same-named agents mark and act independently (previously a second one could not even be marked);

- resolves delete, mute, attach, peek, and peek-reply by id -- delete runs `mngr destroy <id>`, so it removes exactly the agents you marked; a reply goes only to the peeked agent instead of every agent with that name;

- restores focus after a refresh by id, so the cursor stays on the exact agent rather than jumping to the first one sharing its name;

- keys the per-agent field cache and every data source's output by id, and matches old/new snapshots by id when carrying PR/CI columns forward, so same-named agents no longer overwrite or inherit each other's column data;

- exports both `MNGR_AGENT_ID` (globally unique) and `MNGR_AGENT_NAME` to custom commands, so a command can target an agent unambiguously. The README examples now use `$MNGR_AGENT_ID`.

The board's JSON output (`--format json`/`jsonl`) now carries an `agent_id` field per entry. The incremental search (`/`) still matches by name; navigating between two same-named matches is a follow-up.
