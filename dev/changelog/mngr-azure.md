## Azure provider wiring

- Added `--cov=imbue.mngr_azure` to the root pytest coverage config so the new `mngr_azure` package is covered alongside the other provider plugins. The package is picked up automatically by the `libs/*` uv workspace glob.

- Registered the `azure` command group in `scripts/make_cli_docs.py` (`SECONDARY_COMMANDS`) so `mngr azure` gets a generated doc page, alongside `aws` / `gcp`.

- The `azure` create template now builds the project Dockerfile on the VM (so azure agents get `gh` and the full mngr toolchain, matching the `docker`/`modal` templates) instead of coming up on a bare `debian:bookworm-slim` base. A new `scripts/stage_azure_build_context.sh` (run from `pre_command_scripts.create`) assembles a self-contained build context at `.mngr/dev/azure-build/` -- the Dockerfile at the context root next to the hardlinked keyframe `current.tar.gz`, plus a `.dockerignore` that keeps the Dockerfile out of the image's `COPY .` -- which the remote VPS build requires (it uploads one context dir and resolves `--file` inside it). The `docker`/`modal` contexts (`.mngr/dev/build/`) are untouched.

- The `azure` template also forwards `GH_TOKEN` and runs `gh auth setup-git` (via the `github_setup` window) so `gh` is authenticated, and sets `target_path=/code/mngr` to match the Dockerfile's WORKDIR.

- `[providers.azure] builder = "DEPOT"` enables depot's remote build cache so azure creates after the first reuse cached layers instead of building cold. The create-time build reads `DEPOT_TOKEN` from the environment you run `mngr create` in (not from `pass_env`), so export `DEPOT_TOKEN` before creating an azure agent; drop the block to fall back to a native `docker build`.
