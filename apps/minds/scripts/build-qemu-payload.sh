#!/usr/bin/env bash
# Produce the self-contained qemu-img payload bundled into the minds desktop
# app: a single static-deps Mach-O that links only always-present system
# libraries (/usr/lib + /System frameworks), built entirely from pinned
# source tarballs. Modeled on containers/podman-machine-qemu, narrowed to
# qemu-img: every optional QEMU feature is disabled, so the dependency set
# collapses to glib and glib's own deps (libffi, libintl, pcre2), all linked
# statically.
#
# Emits a per-arch tarball to upload to the `qemu-img-v<version>` GitHub
# release, prints the SHA256 to pin in scripts/download-binaries.js, and the
# single mac.additionalBinariesToSign entry for todesktop.js.
#
# macOS only. Run once per darwin arch on a matching host: an arm64 host
# yields the aarch64 payload, an Intel host the x86_64 one.
#
# Prereqs: Xcode CLT plus `brew install meson ninja pkg-config`.
#
# Usage: scripts/build-qemu-payload.sh [OUTPUT_DIR]
#   OUTPUT_DIR holds the staged payload and the tarball (default: a temp dir).
#   QEMU_PAYLOAD_WORK_DIR caches source tarballs + build trees across runs.

set -euo pipefail

QEMU_VERSION="10.2.2"

# Pinned source tarballs; SHA256s recorded from the official hosts. The
# dependency set follows containers/podman-machine-qemu, with libffi and glib
# on newer releases (libffi 3.4.4 miscompiles under current clang's CFI
# checks; glib 2.78's gdbus-codegen needs the distutils module that python
# 3.12 removed).
SOURCES=(
  "libffi-3.4.8.tar.gz|https://github.com/libffi/libffi/releases/download/v3.4.8/libffi-3.4.8.tar.gz|bc9842a18898bfacb0ed1252c4febcc7e78fa139fd27fdc7a3e30d9d9356119b"
  "gettext-0.22.4.tar.gz|https://ftp.gnu.org/gnu/gettext/gettext-0.22.4.tar.gz|c1e0bb2a4427a9024390c662cd532d664c4b36b8ff444ed5e54b115fdb7a1aea"
  "pcre2-10.42.tar.bz2|https://github.com/PCRE2Project/pcre2/releases/download/pcre2-10.42/pcre2-10.42.tar.bz2|8d36cd8cb6ea2a4c2bb358ff6411b0c788633a2a45dabbf1aeb4b701d1b5e840"
  "glib-2.84.4.tar.xz|https://download.gnome.org/sources/glib/2.84/glib-2.84.4.tar.xz|8a9ea10943c36fc117e253f80c91e477b673525ae45762942858aef57631bb90"
  "qemu-${QEMU_VERSION}.tar.xz|https://download.qemu.org/qemu-${QEMU_VERSION}.tar.xz|784b296ff29c1417aa72323abcb2d2ea9ab9771724f577dcd785c3b04f21e176"
)

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build-qemu-payload.sh: macOS only (produces a Mach-O payload); got $(uname -s)." >&2
  exit 1
fi

for tool in meson ninja pkg-config make otool shasum python3 cc; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "build-qemu-payload.sh: '$tool' not found on PATH. Install Xcode CLT and \`brew install meson ninja pkg-config\`." >&2
    exit 1
  fi
done

case "$(uname -m)" in
  arm64) ARCH="aarch64"; export MACOSX_DEPLOYMENT_TARGET=12.0 ;;
  x86_64) ARCH="x86_64"; export MACOSX_DEPLOYMENT_TARGET=10.15 ;;
  *) echo "build-qemu-payload.sh: unsupported arch $(uname -m)." >&2; exit 1 ;;
esac
NCORES="$(sysctl -n hw.ncpu)"

OUT_DIR="${1:-$(mktemp -d "${TMPDIR:-/tmp}/minds-qemu-payload.XXXXXX")}"
mkdir -p "$OUT_DIR"
WORK_DIR="${QEMU_PAYLOAD_WORK_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/minds-qemu-work.XXXXXX")}"
mkdir -p "$WORK_DIR"
PREFIX="$WORK_DIR/prefix"
mkdir -p "$PREFIX"

# Build env: our prefix first for headers/libs/pkg-config so the static deps
# we just built win over any Homebrew copies; deployment target pinned above
# so the binary runs on end-user macOS versions, not just the build host's.
export CFLAGS="-mmacosx-version-min=${MACOSX_DEPLOYMENT_TARGET}"
export CPPFLAGS="-I$PREFIX/include $CFLAGS"
export LDFLAGS="-L$PREFIX/lib"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export PATH="$PREFIX/bin:$PATH"

