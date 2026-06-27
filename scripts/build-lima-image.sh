#!/usr/bin/env bash
# Build a pre-baked Lima VM image for one architecture (issue #2306).
#
# Boots Debian 12 via Packer/QEMU, bakes the forever-claude-template toolchain for
# a given minds-v<version> tag, and emits both the qcow2 (Lima format) and a raw
# image (what scripts/lima_image/publish.py chunks -- qcow2 metadata churn would
# amplify deltas, so we chunk raw).
#
# Run one arch per native host (amd64 on KVM Linux, arm64 on an Apple-Silicon/HVF
# Mac). Then publish with scripts/lima_image/publish.py.
#
# Usage:
#   ./scripts/build-lima-image.sh --fct-ref minds-v0.3.4 [--arch amd64|arm64]
#                                 [--accelerator kvm|hvf|tcg]
#                                 [--fct-repo URL] [--iso-checksum file:URL|sha512:...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKER_DIR="$SCRIPT_DIR/packer"

ARCH=""
ACCELERATOR=""
FCT_REF=""
FCT_REPO="https://github.com/imbue-ai/forever-claude-template.git"
ISO_CHECKSUM="none"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch) ARCH="$2"; shift 2 ;;
    --accelerator) ACCELERATOR="$2"; shift 2 ;;
    --fct-ref) FCT_REF="$2"; shift 2 ;;
    --fct-repo) FCT_REPO="$2"; shift 2 ;;
    --iso-checksum) ISO_CHECKSUM="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$FCT_REF" ]; then
  echo "ERROR: --fct-ref <minds-v...> is required" >&2
  exit 1
fi

# Auto-detect arch + accelerator from the host when not given.
if [ -z "$ARCH" ]; then
  case "$(uname -m)" in
    aarch64|arm64) ARCH="arm64" ;;
    x86_64|amd64)  ARCH="amd64" ;;
    *) echo "Unsupported host architecture: $(uname -m)" >&2; exit 1 ;;
  esac
fi
if [ -z "$ACCELERATOR" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    ACCELERATOR="hvf"
  else
    ACCELERATOR="kvm"
  fi
fi

ARCH_TAG="$([ "$ARCH" = "arm64" ] && echo aarch64 || echo x86_64)"
OUTPUT_DIR="$PACKER_DIR/output-mngr-lima-$ARCH_TAG"
QCOW2_OUT="$OUTPUT_DIR/mngr-lima-$ARCH_TAG.qcow2"
RAW_OUT="$OUTPUT_DIR/mngr-lima-$ARCH_TAG.raw"

echo "Building Lima image: arch=$ARCH accelerator=$ACCELERATOR fct_ref=$FCT_REF"
rm -rf "$OUTPUT_DIR"

cd "$PACKER_DIR"
packer init .
packer build \
  -var "arch=$ARCH" \
  -var "accelerator=$ACCELERATOR" \
  -var "fct_ref=$FCT_REF" \
  -var "fct_repo_url=$FCT_REPO" \
  -var "iso_checksum=$ISO_CHECKSUM" \
  mngr-lima.pkr.hcl

echo "Converting qcow2 -> raw for chunking ..."
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
