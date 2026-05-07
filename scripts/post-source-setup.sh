#!/usr/bin/env bash
#
# Source-dependent setup for the mngr Docker image.
#
# This script is invoked from TWO places and the two MUST stay in sync:
#
#   1. As the final RUN step in libs/mngr/imbue/mngr/resources/Dockerfile.
#   2. As `post_patch_cmd` in offload-modal*.toml, where offload runs it
#      after applying the thin diff to its cached checkpoint image (or
#      after a full build fallback).
#
# Adding any other RUN step below the script call in the Dockerfile
# causes drift between the Dockerfile-build path and offload's
# post-patch path. Don't add another RUN below the script invocation.
#
# The script is idempotent: each step is guarded so running it multiple
# times leaves the same image state.

set -euo pipefail

CODE_DIR="${CODE_DIR:-/code/mngr}"
cd "$CODE_DIR"

# Step 1: extract current.tar.gz keyframe if present.
#
# The typical end-user flow (.mngr/settings.toml's pre_command_scripts
# hook for `mngr create`) ships a tarball produced by
# scripts/make_tar_of_repo.sh as the Modal build context. Other paths
# (offload, the test_modal_create.py release test, mngr_schedule's
# deploy) hand off a real source tree and this branch is a no-op.
if [ -f current.tar.gz ]; then
    echo "Extracting $CODE_DIR/current.tar.gz"
    tar xzf current.tar.gz
    rm -f current.tar.gz *.checkpoint
fi

# Step 2: normalize .git so downstream tooling (git rev-parse, ratchet
# tests, mngr CLI repo discovery) finds a real in-image git directory.
# Three input shapes:
#   (a) .git is a *file* (worktree pointer): drop it and re-init.
#       Worktree pointers reference paths on the host that don't exist
#       inside the sandbox.
#   (b) .git is missing entirely (e.g. tarball produced by `git archive`,
#       or offload's export_tree which does git init + fetch but
#       skipped here): init a fresh repo and commit.
#   (c) .git is already a directory: leave alone.
if [ -f .git ]; then
    echo "Normalizing worktree-style .git -> fresh in-image .git"
    rm .git
    git init -q .
    git add -A
    git -c user.email=ci@local -c user.name=ci commit -q -m 'sandbox-init'
elif [ ! -d .git ]; then
    echo "No .git at all -> fresh in-image .git"
    git init -q .
    git add -A
    git -c user.email=ci@local -c user.name=ci commit -q -m 'sandbox-init'
fi

# Step 3: ensure origin remote is registered. The URL is only used to
# derive a github.com base for packaged tarballs in mngr_schedule
# release tests; the precise string doesn't matter, just that it
# parses as a github.com URL.
git remote get-url origin >/dev/null 2>&1 \
    || git remote add origin https://github.com/imbue-ai/mngr.git

# Step 4: write image_commit_hash for downstream tooling.
mkdir -p .mngr
git rev-parse HEAD > .mngr/image_commit_hash

# Step 5: install Python dependencies.
# `uv sync --all-packages` is naturally idempotent (no-op when lockfile
# is satisfied). `uv tool install -e ...` overwrites cleanly on rerun.
unset UV_INDEX_URL
uv sync --all-packages
uv tool install -e "$CODE_DIR/libs/mngr" \
    --with-editable "$CODE_DIR/libs/mngr_modal" \
    --with-editable "$CODE_DIR/libs/mngr_schedule" \
    --with-editable "$CODE_DIR/libs/mngr_claude"
uv tool install modal
