#/bin/bash
# This script generates a patch file that captures the differences between a specified checkpoint commit and the current
# state of the repository.

set -euo pipefail

CHECKPOINT_HASH="$1"

# Generate the patch file. If it's in the future relative to the checkpoint, this
# MUST raise an error

if [ -z "$CHECKPOINT_HASH" ]; then
  echo "Error: CHECKPOINT_HASH is not provided."
  exit 1
fi

if ! git rev-parse "$CHECKPOINT_HASH" >/dev/null 2>&1; then
  echo "Error: CHECKPOINT_HASH '$CHECKPOINT_HASH' does not exist in the repository."
  exit 1
fi

# Diff against the working tree so uncommitted changes are included
git diff "$CHECKPOINT_HASH"
