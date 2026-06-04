Added a blueprint plan (`blueprint/gvisor-docker-hardening/`) for hardening docker invocations with the gVisor (runsc) runtime.

Added a dev `mngr` shim (`scripts/mngr`) plus `just install-mngr-shim` so `mngr` always runs the checkout you're working in (per-worktree) instead of a stale global install. A new pre-commit hook (`scripts/check_mngr_shim.sh`) enforces that the `mngr` on your PATH is this shim. Updated the README dev-install step to use `just install-mngr-shim` instead of `uv tool install -e libs/mngr`.
