#!/bin/bash
# Build the mngr Lima base image using Packer.
#
# Prerequisites:
#   - packer (https://www.packer.io/)
#   - qemu-system-* (for the target architecture)
#
# Usage:
#   ./scripts/build-lima-image.sh                         # build for current arch, latest Claude
#   ./scripts/build-lima-image.sh --arch arm64            # build for arm64
#   ./scripts/build-lima-image.sh --arch amd64            # build for amd64
#   ./scripts/build-lima-image.sh --claude-version 2.1.75 # pin Claude Code version
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKER_DIR="$SCRIPT_DIR/packer"
ARCH=""
CLAUDE_CODE_VERSION=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --arch)
            ARCH="$2"
            shift 2
            ;;
        --claude-version)
            CLAUDE_CODE_VERSION="$2"
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

# Pick the right QEMU accelerator for the host OS. Mac -> hvf (Apple's
# Hypervisor.framework). Linux -> kvm. Anything else -> tcg (slow emulation,
# but at least works). Cross-arch builds (e.g. amd64 on Apple Silicon) will
# fail to use hardware accel and silently fall back, so only pass native accel
# when the host arch matches the build arch.
HOST_OS="$(uname -s)"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    aarch64|arm64) HOST_ARCH_NORMALIZED="arm64" ;;
    x86_64|amd64)  HOST_ARCH_NORMALIZED="amd64" ;;
    *)             HOST_ARCH_NORMALIZED="$HOST_ARCH" ;;
esac
if [ "$HOST_ARCH_NORMALIZED" = "$ARCH" ]; then
    case "$HOST_OS" in
        Darwin) ACCELERATOR="hvf" ;;
        Linux)  ACCELERATOR="kvm" ;;
        *)      ACCELERATOR="tcg" ;;
    esac
else
    ACCELERATOR="tcg"
fi

echo "Building mngr Lima image for $ARCH (host $HOST_OS/$HOST_ARCH, accelerator $ACCELERATOR)..."

# Initialize Packer plugins
cd "$PACKER_DIR"
packer init .

# Build the image
packer build \
    -var "arch=$ARCH" \
    -var "accelerator=$ACCELERATOR" \
    -var "claude_code_version=$CLAUDE_CODE_VERSION" \
    mngr-lima.pkr.hcl

OUTPUT_DIR="output-mngr-lima-$([ "$ARCH" = "arm64" ] && echo "aarch64" || echo "x86_64")"
OUTPUT_FILE="$OUTPUT_DIR/mngr-lima-$([ "$ARCH" = "arm64" ] && echo "aarch64" || echo "x86_64").qcow2"

echo ""
echo "Build complete: $PACKER_DIR/$OUTPUT_FILE"
echo ""
echo "To publish, run:"
echo "  ./scripts/publish-lima-image.sh $PACKER_DIR/$OUTPUT_FILE"
