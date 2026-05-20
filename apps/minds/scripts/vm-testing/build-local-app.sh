#!/usr/bin/env bash
# Build minds.app locally from the current checkout, with workspace wheels
# bundled so the packaged runtime uses the in-tree code -- not the stale
# PyPI version.
#
# Usage: build-local-app.sh [<minds-source-dir>] [--out=<dir>]
#
# <minds-source-dir> defaults to the apps/minds directory of the repo
# this script lives in. Useful values: a worktree of a feature branch you
# want to verify, e.g. .external_worktrees/minds-bundle-lima/apps/minds .
#
# Output: <minds-source-dir>/dist/minds-darwin-arm64/minds.app (or
# whatever --out= overrides to).
#
# Prerequisites: node 20+, pnpm 10+, uv, an installed Python 3.12+.
#
# WHY THIS EXISTS
#
# Production builds run via ToDesktop's cloud builder, which generates
# wheels for every workspace package the desktop app depends on and writes
# a wheel-aware pyproject.toml into Contents/Resources/pyproject/. Locally,
# `pnpm build` only downloads binary resources (uv, git, lima, latchkey);
# the bundled pyproject still lists `minds>=0.1.0` and resolves it from
# PyPI at first launch. PyPI's `minds` is whatever was last released, not
# the in-tree code you're trying to verify -- so any local-only change to
# the Python backend silently does nothing. This script closes that gap.

set -euo pipefail

HERE="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$HERE/lib.sh"

require_cmd uv
require_cmd pnpm
require_cmd node
require_cmd npx
require_cmd python3

# Workspace packages that production bundles. Each entry is the package
# directory (relative to the repo root) and the project name as it appears
# in pyproject.toml `[project] name`. The bundled pyproject.toml's
# `[tool.uv.sources]` block points each project name at the freshly-built
# wheel for the corresponding directory.
WORKSPACE_PACKAGES=(
    "apps/minds:minds"
    "libs/mngr:imbue-mngr"
    "libs/mngr_claude:imbue-mngr-claude"
    "libs/mngr_forward:imbue-mngr-forward"
    "libs/mngr_imbue_cloud:imbue-mngr-imbue-cloud"
    "libs/mngr_latchkey:imbue-mngr-latchkey"
    "libs/mngr_lima:imbue-mngr-lima"
    "libs/mngr_modal:imbue-mngr-modal"
    "libs/mngr_ovh:imbue-mngr-ovh"
    "libs/mngr_vps_docker:imbue-mngr-vps-docker"
    "libs/imbue_common:imbue-common"
    "libs/concurrency_group:concurrency-group"
    "libs/resource_guards:resource-guards"
    "libs/modal_proxy:modal-proxy"
)

minds_dir="${1:-}"
out_override=""
for arg in "$@"; do
    case "$arg" in
        --out=*) out_override="${arg#--out=}" ;;
    esac
done

# If the first positional looks like a flag we already consumed, fall
# through to the default.
if [[ -z "$minds_dir" || "$minds_dir" == --* ]]; then
    minds_dir="$(cd "$HERE/../.." && pwd)"
fi

[[ -d "$minds_dir/electron" && -f "$minds_dir/package.json" ]] \
    || die "expected an apps/minds directory at $minds_dir"

repo_root="$(cd "$minds_dir/../.." && pwd)"
resources_dir="$minds_dir/resources"
wheels_dir="$resources_dir/wheels"
pyproject_dir="$resources_dir/pyproject"

# Run pnpm build first because it wipes resources/ entirely; building
# wheels and writing the wheel-aware pyproject before it would just be
# erased.
log "running pnpm build for the binary resources (wipes resources/)"
( cd "$minds_dir" && pnpm install --silent && pnpm build 2>&1 | sed 's/^/[pnpm-build] /' )

# Replace the bundled git with the real one from
# /Library/Developer/CommandLineTools/. build.js copies `which git`, which
# on a developer Mac is /usr/bin/git -- an xcselect stub that links against
# /usr/lib/libxcselect.dylib. macOS Tahoe SIGKILLs that stub when it's run
# from anywhere other than /usr/bin/ (AMFI / library validation), which
# breaks the desktop client's first `git clone` inside the agent-creation
# step. The CLT-installed git at the path below depends only on system
# libs and runs fine from any location.
real_git=/Library/Developer/CommandLineTools/usr/bin/git
if [[ -x "$real_git" ]]; then
    log "swapping bundled git with $real_git"
    cp "$real_git" "$resources_dir/git/bin/git"
    chmod +x "$resources_dir/git/bin/git"
    # git looks for its helpers (git-remote-https, etc.) and templates
    # relative to the binary location: <prefix>/libexec/git-core/ and
    # <prefix>/share/git-core/. Copy both so HTTPS clones actually work.
    clt_root=/Library/Developer/CommandLineTools
    rm -rf "$resources_dir/git/libexec" "$resources_dir/git/share"
    mkdir -p "$resources_dir/git/libexec" "$resources_dir/git/share"
    cp -R "$clt_root/usr/libexec/git-core" "$resources_dir/git/libexec/"
    cp -R "$clt_root/usr/share/git-core" "$resources_dir/git/share/"
else
    die "expected real git at $real_git; install Xcode Command Line Tools first"
fi

log "building workspace wheels into $wheels_dir"
rm -rf "$wheels_dir"
mkdir -p "$wheels_dir"

