Add three dev skills under `.claude/skills/`:

- `post-pr-to-slack`: announce this repo's PRs in `#project-minds-internal-product` with a one-line message, and mark the announcement `:merged:` when the PR merges.

- `crispy-comments`: prune code comments on the current branch down to what helps future maintainers (copied from its canonical repo, which is noted in the skill).

- `address-pr-comments`: apply `CLAUDE:`/`SCULPTOR:`-prefixed PR comments, and critically evaluate feedback from automated reviewers (Vet, Copilot, or any bot) against the repo's conventions and the PR's goals rather than following it blindly.
