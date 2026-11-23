New `mngr upgrade` rewrites the files mngr generated for existing agents on the local host with this mngr's versions, so agents created by an earlier mngr pick up newer behaviour without being recreated. `mngr list --local --ids | mngr upgrade -` upgrades every local agent; `--dry-run` previews.

- Rewriting an agent's files needs rsync. An idle agent is restarted after any message being sent to it finishes. A stopped agent gets the new files and stays stopped. An agent that is busy, or whose program has exited, is skipped unless `--force`. A skipped agent's line says why.

- After an agent fails, the run goes on to the next one; `--on-error abort` stops it there and reports the agents it did not reach as skipped.

- Exit code 9 (`EXIT_CODE_AGENTS_NOT_UPGRADED`) means an agent was skipped, 1 that one failed. `--format json` and `--format jsonl` report every outcome. A skipped agent's entry carries `detail`, the same sentence as its line, and `kind`: `NOT_IDLE` (try again later, or `--force`), `UNSUPPORTED` (needs a person) or `ABORTED` (run the command again). An agent destroyed during the run is also skipped as `NOT_IDLE`, and needs neither.

- Agent types opt in through `UpgradableAgentMixin`; an agent of a type that has not is skipped as `UNSUPPORTED`. An opted-in type can also decline one of its agents, which is skipped with the reason and `mngr create NAME --reuse --update` to fix it.

- Help printed outside a terminal (piped or redirected) keeps each `- ` list item of a command's description on its own line.
