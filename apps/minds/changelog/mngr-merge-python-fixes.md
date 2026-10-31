The workspace docs (README, overview, glossary) describe the bootstrap as `.venv/bin/bootstrap`, started after a `--frozen` sync, matching the paired template change that keeps any `uv` process from staying resident above supervisord.

The `agent_creator` comments no longer say the template forwards `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` into a new workspace: a workspace signs in to a provider account from its own UI, and the paired template change drops the last two templates (gcp and azure) that still forwarded them.

The in-workspace programs that run a workspace's own one-offs -- the backup and update programs' `mngr` calls, the update dispatcher's and the account probe's run of an older template's account resolver -- use `uv run --no-sync`, so they never relock or sync the workspace's environment.

The pool-slice verification script in `docs/deploy/ops/pool-hosts.md` lists a parked slice's agents with `uv run --no-sync mngr list` too, so the probe, which runs as root, never syncs the workspace's environment.

The published `imbue-studio` skill's frontmatter carries `metadata.author: imbue`, the marker the paired template's built-in skills carry; the template vendors this skill.
