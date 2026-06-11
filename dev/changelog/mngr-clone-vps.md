Clarified the `aws` dogfood create-template comment in `.mngr/settings.toml`: `mngr create -t aws`
now works whether you run it from the primary checkout or a linked git worktree. The
`mngr_vps_docker` build flow clones the build context in both cases (see the `mngr_vps_docker`
changelog), so the comment no longer implies you must run from a worktree.
