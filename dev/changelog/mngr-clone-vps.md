Small phrasing fixes to the `aws` create-template comments in `.mngr/settings.toml`: dropped
the redundant "analogue of the modal template" aside and the "(the worktree)" qualifier on the
build context (with the broadened clone -- see the `mngr_vps_docker` changelog -- `mngr create
-t aws` works from a primary checkout too, not only a linked worktree), and removed the stale
note that per-developer `allowed_ssh_cidrs` must live in `.mngr/settings.local.toml` (the
provider already defaults it to `0.0.0.0/0`).
