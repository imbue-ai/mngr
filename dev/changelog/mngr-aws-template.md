Added an `aws` create-template to the repo's `.mngr/settings.toml` for dogfooding this
codebase on an AWS EC2 host, mirroring the existing `modal` and `docker` dev templates.

`mngr create -t aws <name>` builds the dev Dockerfile and runs an agent on EC2. Because
the AWS/`mngr_vps_docker` backend runs `docker build` on the remote VPS (rewriting
`--file=` relative to the uploaded context), the template uses the real-source-tree
build shape (context `.`, cloned + overlaid with uncommitted changes) rather than the
`.mngr/dev/build/` keyframe tarball shape that modal/docker use. `--git-depth=1` keeps
the uploaded clone small.

The per-developer `[providers.aws]` block (region, instance plan, `allowed_ssh_cidrs`,
`auto_shutdown_minutes`, `builder = "DEPOT"`) lives in the gitignored
`.mngr/settings.local.toml`, since `allowed_ssh_cidrs` is operator-specific. Using
`builder = "DEPOT"` offloads the Rust/`uv sync` build to depot's cached remote builders,
which is required to stay under the backend's 600s local-build timeout; `DEPOT_TOKEN`
and `GH_TOKEN` must be exported when running `mngr create -t aws`.
