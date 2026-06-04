#!/usr/bin/env bash
# Pre-commit guard: the `mngr` on your PATH must be the dev shim (scripts/mngr),
# so `mngr` -- and anything that shells out to it (e.g. minds) -- runs the
# checkout you are working in rather than a stale global install. No opt-out by
# design: a misresolved `mngr` is always a bug. Run `just install-mngr-shim`.
set -euo pipefail

resolved=$(command -v mngr 2>/dev/null || true)
# grep follows the ~/.local/bin/mngr symlink through to scripts/mngr.
if [ -n "$resolved" ] && grep -q "MNGR_DEV_SHIM_V1" "$resolved" 2>/dev/null; then
    exit 0
fi

cat >&2 <<EOF
error: \`mngr\` on your PATH is not the dev shim (scripts/mngr).
  Found: ${resolved:-<mngr not found on PATH>}
  Fix:   just install-mngr-shim   (then \`hash -r\` or open a new shell)
  Why:   otherwise \`mngr\` runs one global install instead of the checkout you
         are in -- silently running the wrong code (how the gVisor change
         appeared not to work).
EOF
exit 1
