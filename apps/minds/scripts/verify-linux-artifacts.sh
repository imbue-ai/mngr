#!/usr/bin/env bash
# Verify the two Linux x86_64 artifacts ToDesktop built are runnable Linux
# packages: every bundled tool is an x86-64 ELF that runs, the .deb selects the
# DebUpdater, and the bundled wheels resolve into a working `minds` on Linux.
#
# Usage: verify-linux-artifacts.sh <minds.AppImage> <minds.deb>
#
# Nothing is installed: the AppImage is extracted with --appimage-extract and
# the .deb with dpkg-deb -x, and uv syncs into a scratch directory.
set -euo pipefail

# Resolved up front: the AppImage is extracted from inside a scratch directory,
# where a relative path would no longer name the file.
APPIMAGE="$(realpath "${1:?path to the AppImage}")"
DEB="$(realpath "${2:?path to the .deb}")"

# Every extracted tree and the uv environment live under one scratch root,
# removed however the script ends: the two package trees run to hundreds of
# megabytes, and a failed sync leaves a managed CPython behind as well.
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# <resources-relative path>|<argument that makes it run and exit 0>. An empty
# argument means only the format is checked: git-remote-http is a helper git
# execs, with no version flag of its own.
RUNNABLE_TOOLS=(
  "uv/uv|--version"
  "git/bin/git|--version"
  "git/libexec/git-core/git-remote-http|"
  "restic/restic|version"
  "desync/desync|--help"
  "lima/bin/limactl|--version"
  "curl/latchkey-curl-dispatch|--version"
  "curl/latchkey-curl-impersonate|--version"
)

fail() {
  echo "::error::$*" >&2
  exit 1
}

assert_x86_64_elf() {
  local binary="$1"
  [[ -f "$binary" ]] || fail "missing bundled tool: $binary"
  local description
  description=$(file -b "$binary")
  case "$description" in
    *"ELF 64-bit LSB"*"x86-64"*) ;;
    *) fail "$binary is not an x86-64 ELF: $description" ;;
  esac
}

verify_tools() {
  local resources="$1"
  local entry relative argument binary
  for entry in "${RUNNABLE_TOOLS[@]}"; do
    relative="${entry%%|*}"
    argument="${entry#*|}"
    binary="$resources/$relative"
    assert_x86_64_elf "$binary"
    if [[ -n "$argument" ]]; then
      "$binary" "$argument" > /dev/null || fail "$binary $argument did not exit 0"
    fi
    echo "ok: $relative"
  done
}

# The bundled wheels and lockfile must resolve on Linux; `minds --help` proves
# the environment imports. Mirrors electron/env-setup.js: a managed CPython,
# synced into an explicit venv rather than <project>/.venv.
verify_python_environment() {
  local resources="$1"
  local scratch="$SCRATCH/python"
  local uv="$resources/uv/uv"
  local project="$resources/pyproject"
  [[ -f "$project/pyproject.toml" && -f "$project/uv.lock" ]] || fail "no bundled pyproject + uv.lock under $project"
  VIRTUAL_ENV="$scratch/venv" UV_CACHE_DIR="$scratch/cache" UV_PYTHON_INSTALL_DIR="$scratch/python" \
    "$uv" sync --project "$project" --active --python-preference only-managed --frozen \
    || fail "uv sync of the bundled pyproject failed"
  VIRTUAL_ENV="$scratch/venv" UV_CACHE_DIR="$scratch/cache" UV_PYTHON_INSTALL_DIR="$scratch/python" \
    "$uv" run --project "$project" --active --frozen minds --help > /dev/null \
    || fail "the synced environment cannot run minds --help"
  echo "ok: bundled wheels resolve and minds imports"
}

extract_appimage() {
  local work="$SCRATCH/appimage"
  mkdir "$work"
  chmod +x "$APPIMAGE"
  (cd "$work" && "$APPIMAGE" --appimage-extract > /dev/null) || fail "could not extract $APPIMAGE"
  echo "$work/squashfs-root"
}

extract_deb() {
  local work="$SCRATCH/deb"
  mkdir "$work"
  dpkg-deb -x "$DEB" "$work" || fail "could not extract $DEB"
  # electron-builder installs under /opt/<productName>.
  [[ -d "$work/opt" ]] || fail "$DEB installs nothing under /opt"
  local root
  root=$(find "$work/opt" -mindepth 1 -maxdepth 1 -type d | head -1)
  [[ -n "$root" ]] || fail "$DEB installs nothing under /opt"
  echo "$root"
}

package_type_of() {
  local resources="$1"
  if [[ -f "$resources/package-type" ]]; then
    tr -d '[:space:]' < "$resources/package-type"
  else
    echo "(absent)"
  fi
}

echo "== AppImage: $APPIMAGE"
appimage_root=$(extract_appimage)
# electron-builder names the Electron executable after package.json's `name`.
assert_x86_64_elf "$appimage_root/minds"
verify_tools "$appimage_root/resources"
# electron-updater picks the AppImageUpdater unless this file names another
# package; an AppImage that says deb would try to dpkg -i itself.
appimage_type=$(package_type_of "$appimage_root/resources")
[[ "$appimage_type" != "deb" ]] || fail "the AppImage carries a package-type of deb"
echo "ok: package-type $appimage_type"

echo "== deb: $DEB"
deb_root=$(extract_deb)
assert_x86_64_elf "$deb_root/minds"
verify_tools "$deb_root/resources"
deb_type=$(package_type_of "$deb_root/resources")
[[ "$deb_type" == "deb" ]] || fail "the .deb's resources/package-type reads '$deb_type', so electron-updater would run the AppImage updater inside it"
echo "ok: package-type deb"

# The shared tree is identical in both packages, so one sync proves both.
verify_python_environment "$deb_root/resources"
echo "== both Linux artifacts verified"
