#!/usr/bin/env bash

# This script exists to create a tarball of a "keyframe" commit of the current git repository. This keyframe is then
# cached within the Docker or Modal image, which allows us to speed up CI builds by avoiding having to clone the entire
# git history every time.
set -euo pipefail

HASH="$1"
DEST="$2"

mkdir -p "$DEST";


# current.tar.gz holds $HASH's tree only while $HASH.checkpoint is the sole checkpoint.
checkpoints=("$DEST"/*.checkpoint)
[ "${checkpoints[*]}" = "$DEST/$HASH.checkpoint" ] || ( \
  tmp=$(mktemp -d); \
  rm -rf "$tmp"; \
  real_origin="https://github.com/$(git remote get-url origin | sed 's|.*github.com[:/]||')"; \
  git clone --no-hardlinks . "$tmp"; \
  git -C "$tmp" remote set-url origin "$real_origin"; \
  git -C "$tmp" checkout "$HASH"; \
  mv "$tmp" "$DEST/$HASH"; \
  rm -f "$DEST"/*.checkpoint; \
  COPYFILE_DISABLE=1 tar --no-xattrs -czf "$DEST/current.tar.gz" -C "$DEST/$HASH" .; \
  rm -rf "$DEST/$HASH"; \
  touch "$DEST/$HASH.checkpoint"; \
)

# The Dockerfile COPYs this manifest before it extracts the tarball.
manifest=apps/minds/scripts/git-manifest.json
if [ -f "$manifest" ]; then
  mkdir -p "$DEST/$(dirname "$manifest")"
  cp "$manifest" "$DEST/$manifest"
else
  rm -f "$DEST/$manifest"
fi
