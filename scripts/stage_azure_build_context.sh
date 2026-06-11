#!/usr/bin/env bash

# Stage a self-contained docker build context for *remote* (VPS / Azure) image builds.
#
# The local `docker` and `modal` create templates reference the mngr Dockerfile by
# its in-repo path (`--file=libs/mngr/.../Dockerfile`) and point the build context at
# `.mngr/dev/build/` (which holds only the keyframe `current.tar.gz`). That works
# because their daemons resolve `--file` against a path they can already see.
#
# A VPS/Azure build is different: mngr uploads a *single* context directory to the VM
# and rewrites a relative `--file` to live inside it (see
# `mngr_vps_docker.container_setup.resolve_dockerfile_paths`). So the Dockerfile must
# physically sit at the root of the uploaded context. We assemble that here, in a
# directory separate from `.mngr/dev/build/` so the docker/modal contexts are
# untouched.
#
# The keyframe tarball is hardlinked (not copied) to avoid duplicating a large file.
set -euo pipefail

KEYFRAME_TARBALL="$1"   # e.g. .mngr/dev/build/current.tar.gz (produced by make_tar_of_repo.sh)
DEST="$2"               # e.g. .mngr/dev/azure-build
DOCKERFILE="$3"         # e.g. libs/mngr/imbue/mngr/resources/Dockerfile

mkdir -p "$DEST"

# Re-link every run so a regenerated keyframe (new inode at the same path) is picked up.
ln -f "$KEYFRAME_TARBALL" "$DEST/current.tar.gz"
cp "$DOCKERFILE" "$DEST/Dockerfile"

# Keep the Dockerfile and this ignore file out of the image's `COPY . /code/mngr/`
# (native `docker build` and `depot build` both honor .dockerignore). current.tar.gz
# is intentionally NOT ignored -- the Dockerfile extracts it to seed the source tree.
printf 'Dockerfile\n.dockerignore\n' > "$DEST/.dockerignore"
