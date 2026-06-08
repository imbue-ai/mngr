`just minds-start` now exports `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` alongside
the `MINDS_WORKSPACE_*` vars. This is the explicit opt-in that makes the minds
desktop create-form honor the local-worktree defaults on any tier (including
staging / production), instead of only on per-developer dev envs.
