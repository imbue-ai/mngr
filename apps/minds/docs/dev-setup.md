# Setting up minds for development

This is the one-time setup for hacking on the minds desktop client and its
stack from source. Install the prerequisites below, then follow the
**minds-dev-workflow** skill (`.claude/skills/minds-dev-workflow/SKILL.md`) --
ask your agent to run it, or read it directly -- for the build/run loop.

## Prerequisites (install once)

- [ ] **uv, just, git** -- the monorepo's core tooling. Every command is run
      via `uv run ...` from the repo root.

- [ ] **Docker Desktop** (or colima / lima) -- local minds agents run in
      Docker (or Lima) containers; start it before creating an agent.

- [ ] **Node 24.15.0 (via nvm) + pnpm 10.33.4** -- the Electron desktop
      client. Both versions are pinned (`apps/minds/.nvmrc`,
      `apps/minds/package.json` `engines`, `engine-strict=true`).
      `just minds-install` selects the pinned Node via nvm and errors --
      never auto-installs -- if it's missing (`nvm install 24.15.0`). Get
      pnpm via `corepack enable` (ships with Node) or a standalone install.

- [ ] **GNU rsync** (macOS) -- `just minds-start` syncs your working tree
      into the default-workspace-template worktree with
      `rsync --filter=':- .gitignore'`, a GNU rsync feature. Recent macOS
      ships Apple's `openrsync` as `/usr/bin/rsync`, which doesn't support
      it, so the sync fails. Install GNU rsync ahead of `/usr/bin` on `PATH`:

      ```bash
      brew install rsync
      rsync --version | head -1   # must NOT say "openrsync"
      ```

- [ ] **GitHub access to `imbue-ai/default-workspace-template`** (private) --
      `just default-workspace-template-worktree` clones it. Authenticate with
      `gh auth login` or a git credential helper (agents use `GH_TOKEN`).

- [ ] **Vault CLI + login** -- `minds env deploy` reads dev-tier provisioning
      credentials (Neon, SuperTokens, ...) from HCP Vault at command time. Run
      `vault login -method=oidc` once per session; the deploy CLI applies the
      imbue HCP `VAULT_ADDR` / `VAULT_NAMESPACE` defaults itself, so login is
      all you need. Install + layout: [vault-setup.md](./vault-setup.md).

- [ ] **Membership in the `minds-dev` Modal workspace + a matching
      `~/.modal.toml` profile.** `minds-dev` is a *separate*, workspace-bound
      Modal workspace (there's no shared dev token in Vault), so ask a
      `minds-dev` workspace admin for an invite, then
      `modal token new --profile minds-dev` and select that workspace in the
      browser. Verify with `modal profile list`: the `minds-dev` profile must
      show workspace `minds-dev` -- a profile *named* `minds-dev` that holds a
      token for another workspace passes `minds env activate --deploy` but is
      caught (with a clear error) by `minds env deploy`'s preflight. Full
      detail: [environments.md](./environments.md).

## Then: build and run

With the prerequisites in place, follow the **minds-dev-workflow** skill for
the actual commands (ask your agent to run it, or read
`.claude/skills/minds-dev-workflow/SKILL.md`). It covers the whole loop:

- **First time** -- stand up a default-workspace-template worktree, then
  `vault login` and bootstrap + deploy your dev env
  (`minds env activate --create --deploy dev-<your-user>` -> `minds env deploy`).
- **Every startup** (fresh shell) -- activate the env, then `just minds-start`,
  which re-syncs your live mngr into the worktree's `vendor/mngr/` and launches
  Electron. You create your first agent from the login URL it prints.
- **Iterate** against a running agent with `apps/minds/scripts/propagate_changes`.

The skill has the exact commands, the every-Create `vendor/mngr/` sync, and how
to find a running container's SSH port/key.
