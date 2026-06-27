#!/usr/bin/env bash
# Build a pre-baked Lima VM image for one architecture, using Lima itself (issue 2306).
#
# Baking *with* Lima (rather than a separate Packer/QEMU pipeline) means the image
# is produced by the same virtualizer that consumes it -- `vz` on Apple Silicon,
# accelerated QEMU on Linux -- so the artifact is guaranteed Lima-bootable and the
# macOS build host needs no extra QEMU/Packer toolchain. Emits both a qcow2 (the
# Lima format) and a raw image (what scripts/lima_image/publish.py chunks -- qcow2
# metadata churn would amplify desync deltas, so we chunk raw).
#
# Run one arch per native host (amd64 on a Linux/KVM host, arm64 on an
# Apple-Silicon Mac). Then publish with scripts/lima_image/publish.py.
#
# Usage:
#   ./scripts/build-lima-image.sh --fct-ref minds-v0.3.4 [--arch amd64|arm64]
#                                 [--fct-repo URL] [--cpus N] [--memory GiB]
#                                 [--disk GiB] [--keep]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIMA_IMAGE_DIR="$SCRIPT_DIR/lima_image"

ARCH=""
FCT_REF=""
FCT_REPO="https://github.com/imbue-ai/forever-claude-template.git"
CPUS=4
MEMORY=8
DISK=40
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch) ARCH="$2"; shift 2 ;;
    --fct-ref) FCT_REF="$2"; shift 2 ;;
    --fct-repo) FCT_REPO="$2"; shift 2 ;;
    --cpus) CPUS="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$FCT_REF" ]; then
  echo "ERROR: --fct-ref <minds-v...> is required" >&2
  exit 1
fi
if [ -z "$ARCH" ]; then
  case "$(uname -m)" in
    aarch64|arm64) ARCH="arm64" ;;
    x86_64|amd64)  ARCH="amd64" ;;
    *) echo "Unsupported host architecture: $(uname -m)" >&2; exit 1 ;;
  esac
fi

if [ "$ARCH" = "arm64" ]; then
  ARCH_TAG="aarch64"; LIMA_ARCH="aarch64"
  DEBIAN_URL="https://cloud.debian.org/images/cloud/bookworm/20260601-2496/debian-12-genericcloud-arm64-20260601-2496.qcow2"
else
  ARCH_TAG="x86_64"; LIMA_ARCH="x86_64"
  DEBIAN_URL="https://cloud.debian.org/images/cloud/bookworm/20260601-2496/debian-12-genericcloud-amd64-20260601-2496.qcow2"
fi

INSTANCE="mngr-lima-bake-$ARCH_TAG"
OUTPUT_DIR="$LIMA_IMAGE_DIR/output-$ARCH_TAG"
QCOW2_OUT="$OUTPUT_DIR/mngr-lima-$ARCH_TAG.qcow2"
RAW_OUT="$OUTPUT_DIR/mngr-lima-$ARCH_TAG.raw"
mkdir -p "$OUTPUT_DIR"

# Lima's per-instance state dir (honor LIMA_HOME, as limactl itself does).
LIMA_INSTANCE_DIR="${LIMA_HOME:-$HOME/.lima}/$INSTANCE"

TMP_YAML=""
cleanup() {
  [ -n "$TMP_YAML" ] && rm -f "$TMP_YAML"
  if [ "$KEEP" = "1" ]; then
    echo "(--keep) leaving Lima instance '$INSTANCE' in place"
  else
    limactl delete -f "$INSTANCE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "Building Lima image: arch=$ARCH instance=$INSTANCE fct_ref=$FCT_REF"

# Start from a clean slate.
limactl delete -f "$INSTANCE" >/dev/null 2>&1 || true

# Minimal Lima config: just the Debian base sized for the toolchain build. We run
# the bake via `limactl shell` (below) rather than a Lima `provision:` block so the
# bake script stays a normal file we can lint/version independently.
TMP_YAML="$(mktemp -t mngr-lima-bake-XXXXXX.yaml)"
cat > "$TMP_YAML" <<EOF
images:
  - location: "$DEBIAN_URL"
    arch: "$LIMA_ARCH"
cpus: $CPUS
memory: "${MEMORY}GiB"
disk: "${DISK}GiB"
mounts: []
EOF

echo "==> Starting Lima instance (downloads base + boots)"
# `limactl start` occasionally fails transiently -- e.g. a TLS timeout on the
# base-image freshness check can leave the instance half-initialized
# ("open .../image: no such file"). Retry a few times, deleting the partial
# instance and backing off between attempts.
START_ATTEMPTS=3
start_ok=0
for attempt in $(seq 1 "$START_ATTEMPTS"); do
  if limactl start --name="$INSTANCE" --tty=false "$TMP_YAML"; then
    start_ok=1
    break
  fi
  echo "WARN: 'limactl start' failed (attempt $attempt/$START_ATTEMPTS); cleaning up + retrying" >&2
  limactl delete -f "$INSTANCE" >/dev/null 2>&1 || true
  [ "$attempt" -lt "$START_ATTEMPTS" ] && sleep $((attempt * 10))
done
if [ "$start_ok" != "1" ]; then
  echo "ERROR: 'limactl start' failed after $START_ATTEMPTS attempts" >&2
  exit 1
fi
rm -f "$TMP_YAML"; TMP_YAML=""

echo "==> Copying the bake provisioner into the VM"
limactl copy "$LIMA_IMAGE_DIR/bake_provision.sh" "$INSTANCE:/tmp/bake_provision.sh"

echo "==> Running the FCT toolchain bake inside the VM (this is the long pole)"
limactl shell --workdir / "$INSTANCE" sudo env \
  FCT_REPO_URL="$FCT_REPO" FCT_REF="$FCT_REF" bash /tmp/bake_provision.sh

echo "==> Stopping the VM for a consistent disk"
limactl stop "$INSTANCE"

# Locate Lima's writable disk. Lima 2.x stores it as `disk`; older versions used
# a `diffdisk` overlay (qcow2 backed by `basedisk`). `qemu-img convert` reads the
# input format from the header and follows any backing chain, so it writes a
# standalone image either way (run while the instance dir still has its base).
DISK_FILE=""
for candidate in disk diffdisk; do
  if [ -f "$LIMA_INSTANCE_DIR/$candidate" ]; then
    DISK_FILE="$LIMA_INSTANCE_DIR/$candidate"
    break
  fi
done
if [ -z "$DISK_FILE" ]; then
  echo "ERROR: could not find a Lima disk under $LIMA_INSTANCE_DIR" >&2
  ls -la "$LIMA_INSTANCE_DIR" >&2 || true
  exit 1
fi
echo "==> Flattening the Lima disk ($DISK_FILE) to a standalone qcow2 + raw"
qemu-img convert -O qcow2 "$DISK_FILE" "$QCOW2_OUT"
qemu-img convert -f qcow2 -O raw "$QCOW2_OUT" "$RAW_OUT"

echo ""
echo "Build complete:"
echo "  qcow2: $QCOW2_OUT"
echo "  raw:   $RAW_OUT"
echo ""
echo "Publish with:"
echo "  uv run python scripts/lima_image/publish.py \\"
echo "    --version $FCT_REF --arch $ARCH_TAG --raw-image $RAW_OUT \\"
echo "    --bucket <r2-bucket> --secret-key-file <minisign.key> --uploader s3"
