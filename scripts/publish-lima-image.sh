#!/bin/bash
# Publish mngr Lima images to GitHub Releases.
#
# Prerequisites:
#   - gh (GitHub CLI, authenticated)
#
# Usage:
#   ./scripts/publish-lima-image.sh <image_file> [<image_file>...]
#
# Example:
#   # Build both architectures, then publish
#   ./scripts/build-lima-image.sh --arch amd64
#   ./scripts/build-lima-image.sh --arch arm64
#   ./scripts/publish-lima-image.sh \
#       scripts/packer/output-mngr-lima-x86_64/mngr-lima-x86_64.qcow2 \
#       scripts/packer/output-mngr-lima-aarch64/mngr-lima-aarch64.qcow2
#
set -euo pipefail

if [ $# -eq 0 ]; then
    echo "Usage: $0 <image_file> [<image_file>...]"
    exit 1
fi

# Verify all files exist
for file in "$@"; do
    if [ ! -f "$file" ]; then
        echo "File not found: $file"
        exit 1
    fi
done

TAG="lima-image-v0.1.0"
REPO="imbue-ai/mngr"

echo "Publishing to $REPO release $TAG..."

# Create the release if it doesn't exist
if ! gh release view "$TAG" --repo "$REPO" > /dev/null 2>&1; then
    echo "Creating release $TAG..."
    gh release create "$TAG" \
        --repo "$REPO" \
        --title "Lima VM Base Image v0.1.0" \
        --notes "Pre-built Lima VM images for mngr. Ubuntu LTS with mngr dependencies pre-installed." \
        --prerelease
fi

# Upload each image file
for file in "$@"; do
    echo "Uploading $(basename "$file")..."
    gh release upload "$TAG" "$file" --repo "$REPO" --clobber
done

echo ""
echo "Done. Images available at:"
echo "  https://github.com/$REPO/releases/tag/$TAG"
