## Azure provider wiring

- Added `--cov=imbue.mngr_azure` to the root pytest coverage config so the new `mngr_azure` package is covered alongside the other provider plugins. The package is picked up automatically by the `libs/*` uv workspace glob.

- Registered the `azure` command group in `scripts/make_cli_docs.py` (`SECONDARY_COMMANDS`) so `mngr azure` gets a generated doc page, alongside `aws` / `gcp`.

- The `azure` create template now builds the project Dockerfile on the VM (so azure agents get `gh` and the full mngr toolchain) instead of coming up on a bare `debian:bookworm-slim` base. It mirrors the `gcp` template: `build_arg = ["--azure-vm-size=...", "--file=libs/mngr/imbue/mngr/resources/Dockerfile", "."]` -- the context is the worktree, which the shared `mngr_vps_docker` build flow clones (overlaying uncommitted changes) and uploads, resolving `--file` inside it. Also forwards `GH_TOKEN` + runs `gh auth setup-git` (via the `github_setup` window), sets `agent_args=--dangerously-skip-permissions` and `target_path=/code/mngr`.

- `[providers.azure] builder = "DEPOT"` builds on depot's cached remote builders (like `gcp`) so azure creates after the first reuse cached layers instead of building cold. Requires `DEPOT_TOKEN` exported at `mngr create -t azure` time (read from the create shell, not `pass_env`); `depot.json` in the repo supplies the project id. Drop the block to fall back to a native `docker build` on the VM.
