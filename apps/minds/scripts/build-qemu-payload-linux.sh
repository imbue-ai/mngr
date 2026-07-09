#!/usr/bin/env bash
# Produce the self-contained qemu-img payload for Linux: a fully static ELF
# (musl) built in an Alpine container, so it runs on any distro with no
# runtime dependencies at all. Companion to build-qemu-payload.sh (macOS);
# same tarball layout (bin/qemu-img) and naming
# (qemu-img-<ver>-linux-<arch>.tar.gz), uploaded to the same
# `qemu-img-v<version>` GitHub release.
#
# Unlike macOS (no static libc), Linux allows -static, so QEMU's own
# --static flag does the whole job against Alpine's static musl/glib/pcre2
# packages. The QEMU source tarball is the same SHA256-pinned one the macOS
# producer uses; the static dependencies come from the pinned Alpine image.
#
# Requires a running Docker daemon. Builds one arch per invocation; the
# non-native arch runs under Docker's binfmt emulation (slower but correct).
#
# Usage: scripts/build-qemu-payload-linux.sh <x86_64|aarch64> [OUTPUT_DIR]

set -euo pipefail

QEMU_VERSION="10.2.2"
QEMU_SHA256="784b296ff29c1417aa72323abcb2d2ea9ab9771724f577dcd785c3b04f21e176"
ALPINE_IMAGE="alpine:3.21"

ARCH="${1:?usage: build-qemu-payload-linux.sh <x86_64|aarch64> [OUTPUT_DIR]}"
case "$ARCH" in
  x86_64) DOCKER_PLATFORM="linux/amd64" ;;
  aarch64) DOCKER_PLATFORM="linux/arm64" ;;
  *) echo "unsupported arch $ARCH (use x86_64 or aarch64)" >&2; exit 1 ;;
esac

OUT_DIR="${2:-$(mktemp -d "${TMPDIR:-/tmp}/minds-qemu-linux.XXXXXX")}"
mkdir -p "$OUT_DIR"

if ! docker ps >/dev/null 2>&1; then
  echo "build-qemu-payload-linux.sh: Docker daemon not reachable." >&2
  exit 1
fi

echo "==> Building static qemu-img $QEMU_VERSION for linux/$ARCH in $ALPINE_IMAGE"
docker run --rm --platform "$DOCKER_PLATFORM" -v "$OUT_DIR:/out" "$ALPINE_IMAGE" sh -euc "
  # pcre2's static lib ships in pcre2-dev (no separate -static package).
  apk add --no-cache build-base meson ninja pkgconf python3 bash perl curl xz \
    glib-dev glib-static pcre2-dev zlib-static zlib-dev \
    gettext-static gettext-dev libffi-dev util-linux-static >/dev/null
  cd /tmp
  curl -fsSL -o qemu.tar.xz https://download.qemu.org/qemu-${QEMU_VERSION}.tar.xz
  echo '${QEMU_SHA256}  qemu.tar.xz' | sha256sum -c -
  tar -xf qemu.tar.xz
  mkdir build && cd build
  ../qemu-${QEMU_VERSION}/configure --static \
    --without-default-features \
    --disable-system --disable-user --enable-tools \
    --disable-docs --disable-guest-agent >/dev/null
  ninja qemu-img >/dev/null
  ./qemu-img --version
  # verify fully static
  if ldd ./qemu-img 2>/dev/null | grep -q '=>'; then
    echo 'qemu-img is not fully static:' >&2; ldd ./qemu-img >&2; exit 1
  fi
  # smoke test: raw -> qcow2 -> info
  dd if=/dev/zero of=/tmp/in.raw bs=1M count=4 2>/dev/null
  ./qemu-img convert -f raw -O qcow2 /tmp/in.raw /tmp/out.qcow2
  ./qemu-img info /tmp/out.qcow2 >/dev/null
  install -m 0755 ./qemu-img /out/qemu-img
"

STAGE="$OUT_DIR/qemu"
rm -rf "$STAGE"
mkdir -p "$STAGE/bin"
mv "$OUT_DIR/qemu-img" "$STAGE/bin/qemu-img"
chmod 0755 "$STAGE/bin/qemu-img"

TARBALL="$OUT_DIR/qemu-img-${QEMU_VERSION}-linux-${ARCH}.tar.gz"
echo "==> Writing $TARBALL"
find "$STAGE" -exec touch -h -t 202001010000.00 {} + 2>/dev/null || true
( cd "$STAGE" && find bin \( -type f -o -type l \) | LC_ALL=C sort ) >"$OUT_DIR/manifest.txt"
( cd "$STAGE" && tar --uid 0 --gid 0 --numeric-owner -cf - -T "$OUT_DIR/manifest.txt" ) | gzip -n -9 >"$TARBALL"

SHA256="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
echo
echo "Tarball: $TARBALL"
echo "Pin in scripts/download-binaries.js EXPECTED_SHA256:"
echo "  'qemu-img-${QEMU_VERSION}-linux-${ARCH}.tar.gz': '${SHA256}',"
