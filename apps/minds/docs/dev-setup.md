# Setting up Imbue Studio for development

This is the one-time setup for hacking on the Imbue Studio desktop client and its
stack from source. Run from source, the app targets production by default:
it reads the in-repo production `client.toml`, owns the production tier's data
roots, and needs nothing exported.

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
they say. It writes `~/.local/bin/studio-desktop`, which starts the app from
the checkout.

The same script covers WSL2 ([wsl.md](./wsl.md), detected automatically)
and, with `--raspberry-pi`, a Raspberry Pi 5 always-on host
([raspberry-pi.md](./raspberry-pi.md)).

## Testing Linux behaviour from a Mac

[linux-on-mac.md](./linux-on-mac.md) brings the real Linux app up in a Lima VM
over VNC, which is what you need for anything the path resolver, the data
migration or the Electron shell does differently there.

## macOS: prerequisites (install once)

- [ ] **uv 0.13.4 or later, git** -- the monorepo's core tooling. Every
      command is run via `uv run ...` from the repo root, and the root
      `pyproject.toml`'s `required-version` refuses an older uv.

- [ ] **Docker Desktop** (or colima / lima) -- local Imbue Studio agents run in
      Docker (or Lima) containers; start it before creating an agent.

- [ ] **Node 24.22.1 (via nvm) + pnpm 10.33.4** -- the Electron desktop
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
lima, desync, the latchkey curl, unison and its file watcher) into a per-user
cache, builds the UI, and
launches Electron. The Linux launcher and the internal `just minds-start`
recipes end in this same script. You create your first agent from the login
URL the app prints on startup.

## Profiling a session

Imbue Studio has an opt-in profiling mode that records a session as one trace
file the [Perfetto UI](https://ui.perfetto.dev) opens as is: the page's spans,
the backend's request and sharing spans, each warm `mngr` process's import,
wait and command, and, under Electron, Chromium's own trace of the shell and
the renderer, all on one timeline. It is off unless the backend is started
with a path to write to, and the path must not be under the logs directory a
bug report uploads (the backend refuses one). To measure a share-panel
operation from source:

1. Start the app with the trace file named: `MINDS_PROFILE_FILE=~/profiles/share-1.json just minds-start`
   (the shell's `--profile` launch argument does the same; see
   [desktop-app.md](./desktop-app.md#environment-variables)), or run the
   backend alone with `uv run minds run --profile ~/profiles/share-1.json`
   and open it in a browser, which records everything but Chromium's trace.
2. Open the share panel, publish, add a grant, close the panel. The page
   posts its spans as they happen, and each warm process hands its spans
   back as it finishes.
3. Quit the app. Under Electron the shell flushes Chromium's trace into the
   file and then stops the backend, which closes the file. Open
   `~/profiles/share-1.json` in the Perfetto UI, or serve it to the UI from
   localhost with Perfetto's own `open_trace_in_ui` script (fetched from
   `get.perfetto.dev`).
4. In the UI, the backend process shows one slice per request with the
   sharing spans nested under it; each `mngr_caller.call` slice has an arrow
   to the child process's lane, where the import, the wait, and the command
   are three slices; the `Imbue Studio UI` process shows the panel's
   operations with arrows to the requests that served them; Chromium's
   browser and renderer processes show what the page and the shell were
   doing meanwhile. A connector call's server-side duration is one
   OpenObserve query on the `trace_id` its slice carries.

For a function-level view of the backend, start it under viztracer instead
(`uv run viztracer --min_duration 500 --ignore_frozen -o ~/profiles/share-1.viztracer.json -m imbue.minds.main run --profile ~/profiles/share-1.json`),
then combine the two files (`viztracer --combine ours.json theirs.json -o combined.json`,
or `jq -s add`); both are on the system monotonic clock, so they line up
without rebasing. viztracer's filters are tuned per investigation:
`--min_duration` keeps the file small and `--include_files` narrows it to our
packages.

## Imbue-internal: developing against a dev env

Everything above is what a contributor working from the public repo needs.
Imbue developers who iterate on mngr, the workspace template, and the Imbue Studio
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
      #project-studio-engineering for an invite, then
      `modal token new --profile minds-dev` and select that workspace in the
      browser. Verify with `modal profile list`: the `minds-dev` profile must
      show workspace `minds-dev` -- a profile *named* `minds-dev` that holds a
      token for another workspace passes `minds-admin env activate --deploy` but is
      caught (with a clear error) by `minds-admin env deploy`'s preflight. Full
      detail: [environments.md](./deploy/reference/environments.md).

- [ ] **Linear credentials in latchkey.** Agents reach Linear only through
      `latchkey curl`, so without this the flake skills silently skip filing
      (`/report-incidental-flakes`, `/detect-flakes`) and `/work-on-linear` and
      `/deflake-mngr-ci` cannot run at all. Install the CLI --
      `npm install -g latchkey`, which must land at or above the
      `LATCHKEY_MIN_VERSION` in
      `libs/mngr_latchkey/imbue/mngr_latchkey/core.py`; the desktop app's
      bundled copy is named by absolute path and is not on your PATH -- then
      `latchkey auth browser linear`, or, with no GUI,
      `latchkey auth set linear -H "Authorization: <key>"` with a personal API
      key from Linear's Settings -> Security & access. Verify with
      `uv run python scripts/flake_reconcile.py list-tickets`, which is
      read-only and is the exact call the skills make. `latchkey services info
      linear` is not a substitute: 3.x moved `credentialStatus` from the top
      level into a per-account `credentials` map, so anything reading the old
      top-level field sees nothing whether or not the credential works.

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
