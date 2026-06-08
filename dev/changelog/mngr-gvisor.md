Added a blueprint plan (`blueprint/gvisor-docker-hardening/`) for hardening docker invocations with the gVisor (runsc) runtime.

Added a dev `mngr` shim (`scripts/mngr`) so `mngr` always runs the checkout you're working in (per-worktree, by cwd) instead of a stale global install. A pre-commit hook (`scripts/check_mngr_shim.sh`) installs the shim automatically (a symlink in `~/.local/bin`) and verifies it's on PATH -- no per-worktree setup. Updated the README dev-install notes accordingly (use the shim, not `uv tool install -e libs/mngr`).
