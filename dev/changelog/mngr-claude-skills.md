# Add the imbue-mngr-skills Claude Code plugin

The repo now hosts a Claude Code plugin marketplace at its root
(`.claude-plugin/marketplace.json`), publishing the `imbue-mngr-skills`
plugin under `plugins/imbue-mngr-skills/`. The plugin bundles skills that
teach Claude how to use mngr: `message-agent`, `wait-for-agent`, and
`find-agent` (coordinating with other agents), plus `mngr-help`, which has
the agent run `mngr help` for context whenever mngr comes up (and points at
`mngr ask`).

The skills prefer the installed `mngr` tool so they work in any project (not
just the mngr monorepo), while also allowing `uv run mngr` for use inside the
monorepo.

These skills previously lived only in this repo's project-level
`.claude/skills/` directory; they have been moved into the plugin so any
mngr user can install them for any project (via
`claude plugin marketplace add imbue-ai/mngr` +
`claude plugin install imbue-mngr-skills@imbue-mngr`, or `mngr extras
claude-plugin`). The repo dogfoods the published plugin by enabling it in
`.claude/settings.json`, mirroring how `imbue-code-guardian` is consumed.

`message-agent` and `wait-for-agent` use the name you give them when it already
looks like an agent name, and fall back to `find-agent` resolution only when it
doesn't -- for example when you paste a `mngr/<branch>` name or describe the
agent instead of naming it.

`scripts/claude_update_plugin.sh` now refreshes `imbue-mngr-skills@imbue-mngr`
on SessionStart alongside `imbue-code-guardian`, so the dogfooded skills stay
current with the marketplace.
