Tightened the `aws` dogfood create-template comments in `.mngr/settings.toml`: condensed to a
focused note on how it differs from the `modal` template (VPS `docker build` resolves `--file=`
inside the uploaded context, so the context is `.`, which `mngr_vps_docker` now clones +
overlays for a self-contained baked `.git` -- see the `mngr_vps_docker` changelog) plus the
create-time requirements (`DEPOT_TOKEN`, `GH_TOKEN`). Dropped the stale guidance about setting
per-developer `allowed_ssh_cidrs` in `.mngr/settings.local.toml` (the provider already defaults
it to `0.0.0.0/0`).
