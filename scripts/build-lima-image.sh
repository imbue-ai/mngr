#!/bin/bash
# Build the mngr Lima base image using Packer.
#
# Prerequisites:
#   - packer (https://www.packer.io/)
#   - qemu-system-* (for the target architecture)
#
# Usage:
#   ./scripts/build-lima-image.sh              # build for current arch
#   ./scripts/build-lima-image.sh --arch arm64  # build for arm64
#   ./scripts/build-lima-image.sh --arch amd64  # build for amd64
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKER_DIR="$SCRIPT_DIR/packer"
ARCH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --arch)
            ARCH="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

# Auto-detect architecture if not specified
if [ -z "$ARCH" ]; then
    case "$(uname -m)" in
        aarch64|arm64) ARCH="arm64" ;;
        x86_64|amd64)  ARCH="amd64" ;;
        *)
            echo "Unsupported architecture: $(uname -m)"
            exit 1
            ;;
    esac
fi

echo "Building mngr Lima image for $ARCH..."

# Initialize Packer plugins
cd "$PACKER_DIR"
packer init .

# Build the image
packer build \
    -var "arch=$ARCH" \
    mngr-lima.pkr.hcl

OUTPUT_DIR="output-mngr-lima-$([ "$ARCH" = "arm64" ] && echo "aarch64" || echo "x86_64")"
OUTPUT_FILE="$OUTPUT_DIR/mngr-lima-$([ "$ARCH" = "arm64" ] && echo "aarch64" || echo "x86_64").qcow2"

echo ""
echo "Build complete: $PACKER_DIR/$OUTPUT_FILE"
echo ""
echo "To publish, run:"
echo "  ./scripts/publish-lima-image.sh $PACKER_DIR/$OUTPUT_FILE"
