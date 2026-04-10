#!/bin/bash
# Build the mngr Lima base image using Lima itself.
#
# This boots an Alpine VM via Lima, runs the provision script inside it,
# then exports the disk image. Much simpler than packer since Lima already
# knows how to boot cloud images with cloud-init.
#
# Prerequisites:
#   - limactl (https://lima-vm.io/)
#
# Usage:
#   ./scripts/build-lima-image.sh              # build for current arch
#   ./scripts/build-lima-image.sh --arch arm64  # build for arm64
#   ./scripts/build-lima-image.sh --arch amd64  # build for amd64
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARCH=""
INSTANCE_NAME="mngr-image-build"

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
        aarch64|arm64) ARCH="aarch64" ;;
        x86_64|amd64)  ARCH="x86_64" ;;
        *)
            echo "Unsupported architecture: $(uname -m)"
            exit 1
            ;;
    esac
fi

echo "Building mngr Lima image for $ARCH..."

# Clean up any previous build instance
limactl delete -f "$INSTANCE_NAME" 2>/dev/null || true

# Create a minimal Lima YAML config for the build VM
BUILD_YAML=$(mktemp /tmp/mngr-build-XXXXXX.yaml)
cat > "$BUILD_YAML" <<EOF
images:
  - location: "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/nocloud_alpine-3.23.3-${ARCH}-uefi-cloudinit-r0.qcow2"
    arch: "${ARCH}"
containerd:
  system: false
  user: false
mounts: []
portForwards: []
provision:
  - mode: system
    script: |
      #!/bin/sh
      # Just ensure bash is available for the provision script
      apk add --no-cache bash
EOF

echo "Starting build VM..."
limactl start --name="$INSTANCE_NAME" "$BUILD_YAML" --cpus=2 --memory=4 --disk=10
rm -f "$BUILD_YAML"

echo "Running provision script..."
limactl copy "$SCRIPT_DIR/packer/provision.sh" "${INSTANCE_NAME}:/tmp/provision.sh"
limactl shell "$INSTANCE_NAME" -- sudo bash /tmp/provision.sh

echo "Cleaning up build artifacts in VM..."
limactl shell "$INSTANCE_NAME" -- sudo sh -c '
    cloud-init clean --logs 2>/dev/null || true
    rm -f /tmp/provision.sh
    # Clear SSH host keys so they are regenerated on first real boot
    rm -f /etc/ssh/ssh_host_*
'

# Export the disk image BEFORE stopping (Lima cleans up disk files on stop)
OUTPUT_DIR="$SCRIPT_DIR/packer/output-mngr-lima-${ARCH}"
OUTPUT_FILE="$OUTPUT_DIR/mngr-lima-${ARCH}.qcow2"
mkdir -p "$OUTPUT_DIR"

# Find the disk image -- Lima uses different names across versions
LIMA_DIR="$HOME/.lima/${INSTANCE_NAME}"
LIMA_DISK=""
for candidate in "$LIMA_DIR/disk" "$LIMA_DIR/diffdisk" "$LIMA_DIR/basedisk" "$LIMA_DIR/disk.qcow2"; do
    if [ -f "$candidate" ]; then
        LIMA_DISK="$candidate"
        break
    fi
done
# Fall back to finding any qcow2 file
if [ -z "$LIMA_DISK" ]; then
    LIMA_DISK=$(find "$LIMA_DIR" -name "*.qcow2" -type f 2>/dev/null | head -1)
fi

if [ -n "$LIMA_DISK" ] && [ -f "$LIMA_DISK" ]; then
    echo "Exporting disk image from $LIMA_DISK..."
    cp "$LIMA_DISK" "$OUTPUT_FILE"
else
    echo "ERROR: Could not find Lima disk image in $LIMA_DIR"
    echo "Contents:"
    ls -la "$LIMA_DIR/" 2>/dev/null || true
    limactl delete -f "$INSTANCE_NAME"
    exit 1
fi

echo "Stopping and deleting build VM..."
limactl stop "$INSTANCE_NAME" 2>/dev/null || true
limactl delete -f "$INSTANCE_NAME"

# Compact the qcow2 image
if command -v qemu-img >/dev/null 2>&1; then
    echo "Compacting image..."
    qemu-img convert -O qcow2 -c "$OUTPUT_FILE" "${OUTPUT_FILE}.tmp"
    mv "${OUTPUT_FILE}.tmp" "$OUTPUT_FILE"
fi

IMAGE_SIZE=$(du -h "$OUTPUT_FILE" | cut -f1)
echo ""
echo "Build complete: $OUTPUT_FILE ($IMAGE_SIZE)"
echo ""
echo "To publish, run:"
echo "  ./scripts/publish-lima-image.sh $OUTPUT_FILE"
