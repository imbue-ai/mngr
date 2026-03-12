#!/usr/bin/env bash

set -euo pipefail

# Go to the root of the repo
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
cd ../..

mkdir -p .git/hooks

ln -sf ../../scripts/githooks/pre-commit .git/hooks/pre-commit
