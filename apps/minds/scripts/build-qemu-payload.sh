#!/usr/bin/env bash
# Produce the self-contained qemu-img payload bundled into the minds desktop
# app: bin/qemu-img plus its transitive dylib closure under lib/, with every
# Mach-O's load commands rewritten to @executable_path/../lib via dylibbundler.
# The result drops into resources/qemu/ and runs with no Homebrew present.
#
# Emits a per-arch tarball to upload to the `qemu-img-v<version>` GitHub
# release, prints the SHA256 to pin in scripts/download-binaries.js, and prints
# the mac.additionalBinariesToSign entries for todesktop.js (ToDesktop signs
# each shipped Mach-O bottom-up under the hardened runtime).
#
# macOS only (Mach-O + dylibbundler). Run once per darwin arch on a matching
# host: an arm64 host yields the aarch64 payload, an Intel host the x86_64 one.
#
# Prereqs: `brew install qemu dylibbundler`.
#
# Usage: scripts/build-qemu-payload.sh [OUTPUT_DIR]
#   OUTPUT_DIR holds the staged payload and the tarball (default: a temp dir).

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build-qemu-payload.sh: macOS only (produces a Mach-O payload); got $(uname -s)." >&2
  exit 1
fi

for tool in dylibbundler otool shasum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "build-qemu-payload.sh: '$tool' not found on PATH. Run \`brew install qemu dylibbundler\`." >&2
    exit 1
  fi
done

QEMU_PREFIX="${QEMU_PREFIX:-$(brew --prefix qemu 2>/dev/null || true)}"
QEMU_IMG_SRC="${QEMU_PREFIX%/}/bin/qemu-img"
if [[ ! -x "$QEMU_IMG_SRC" ]]; then
  echo "build-qemu-payload.sh: qemu-img not found at $QEMU_IMG_SRC. Run \`brew install qemu\` or set QEMU_PREFIX." >&2
  exit 1
fi

VERSION="$("$QEMU_IMG_SRC" --version | sed -n '1s/.*version \([0-9][0-9.]*\).*/\1/p')"
if [[ -z "$VERSION" ]]; then
  echo "build-qemu-payload.sh: could not parse qemu-img version from '$("$QEMU_IMG_SRC" --version | head -1)'." >&2
  exit 1
fi

case "$(uname -m)" in
  arm64) ARCH="aarch64" ;;
  x86_64) ARCH="x86_64" ;;
  *) echo "build-qemu-payload.sh: unsupported arch $(uname -m)." >&2; exit 1 ;;
esac

OUT_DIR="${1:-$(mktemp -d "${TMPDIR:-/tmp}/minds-qemu-payload.XXXXXX")}"
mkdir -p "$OUT_DIR"
STAGE="$OUT_DIR/qemu"
rm -rf "$STAGE"
mkdir -p "$STAGE/bin"

cp "$QEMU_IMG_SRC" "$STAGE/bin/qemu-img"
chmod 0755 "$STAGE/bin/qemu-img"

echo "==> Relocating qemu-img $VERSION ($ARCH) dylib closure with dylibbundler"
# -od overwrite+create dest dir, -b fix the binary, -x also fix each copied lib,
# -p the inner install path load commands are rewritten to. dylibbundler
# ad-hoc re-signs each Mach-O so it runs locally for the smoke test below;
# ToDesktop replaces those signatures with the Developer ID at notarization.
# install_name_tool prints an expected "will invalidate the code signature"
# warning per rewrite; keep that noise in a log and surface it only on failure.
BUNDLER_LOG="$OUT_DIR/dylibbundler.log"
if ! dylibbundler -od -b -x "$STAGE/bin/qemu-img" -d "$STAGE/lib" -p '@executable_path/../lib' >"$BUNDLER_LOG" 2>&1; then
  echo "build-qemu-payload.sh: dylibbundler failed:" >&2
  cat "$BUNDLER_LOG" >&2
  exit 1
fi

echo "==> Verifying no Homebrew paths remain in any load command"
LEAKS="$(otool -L "$STAGE/bin/qemu-img" "$STAGE"/lib/*.dylib | grep -E '/opt/homebrew|/Cellar/' || true)"
if [[ -n "$LEAKS" ]]; then
  echo "build-qemu-payload.sh: relocated payload still references Homebrew paths:" >&2
  echo "$LEAKS" >&2
  exit 1
fi

echo "==> Smoke-testing a raw -> qcow2 conversion with the relocated binary"
SMOKE="$OUT_DIR/smoke"
rm -rf "$SMOKE"; mkdir -p "$SMOKE"
dd if=/dev/zero of="$SMOKE/in.raw" bs=1m count=4 >/dev/null 2>&1
env -i PATH=/usr/bin:/bin "$STAGE/bin/qemu-img" convert -f raw -O qcow2 "$SMOKE/in.raw" "$SMOKE/out.qcow2"
env -i PATH=/usr/bin:/bin "$STAGE/bin/qemu-img" info "$SMOKE/out.qcow2" >/dev/null
rm -rf "$SMOKE"

TARBALL="$OUT_DIR/qemu-img-${VERSION}-darwin-${ARCH}.tar.gz"
echo "==> Writing $TARBALL"
# Deterministic-ish archive so a rebuild on the same Homebrew closure reproduces
# the SHA: sorted member order, normalized mtime/owner, no AppleDouble sidecars,
# and gzip -n (no embedded name/timestamp). The closure itself still tracks the
# host's Homebrew dependency versions, so regenerate the SHA + signing list
# together whenever those move.
export COPYFILE_DISABLE=1
find "$STAGE" -exec touch -h -t 202001010000.00 {} + 2>/dev/null || true
( cd "$STAGE" && find bin lib \( -type f -o -type l \) | LC_ALL=C sort ) >"$OUT_DIR/manifest.txt"
( cd "$STAGE" && tar --uid 0 --gid 0 --numeric-owner --no-mac-metadata -cf - -T "$OUT_DIR/manifest.txt" ) | gzip -n -9 >"$TARBALL"

SHA256="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"

echo
echo "Payload staged at: $STAGE (bin/qemu-img + $(find "$STAGE/lib" -name '*.dylib' | wc -l | tr -d ' ') dylibs)"
echo "Tarball:           $TARBALL"
echo
echo "Pin in scripts/download-binaries.js EXPECTED_SHA256:"
echo "  'qemu-img-${VERSION}-darwin-${ARCH}.tar.gz': '${SHA256}',"
echo
echo "Add to todesktop.js mac.additionalBinariesToSign (dylibs first, qemu-img last):"
for dylib in "$STAGE"/lib/*.dylib; do
  echo "      'resources/qemu/lib/$(basename "$dylib")',"
done
echo "      'resources/qemu/bin/qemu-img',"
