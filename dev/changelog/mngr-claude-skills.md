# Point mngr at the imbue-mngr-skills Claude Code plugin

The `imbue-mngr-skills` Claude Code plugin (the `message-agent`,
`wait-for-agent`, `find-agent`, and `mngr-help` skills) is published from its
own GitHub repo, `imbue-ai/mngr-claude-skills`, as a Claude Code plugin
marketplace -- mirroring how `imbue-code-guardian` is distributed from its own
repo.

This repo dogfoods the published plugin: `.claude/settings.json` registers the
`imbue-mngr` marketplace from `imbue-ai/mngr-claude-skills` and enables
`imbue-mngr-skills@imbue-mngr`, and `scripts/claude_update_plugin.sh` refreshes
it on SessionStart alongside `imbue-code-guardian`.

These skills previously lived in this repo's project-level `.claude/skills/`
directory; they have moved out to the dedicated repo so any mngr user can
install them for any project (via `mngr extras claude-plugin`, or
`claude plugin marketplace add imbue-ai/mngr-claude-skills` +
`claude plugin install imbue-mngr-skills@imbue-mngr`).