fetch_and_verify() {
  local name="$1" url="$2" sha="$3"
  local tarball="$WORK_DIR/$name"
  if [[ ! -f "$tarball" ]]; then
    echo "==> Downloading $name"
    curl -fsSL -o "$tarball" "$url"
  fi
  local actual
  actual="$(shasum -a 256 "$tarball" | awk '{print $1}')"
  if [[ "$actual" != "$sha" ]]; then
    echo "build-qemu-payload.sh: SHA256 mismatch for $name: expected $sha got $actual" >&2
    exit 1
  fi
}

extract() {
  local name="$1"
  local dir="$WORK_DIR/${name%.tar.*}"
  if [[ ! -d "$dir" ]]; then
    tar -xf "$WORK_DIR/$name" -C "$WORK_DIR"
  fi
  echo "$dir"
}

for entry in "${SOURCES[@]}"; do
  IFS='|' read -r name url sha <<<"$entry"
  fetch_and_verify "$name" "$url" "$sha"
done

# Every dep is built static-only (.a, no dylib), so the linker folds it into
# qemu-img and the payload has no dylib closure to relocate or co-sign.

build_libffi() {
  local src; src="$(extract libffi-3.4.8.tar.gz)"
  [[ -f "$PREFIX/lib/libffi.a" ]] && return 0
  echo "==> Building libffi (static)"
  (cd "$src" && ./configure --prefix="$PREFIX" --disable-shared --enable-static \
    --disable-dependency-tracking >/dev/null && make -j"$NCORES" >/dev/null && make install >/dev/null)
}

build_libintl() {
  # Only gettext-runtime (libintl); the full gettext tools are not needed.
  local src; src="$(extract gettext-0.22.4.tar.gz)"
  [[ -f "$PREFIX/lib/libintl.a" ]] && return 0
  echo "==> Building libintl (gettext-runtime, static)"
  (cd "$src/gettext-runtime" && ./configure --prefix="$PREFIX" --disable-shared --enable-static \
    --disable-dependency-tracking --disable-silent-rules --disable-java --disable-csharp \
    --disable-libasprintf >/dev/null && make -j"$NCORES" >/dev/null && make install >/dev/null)
}

build_pcre2() {
  local src; src="$(extract pcre2-10.42.tar.bz2)"
  [[ -f "$PREFIX/lib/libpcre2-8.a" ]] && return 0
  echo "==> Building pcre2 (static)"
  (cd "$src" && ./configure --prefix="$PREFIX" --disable-shared --enable-static \
    --disable-dependency-tracking >/dev/null && make -j"$NCORES" >/dev/null && make install >/dev/null)
}

build_glib() {
  local src; src="$(extract glib-2.84.4.tar.xz)"
  [[ -f "$PREFIX/lib/libglib-2.0.a" ]] && return 0
  echo "==> Building glib (static)"
  (cd "$src" && rm -rf _build && meson setup _build --prefix="$PREFIX" --libdir="$PREFIX/lib" \
    --buildtype=release --default-library=static --wrap-mode=nofallback \
    -Dtests=false -Dman=false -Dgtk_doc=false >/dev/null \
    && meson compile -C _build >/dev/null && meson install -C _build >/dev/null)

  # Static linking needs each .pc's private deps on the public link line
  # (pkg-config only emits Libs.private/Requires.private under --static,
  # which QEMU's meson does not pass). Fold private into public in-place.
  python3 - "$PREFIX/lib/pkgconfig" <<'PY'
import pathlib, re, sys
for pc in pathlib.Path(sys.argv[1]).glob("*.pc"):
    text = pc.read_text()
    fields = dict()
    for key in ("Requires", "Requires.private", "Libs", "Libs.private"):
        # [ \t]* not \s*: an empty field ("Libs.private:\n") must not let the
        # match cross the newline and swallow the following line.
        m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", text, re.M)
        fields[key] = m.group(1).strip() if m else ""
    merged_requires = " ".join(v for v in (fields["Requires"], fields["Requires.private"]) if v)
    merged_libs = " ".join(v for v in (fields["Libs"], fields["Libs.private"]) if v)
    text = re.sub(r"^Requires\.private:.*\n?", "", text, flags=re.M)
    text = re.sub(r"^Libs\.private:.*\n?", "", text, flags=re.M)
    if merged_requires:
        if re.search(r"^Requires:", text, re.M):
            text = re.sub(r"^Requires:.*$", f"Requires: {merged_requires}", text, flags=re.M)
        else:
            text += f"Requires: {merged_requires}\n"
    if merged_libs:
        if re.search(r"^Libs:", text, re.M):
            text = re.sub(r"^Libs:.*$", f"Libs: {merged_libs}", text, flags=re.M)
        else:
            text += f"Libs: {merged_libs}\n"
    pc.write_text(text)
PY
}

