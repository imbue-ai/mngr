#!/usr/bin/env bash

# Materializes a "keyframe" commit of the current git repository at $DEST. Two artifacts get
# produced:
#   $DEST/<all repo files at HASH>     -- the source tree on disk (so $DEST works as a Docker/
#                                         Modal build context whose `COPY . /code/mngr/` ships
#                                         the real tree, including scripts/post-source-setup.sh
#                                         that the mngr Dockerfile invokes immediately after).
#   $DEST/current.tar.gz               -- the same tree as a tarball, kept for callers that
#                                         consume the tarball form (mngr_schedule's
#                                         unpack_current_tarball_in_place, test_modal_idle_shutdown).
#   $DEST/$HASH.checkpoint             -- 0-byte cache marker; re-runs with the same HASH skip.
#
# Both forms are required: the on-disk tree because Docker's COPY can only ship files actually
# present in the build context (the script can't extract its own tarball before it exists on
# disk to be invoked), and the tarball because it's a stable contract for the producer/consumer
# split used by mngr_schedule and the offload checkpoint cache.
set -euo pipefail

HASH="$1"
DEST="$2"

mkdir -p "$DEST"

if [ ! -e "$DEST/$HASH.checkpoint" ]; then
  tmp=$(mktemp -d)
  rm -rf "$tmp"
  real_origin="https://github.com/$(git remote get-url origin | sed 's|.*github.com[:/]||')"
  git clone --no-hardlinks . "$tmp"
  git -C "$tmp" remote set-url origin "$real_origin"
  git -C "$tmp" checkout "$HASH"
  mv "$tmp" "$DEST/$HASH"
  COPYFILE_DISABLE=1 tar czf "$DEST/current.tar.gz" -C "$DEST/$HASH" .
  # Promote the tree's contents (including dotfiles like .git, .mngr) up to $DEST so it works
  # as a Modal/Docker build context, then drop the now-empty $HASH dir.
  (
    cd "$DEST/$HASH"
    shopt -s dotglob nullglob
    mv -- * "$DEST/"
  )
  rmdir "$DEST/$HASH"
  touch "$DEST/$HASH.checkpoint"
fi
