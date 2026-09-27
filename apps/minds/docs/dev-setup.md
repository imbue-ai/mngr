# Setting up minds for development

This is the one-time setup for hacking on the minds desktop client and its
stack from source. Run from source, the app targets production by default:
it reads the in-repo production `client.toml`, owns `~/.minds/`, and needs
nothing exported.

## Linux: one script

```bash
curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/apps/minds/scripts/install-linux.sh | bash
```

That clones the repo to `~/mngr` (on `main`; `--version latest` picks the
newest `minds-v*` tag, `--version <ref>` any ref, `--install-dir` another
location) and installs everything below, then launches the app. From an
existing checkout, run it in place and it uses that checkout instead:

```bash
apps/minds/scripts/install-linux.sh
```

It is idempotent (re-run it after pulling to re-sync the venv and
`node_modules`, which it does on every run without asking). The toolchain
under `$HOME` (uv, nvm, the pinned Node and pnpm) installs after one
confirmation, and a fresh clone asks before it lands; anything that needs
sudo (apt packages, Docker CE, docker group membership) shows its exact
command and asks first. `--yes` runs every step unprompted;
`--non-interactive` never prompts and instead prints the command for any
privileged step still needed. `--skip-docker` and `--no-launch` do what
they say. It writes `~/.local/bin/minds-desktop`, which starts the app from
the checkout.

The same script covers WSL2 ([wsl.md](./wsl.md), detected automatically)
and, with `--raspberry-pi`, a Raspberry Pi 5 always-on host
([raspberry-pi.md](./raspberry-pi.md)).

## macOS: prerequisites (install once)

- [ ] **uv, git** -- the monorepo's core tooling. Every command is run via
      `uv run ...` from the repo root.

- [ ] **Docker Desktop** (or colima / lima) -- local minds agents run in
      Docker (or Lima) containers; start it before creating an agent.

- [ ] **Node 24.15.0 (via nvm) + pnpm 10.33.4** -- the Electron desktop
      client. Both versions are pinned (`apps/minds/.nvmrc`,
      `apps/minds/package.json` `engines`, `engine-strict=true`), so
      `pnpm install` refuses any other version. `nvm install` reads the
      `.nvmrc`; then `npm install --global pnpm@10.33.4` into that Node.

Then, from any directory:

```bash
apps/minds/scripts/start-desktop.sh
```

It selects the pinned Node, checks for the pinned pnpm (erroring with the
install command if either is missing), installs the Electron dependencies,
and runs `pnpm start`, which provisions the bundled binaries (git, restic,
lima, desync, the latchkey curl) into a per-user cache, builds the UI, and
launches Electron. The Linux launcher and the internal `just minds-start`
recipes end in this same script. You create your first agent from the login
URL the app prints on startup.

## Imbue-internal: developing against a dev env

Everything above is what a contributor working from the public repo needs.
Imbue developers who iterate on mngr, the workspace template, and the minds
services together use the private operator tooling on top of it, all of
which lives outside the public mirror:

- [ ] **GNU rsync** (macOS) -- `apps/minds/scripts/propagate_changes` syncs the
      default-workspace-template worktree into a running container with
      `rsync --filter=':- .gitignore'`, a GNU rsync feature. Recent macOS
      ships Apple's `openrsync` as `/usr/bin/rsync`, which doesn't support
      it, so the sync fails. Install GNU rsync ahead of `/usr/bin` on `PATH`:

      ```bash
      brew install rsync
      rsync --version | head -1   # must NOT say "openrsync"
      ```

- [ ] **`just`** -- the private `just` recipes (`minds-install`,
      `minds-start`, `default-workspace-template-worktree`) drive the loop.

- [ ] **GitHub access to `imbue-ai/default-workspace-template`** (private) --
      `just default-workspace-template-worktree` clones it. Authenticate with
      `gh auth login` or a git credential helper (agents use `GH_TOKEN`).

- [ ] **Vault CLI + login** -- `minds-admin env deploy` reads dev-tier provisioning
      credentials (Neon, SuperTokens, ...) from HCP Vault at command time. Run
      `vault login -method=oidc` once per session; the deploy CLI applies the
      imbue HCP `VAULT_ADDR` / `VAULT_NAMESPACE` defaults itself, so login is
      all you need. Install + layout: [vault-setup.md](./deploy/setup/vault.md).

- [ ] **Gen-2 box access (only if you bake dev pool slices or run box
      commands).** The dev fleet's boxes accept management SSH only from the
      WireGuard overlay and from the connector, with a certificate the dev
      Vault SSH CA signs through your `employee` login. Once per machine:
      generate your operator key (`mkdir -p -m 700 ~/.mindsadmin/dev &&
      wg genkey | tee ~/.mindsadmin/dev/wireguard.key | wg pubkey`), open a
      PR adding the public half as a `[[management_plane.wireguard.operators]]`
      block (a free `10.112.0.x` address) in
      `apps/minds/imbue/minds/config/envs/dev/deploy.toml`, and after it
      merges ask an existing operator to run
      `uv run minds-admin wireguard sync-peers --tier dev` (one run puts your
      key on every dev box). Then `uv run minds-admin wireguard install-onetun`
      installs the userspace tunnel the tooling dials through. Runbook:
      [management-plane.md](./deploy/reference/management-plane.md).

- [ ] **Membership in the `minds-dev` Modal workspace + a matching
      `~/.modal.toml` profile.** `minds-dev` is a *separate*, workspace-bound
      Modal workspace (there's no shared dev token in Vault), so ask in
      #project-minds-internal-product for an invite, then
      `modal token new --profile minds-dev` and select that workspace in the
      browser. Verify with `modal profile list`: the `minds-dev` profile must
      show workspace `minds-dev` -- a profile *named* `minds-dev` that holds a
      token for another workspace passes `minds-admin env activate --deploy` but is
      caught (with a clear error) by `minds-admin env deploy`'s preflight. Full
      detail: [environments.md](./deploy/reference/environments.md).

With those in place, follow the **minds-dev-workflow** skill
(`.agents/skills/minds-dev-workflow/SKILL.md`; ask your agent to run it, or
read it directly) for the actual commands. It covers the whole loop:

- **First time** -- stand up a default-workspace-template worktree, then
  `vault login` and bootstrap + deploy your dev env
  (`minds-admin env activate --create --deploy dev-<your-user>` -> `minds-admin env deploy`).
- **Every startup** (fresh shell) -- activate the env, then `just minds-start`,
  which launches Electron. You create your first agent from the login URL it
  prints. Activation is what points the app at a dev or staging env instead
  of production: it exports `MINDS_ROOT_NAME` / `MNGR_HOST_DIR` /
  `MNGR_PREFIX` / `MINDS_CLIENT_CONFIG_PATH` for that env.
- **Iterate** against a running agent with `apps/minds/scripts/propagate_changes`.

The skill has the exact commands and how to find a running container's SSH port/key.