build_qemu_img() {
  local src; src="$(extract "qemu-${QEMU_VERSION}.tar.xz")"
  echo "==> Building qemu-img $QEMU_VERSION (tools only, all optional features off)"
  local bdir="$WORK_DIR/qemu-build"
  rm -rf "$bdir"; mkdir -p "$bdir"
  # --without-default-features turns every optional dependency off in one
  # flag (no gnutls/libssh/curl/zstd/... probes that could latch onto
  # Homebrew), leaving the mandatory glib and the system zlib/bzip2.
  # ninja is driven directly: QEMU's make wrapper forwards GNU-make jobserver
  # flags that Homebrew's ninja rejects.
  (cd "$bdir" && "$src/configure" --prefix="$PREFIX" \
    --without-default-features \
    --disable-system --disable-user --enable-tools \
    --disable-docs --disable-guest-agent >/dev/null \
    && ninja qemu-img >/dev/null)
  QEMU_IMG_BUILT="$bdir/qemu-img"
  [[ -x "$QEMU_IMG_BUILT" ]] || { echo "qemu-img not produced at $QEMU_IMG_BUILT" >&2; exit 1; }
}

build_libffi
build_libintl
build_pcre2
build_glib
build_qemu_img

STAGE="$OUT_DIR/qemu"
rm -rf "$STAGE"
mkdir -p "$STAGE/bin"
cp "$QEMU_IMG_BUILT" "$STAGE/bin/qemu-img"
chmod 0755 "$STAGE/bin/qemu-img"

echo "==> Verifying only system libraries are linked"
LEAKS="$(otool -L "$STAGE/bin/qemu-img" | tail -n +2 | awk '{print $1}' | grep -Ev '^(/usr/lib/|/System/)' || true)"
if [[ -n "$LEAKS" ]]; then
  echo "build-qemu-payload.sh: qemu-img links non-system libraries:" >&2
  echo "$LEAKS" >&2
  exit 1
fi

echo "==> Smoke-testing a raw -> qcow2 conversion with a scrubbed environment"
SMOKE="$OUT_DIR/smoke"
rm -rf "$SMOKE"; mkdir -p "$SMOKE"
dd if=/dev/zero of="$SMOKE/in.raw" bs=1m count=4 >/dev/null 2>&1
env -i PATH=/usr/bin:/bin "$STAGE/bin/qemu-img" convert -f raw -O qcow2 "$SMOKE/in.raw" "$SMOKE/out.qcow2"
env -i PATH=/usr/bin:/bin "$STAGE/bin/qemu-img" info "$SMOKE/out.qcow2" >/dev/null
rm -rf "$SMOKE"

TARBALL="$OUT_DIR/qemu-img-${QEMU_VERSION}-darwin-${ARCH}.tar.gz"
echo "==> Writing $TARBALL"
# Deterministic archive: sorted member order, normalized mtime/owner, no
# AppleDouble sidecars, gzip -n. Rebuilding from the same pinned sources in
# the same QEMU_PAYLOAD_WORK_DIR reproduces the SHA (the binary embeds
# source paths via assertion __FILE__ strings).
export COPYFILE_DISABLE=1
find "$STAGE" -exec touch -h -t 202001010000.00 {} + 2>/dev/null || true
( cd "$STAGE" && find bin \( -type f -o -type l \) | LC_ALL=C sort ) >"$OUT_DIR/manifest.txt"
( cd "$STAGE" && tar --uid 0 --gid 0 --numeric-owner --no-mac-metadata -cf - -T "$OUT_DIR/manifest.txt" ) | gzip -n -9 >"$TARBALL"

SHA256="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"

echo
echo "qemu-img $QEMU_VERSION ($ARCH): single static-deps binary, $(du -h "$STAGE/bin/qemu-img" | awk '{print $1}')"
echo "Linked libraries (all system):"
otool -L "$STAGE/bin/qemu-img" | tail -n +2
echo
echo "Tarball: $TARBALL"
echo
echo "Pin in scripts/download-binaries.js EXPECTED_SHA256:"
echo "  'qemu-img-${QEMU_VERSION}-darwin-${ARCH}.tar.gz': '${SHA256}',"
echo
echo "todesktop.js mac.additionalBinariesToSign entry:"
echo "      'resources/qemu/bin/qemu-img',"
