#!/usr/bin/env bash

# This script exists to create a tarball of a "keyframe" commit of the current git repository. This keyframe is then
# cached within the Docker or Modal image, which allows us to speed up CI builds by avoiding having to clone the entire
# git history every time.
set -euo pipefail

HASH="$1"
DEST="$2"

# Used below to drop the current copy of post-source-setup.sh alongside
# the tarball. Pulled from the working-tree copy (next to this script)
# rather than the keyframe checkout, because the pinned
# image_commit_hash can predate the script's introduction (2026-05-08).
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

mkdir -p "$DEST";


# Also drops scripts/post-source-setup.sh next to the tarball so the
# Dockerfile can `COPY scripts/post-source-setup.sh ...` it into the
# image before extracting the tarball -- the keyframe shape would
# otherwise leave the script trapped inside the tarball with no way
# for the extracting RUN to invoke it.
[ -e "$DEST/$HASH.checkpoint" ] || ( \
  tmp=$(mktemp -d); \
  rm -rf "$tmp"; \
  real_origin="https://github.com/$(git remote get-url origin | sed 's|.*github.com[:/]||')"; \
  git clone --no-hardlinks . "$tmp"; \
  git -C "$tmp" remote set-url origin "$real_origin"; \
  git -C "$tmp" checkout "$HASH"; \
  mv "$tmp" "$DEST/$HASH"; \
  COPYFILE_DISABLE=1 tar czf "$DEST/current.tar.gz" -C "$DEST/$HASH" .; \
  mkdir -p "$DEST/scripts"; \
  cp "$SCRIPT_DIR/post-source-setup.sh" "$DEST/scripts/post-source-setup.sh"; \
  rm -rf "$DEST/$HASH"; \
  touch "$DEST/$HASH.checkpoint"; \
)
