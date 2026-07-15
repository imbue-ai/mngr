Documented the first-time setup for running minds from source in the minds README: a step-by-step checklist (pinned Node/pnpm toolchain via `just minds-install`, the default-workspace-template worktree, personal dev env bootstrap, Docker) with links to the authoritative toolchain and environment docs. Previously the only end-to-end sequence lived in an agent-facing skill file and the README's Getting started skipped every prerequisite.

Also added a note to docs/desktop-app.md's "Running locally" section pointing at `just minds-install` / `just minds-start` as the preferred dev-loop entry points over bare `pnpm install` / `pnpm start`.

No installer automation was added: the toolchain and environment steps are inherently interactive (version-manager setup, browser auth, per-shell env activation), so the README documents them as manual steps instead.