for entry in "${WORKSPACE_PACKAGES[@]}"; do
    pkg_dir="${entry%%:*}"
    [[ -d "$repo_root/$pkg_dir" ]] || die "workspace package not found: $repo_root/$pkg_dir"
    log "  uv build $pkg_dir"
    uv build --quiet --wheel --out-dir "$wheels_dir" "$repo_root/$pkg_dir"
done

# Resolve the wheel filename for each workspace package and emit a
# bundled pyproject pointing every project at its local wheel. We match by
# the underscored project name because Python wheel filenames replace
# hyphens with underscores.
log "generating wheel-aware pyproject at $pyproject_dir"
mkdir -p "$pyproject_dir"

python3 - "$wheels_dir" "$pyproject_dir" "${WORKSPACE_PACKAGES[@]}" <<'PY'
import sys
from pathlib import Path

wheels_dir = Path(sys.argv[1])
pyproject_dir = Path(sys.argv[2])
pkg_entries = sys.argv[3:]

deps_lines = []
sources_lines = []
override_lines = []
for entry in pkg_entries:
    _, name = entry.split(":", 1)
    wheel_prefix = name.replace("-", "_") + "-"
    matches = sorted(wheels_dir.glob(f"{wheel_prefix}*.whl"))
    if not matches:
        raise SystemExit(f"no wheel found for {name} under {wheels_dir}")
    wheel = matches[-1].name
    deps_lines.append(f'    "{name}>=0.0.0",')
    sources_lines.append(f'{name} = {{ path = "../wheels/{wheel}" }}')
    # Workspace packages pin each other with == in their pyproject.toml
    # (mngr_imbue_cloud declares imbue-mngr==0.2.6 etc.). The pin matches in
    # a workspace install because uv.sources rewrites the resolution, but
    # those == constraints leak into the built wheels' metadata and prevent
    # the bundled pyproject from picking the wheel of a different version
    # we just built. Override every workspace project to "any" so the
    # resolver accepts our locally-built wheel regardless of upstream pins.
    override_lines.append(f'"{name}"')

pyproject_text = (
    "[project]\n"
    'name = "minds-desktop"\n'
    'version = "0.1.0"\n'
    'requires-python = ">=3.12"\n'
    "dependencies = [\n"
    + "\n".join(deps_lines) + "\n"
    "]\n\n"
    "[tool.uv.sources]\n"
    + "\n".join(sources_lines) + "\n\n"
    "[tool.uv]\n"
    "override-dependencies = ["
    + ", ".join(override_lines)
    + "]\n"
)
(pyproject_dir / "pyproject.toml").write_text(pyproject_text)
print(f"wrote {pyproject_dir/'pyproject.toml'}")
PY

# Generate uv.lock against the wheel sources so the packaged runtime can
# uv sync without reaching out to PyPI for our workspace deps.
log "generating uv.lock against the wheel sources"
( cd "$pyproject_dir" && uv lock --quiet )

# electron/paths.js::getBundledConfigDir() looks for
# <pyproject_dir>/imbue/minds/config/envs/_bundled/{client.toml,root_name}
# in packaged mode. build.js writes those files into the source tree but
# does not stage them under resources/pyproject/, which is fine for the
# stock ToDesktop build (which copies the imbue tree there itself) but
# leaves @electron/packager output without them. Mirror that staging step
# explicitly so the packaged runtime can find the client config.
bundled_src="$minds_dir/imbue/minds/config/envs/_bundled"
if [[ -f "$bundled_src/client.toml" ]]; then
    bundled_dst="$pyproject_dir/imbue/minds/config/envs/_bundled"
    log "staging $bundled_src -> $bundled_dst"
    mkdir -p "$bundled_dst"
    cp "$bundled_src/client.toml" "$bundled_dst/client.toml"
    [[ -f "$bundled_src/root_name" ]] && cp "$bundled_src/root_name" "$bundled_dst/root_name"
fi

# electron-packager's pnpm support follows symlinks into the global store
# and chokes on transitive deps. Swap to a flat npm install just for the
# packaging step. We restore the pnpm install at the end so dev mode keeps
# working.
log "installing node deps with npm for flat node_modules (packaging compat)"
(
    cd "$minds_dir"
    rm -rf node_modules
    npm install --silent --no-fund --no-audit
)

# Determine output directory.
out_dir="${out_override:-$minds_dir/dist}"
log "running @electron/packager into $out_dir"
(
    cd "$minds_dir"
    npx --yes @electron/packager . minds \
        --platform=darwin \
        --arch=arm64 \
        --out="$out_dir" \
        --overwrite \
        --extra-resource=resources/uv \
        --extra-resource=resources/git \
        --extra-resource=resources/lima \
        --extra-resource=resources/latchkey \
        --extra-resource=resources/pyproject \
        --extra-resource=resources/wheels \
        2>&1 | sed 's/^/[packager] /'
)

app_path="$out_dir/minds-darwin-arm64/minds.app"
[[ -d "$app_path" ]] || die "expected $app_path after packaging"

# Restore pnpm install so subsequent `pnpm start` / dev iteration works.
log "restoring pnpm install for dev-mode compatibility"
( cd "$minds_dir" && rm -rf node_modules && pnpm install --silent )

log "local minds.app ready: $app_path"
printf '%s\n' "$app_path"
