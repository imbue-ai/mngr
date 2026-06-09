Added an `aws` create-template to the repo's `.mngr/settings.toml` for dogfooding this
codebase on an AWS EC2 host, mirroring the existing `modal` and `docker` dev templates.

`mngr create -t aws <name>` builds the dev Dockerfile and runs an agent on EC2. Because
the AWS/`mngr_vps_docker` backend runs `docker build` on the remote VPS (rewriting
`--file=` relative to the uploaded context), the template uses the real-source-tree
build shape (context `.`, cloned + overlaid with uncommitted changes) rather than the
`.mngr/dev/build/` keyframe tarball shape that modal/docker use. The clone is full
history (no `--git-depth`): after the build, mngr seeds the work dir by pushing the
local repo's refs into the container's `/code/mngr/.git` as a thin pack, which needs the
container repo to already contain the base objects -- a shallow clone fails with
"pack has N unresolved deltas / index-pack abnormal exit".

The shared `[providers.aws]` config (region `us-west-2`, plan `t3.large`,
`auto_shutdown_minutes = 120`, `builder = "DEPOT"`) is committed in `.mngr/settings.toml`;
only the operator-specific `allowed_ssh_cidrs` lives in the gitignored
`.mngr/settings.local.toml`. The two blocks merge per-field (ProviderInstanceConfig.merge_with
honors `model_fields_set`). `builder = "DEPOT"` offloads the Rust/`uv sync` build to depot's
cached remote builders, required to stay under the backend's 600s local-build timeout;
`DEPOT_TOKEN` and `GH_TOKEN` must be exported when running `mngr create -t aws`. The
template uses `pass_env__extend` (not plain `pass_env`) so it adds `GH_TOKEN` without
clobbering any inherited `pass_env` (e.g. a user profile's `ANTHROPIC_API_KEY`).
