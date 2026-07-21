Add first-class dev skills to the monorepo's `.claude/skills/`:

- Bundle the `post-pr-to-slack` skill (canonical in `imbue-ai/sculptor`, generalized there to resolve the Slack channel per repo). PRs in this repo announce in `#project-minds-internal-product`; sculptor PRs keep `#project-sculptor-merges`.

- Vendor the `crispy-comments` skill from its canonical repo (`DanverImbue/crispy-comments`).

- Add `.claude/vendored_skills.toml` plus `scripts/sync_vendored_skills.py` (with a `just sync-vendored-skills` recipe) to refresh vendored skills from their upstream repos and record the pinned upstream commit per skill. Sync refuses to overwrite a local copy whose pinned commit is not found upstream (`--force` overrides); `--check` reports drift without changing files.
