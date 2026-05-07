#!/bin/bash
set -euo pipefail

# Re-install Python dependencies and CLI tools after repo contents change.
# Used in two places -- keep them in sync by using THIS script in both:
#   1. Dockerfile: final RUN before CMD
#   2. offload configs: post_patch_cmd (runs after thin-diff patch)
cd /code/mngr/
unset UV_INDEX_URL
uv sync --all-packages
uv tool install -e /code/mngr/libs/mngr \
    --with-editable /code/mngr/libs/mngr_modal \
    --with-editable /code/mngr/libs/mngr_schedule \
    --with-editable /code/mngr/libs/mngr_claude
uv tool install modal
