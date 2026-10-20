#!/usr/bin/env bash
# Launch the minds desktop app from this source checkout: dev mode, `electron .`
# against the monorepo venv, targeting production unless the shell names
# another env. Run it from any directory; it never changes the caller's cwd.
#
#     apps/minds/scripts/start-desktop.sh
#
# This is the one place that knows how to launch the dev app -- the Linux
# installer's launcher and the internal `just minds-start` recipes all end
# here. It selects the pinned Node (apps/minds/.nvmrc) via nvm, requires the
# pinned pnpm (engines.pnpm in apps/minds/package.json), installs the Electron
# dependencies from the lockfile, and execs `pnpm start`, whose prestart hook
# provisions the bundled binaries and builds the UI. A missing Node or pnpm
# is an error with the install hint, never an install: that is the job of
# install-linux.sh on Linux and docs/dev-setup.md on macOS.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$(cd "$script_dir/.." && pwd)"

# shellcheck disable=SC1091
. "$script_dir/select_node_version.sh" || exit 2

cd "$app_dir"
required_pnpm="$(node -p "require('./package.json').engines.pnpm")"
current_pnpm="$(pnpm --version 2>/dev/null || true)"
if [ "$current_pnpm" != "$required_pnpm" ]; then
    echo "error: active pnpm is ${current_pnpm:-none}, but apps/minds requires ${required_pnpm} (apps/minds/package.json engines.pnpm)." >&2
    echo "       Install it into the selected Node, then re-run:" >&2
    echo "         npm install --global pnpm@${required_pnpm}" >&2
    exit 2
fi

pnpm install --frozen-lockfile
exec pnpm start
