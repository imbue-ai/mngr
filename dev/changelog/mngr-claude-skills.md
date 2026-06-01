# Add the imbue-mngr-skills Claude Code plugin

The repo now hosts a Claude Code plugin marketplace at its root
(`.claude-plugin/marketplace.json`), publishing the `imbue-mngr-skills`
plugin under `plugins/imbue-mngr-skills/`. The plugin bundles three skills
for coordinating mngr agents: `message-agent`, `wait-for-agent`, and
`find-agent`.

These skills previously lived only in this repo's project-level
`.claude/skills/` directory; they have been moved into the plugin so any
mngr user can install them for any project (via
`claude plugin marketplace add imbue-ai/mngr` +
`claude plugin install imbue-mngr-skills@imbue-mngr`, or `mngr extras
claude-plugin`). The repo dogfoods the published plugin by enabling it in
`.claude/settings.json`, mirroring how `imbue-code-guardian` is consumed.

`message-agent` and `wait-for-agent` now use the verbatim agent name when it
matches a live agent exactly, only falling back to `find-agent` resolution
when the exact name does not match.

`scripts/claude_update_plugin.sh` now refreshes `imbue-mngr-skills@imbue-mngr`
on SessionStart alongside `imbue-code-guardian`, so the dogfooded skills stay
current with the marketplace.
