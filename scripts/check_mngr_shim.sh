#!/usr/bin/env bash
# Pre-commit hook: ensure the `mngr` dev shim (scripts/mngr) is on your PATH, so
# `mngr` -- and anything that shells out to it (e.g. minds) -- always runs the
# checkout you're working in, never a stale global install.
#
# It INSTALLS the shim if missing (a symlink in ~/.local/bin), so there is no
# per-worktree setup: the shim routes by cwd, and this hook keeps the single
# symlink healthy. The one thing it can't do for you is edit PATH, so it fails
# (with instructions) if ~/.local/bin isn't ahead of any other `mngr`.
set -euo pipefail

# Point the symlink at the main clone (stable across worktrees). Fall back to the
# checkout being committed if the main clone doesn't have the shim yet (e.g. the
# branch that adds it, before it has merged into the main clone's branch).
primary=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || git rev-parse --show-toplevel)")
target="$primary/scripts/mngr"
[ -f "$target" ] || target="$(git rev-parse --show-toplevel)/scripts/mngr"

bindir="$HOME/.local/bin"
link="$bindir/mngr"
mkdir -p "$bindir"
if [ "$(readlink "$link" 2>/dev/null || true)" != "$target" ]; then
    ln -sfn "$target" "$link"
    echo "installed mngr shim: $link -> $target"
fi

# Verify the shim actually wins on PATH (catches ~/.local/bin missing from PATH,
# or another `mngr` shadowing it).
#
# This hook often runs as a child of `uv run mngr ...` (e.g. `mngr create` makes
# its initial commit under uv). `uv run` force-prepends the project's .venv/bin
# to PATH, so the project-local `mngr` console script shadows the shim *here*
# even though the shim wins in a normal shell. That .venv/bin/mngr is this very
# checkout, not a stale global, so it is not what this hook guards against. Drop
# the active venv's bin dir before resolving, so we evaluate resolution the way
# an interactive shell would -- still catching a genuinely stale global ahead of
# ~/.local/bin.
search_path=$PATH
if [ -n "${VIRTUAL_ENV:-}" ]; then
    search_path=$(printf '%s' "$PATH" | tr ':' '\n' | grep -vxF "$VIRTUAL_ENV/bin" | paste -sd: - || true)
fi
resolved=$(PATH="$search_path" command -v mngr 2>/dev/null || true)
if [ -n "$resolved" ] && grep -q "MNGR_DEV_SHIM_V1" "$resolved" 2>/dev/null; then
    exit 0
fi

cat >&2 <<EOF
error: mngr dev shim installed at $link but \`mngr\` does not resolve to it.
  Resolved: ${resolved:-<mngr not on PATH>}
  Fix: put $bindir on your PATH ahead of any venv bin, then run \`hash -r\`.
EOF
exit 1
