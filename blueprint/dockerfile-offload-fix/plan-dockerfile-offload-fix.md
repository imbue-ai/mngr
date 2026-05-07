# Fix Dockerfile multi-stage build and add offload post_patch_cmd

## Overview

- The Dockerfile gained a multi-stage build (`FROM rust:1-bookworm AS offload-builder`) to compile the offload binary. This breaks mngr's Modal provider, which asserts `not dfp.is_multistage` at `instance.py:3277`. Modal as a provider is completely non-functional.
- The fix is to remove the multi-stage build and install Rust + cargo in the single stage, compile offload, then clean up the Rust toolchain to limit image bloat.
- Offload v0.9.4 introduces `post_patch_cmd`, which runs after thin-diff patches are applied. This keeps derived-artifact setup (dependency install) in sync between Dockerfile builds and offload's checkpoint+thin-diff flow.
- A shared `scripts/post_file_setup.sh` script will be the single source of truth for post-COPY setup steps, used by both the Dockerfile's last RUN and offload's `post_patch_cmd`.
- The `COPY . /code/mngr/` layer invalidation concern (raised by Jacob) is intentionally not addressed: Modal's cache key is content-based regardless of format, and offload's checkpoint cache handles the hot path. The tarball approach would not improve caching.

## Expected behavior

- `uv run mngr create @.modal -b --file=libs/mngr/imbue/mngr/resources/Dockerfile -b context-dir=.` succeeds (currently fails with `AssertionError: Multistage Dockerfiles are not supported yet`)
- The release test `test_mngr_create_with_default_dockerfile_on_modal` passes
- `just test-offload` continues to work, now using offload 0.9.4 with `post_patch_cmd`
- `just test-offload-acceptance` and `just test-offload-release` continue to work with `post_patch_cmd`
- After offload applies a thin-diff, `post_file_setup.sh` re-runs `uv sync` and `uv tool install` to pick up any dependency changes not captured by the checkpoint
- The generated `Dockerfile.release` (base + extras) continues to build correctly -- the `_generate-release-dockerfile` justfile recipe strips CMD and appends extras, which should compose cleanly with the refactored base
- Image size remains reasonable: Rust toolchain is installed and removed in a single RUN layer, so only the compiled offload binary (~15MB) persists

## Implementation plan

### New file: `scripts/post_file_setup.sh`

- Shell script containing only the dependency-installation steps that depend on repo contents:
  - `unset UV_INDEX_URL && uv sync --all-packages`
  - `uv tool install -e /code/mngr/libs/mngr --with-editable /code/mngr/libs/mngr_modal --with-editable /code/mngr/libs/mngr_schedule --with-editable /code/mngr/libs/mngr_claude && uv tool install modal`
- Must be idempotent: `uv sync` is naturally idempotent (no-ops when deps are unchanged)
- Set `#!/bin/bash` and `set -euo pipefail`
- Working directory is assumed to be `/code/mngr/` (the WORKDIR in the Dockerfile)

### Modified: `libs/mngr/imbue/mngr/resources/Dockerfile`

- Remove the `FROM rust:1-bookworm AS offload-builder` stage (lines 1-9)
- Remove `COPY --from=offload-builder /opt/offload/bin/offload /usr/local/bin/offload` (line 80)
- Add a single RUN layer that: installs Rust via rustup, runs `cargo install offload@0.9.4 --locked --root /opt/offload`, copies the binary to `/usr/local/bin/offload`, then removes the Rust toolchain (`rm -rf /root/.cargo /root/.rustup`). All in one RUN to avoid layer bloat.
- Update `ARG OFFLOAD_VERSION=0.9.2` to `ARG OFFLOAD_VERSION=0.9.4`
- Git normalization block (lines 119-134) stays as a Dockerfile RUN -- offload's thin-diff does not change `.git`, so this only needs to run at image build time
- `git config --system --add safe.directory '*'` stays as a Dockerfile RUN -- system-level config, not repo-content-dependent
- Replace the final `RUN unset UV_INDEX_URL && uv sync ...` (line 141) with `COPY scripts/post_file_setup.sh /usr/local/bin/post_file_setup.sh` + `RUN chmod +x /usr/local/bin/post_file_setup.sh && /usr/local/bin/post_file_setup.sh`
- Add a comment on the `post_file_setup.sh` RUN: this must be the last RUN before CMD; offload's `post_patch_cmd` runs the same script to keep environments in sync

### Modified: `offload-modal.toml`

- Add `post_patch_cmd = "/usr/local/bin/post_file_setup.sh"` under the `[offload]` section

### Modified: `offload-modal-acceptance.toml`

- Add `post_patch_cmd = "/usr/local/bin/post_file_setup.sh"` under the `[offload]` section

### Modified: `offload-modal-release.toml`

- Add `post_patch_cmd = "/usr/local/bin/post_file_setup.sh"` under the `[offload]` section

### Modified: `.github/workflows/ci.yml`

- Update 4 occurrences of `0.9.2` to `0.9.4` in the `test-offload` job:
  - Cache key: `cargo-offload-0.9.2-${{ runner.os }}` -> `cargo-offload-0.9.4-${{ runner.os }}` (line 60)
  - Version check grep: `grep -q '0.9.2'` -> `grep -q '0.9.4'` (line 64)
  - Cargo install: `offload@0.9.2` -> `offload@0.9.4` (line 65)
- Update same 3 patterns in the `test-offload-acceptance` job:
  - Cache key (line 258)
  - Version check grep (line 262)
  - Cargo install (line 263)

## Implementation phases

### Phase 1: Create `post_file_setup.sh` and refactor Dockerfile

- Create `scripts/post_file_setup.sh` with the dependency install commands
- Remove the multi-stage build from the Dockerfile
- Add single-stage Rust install + cleanup RUN
- Replace the final dependency-install RUN with the `post_file_setup.sh` invocation
- Validate: the Dockerfile should parse as single-stage (no assertion failure)

### Phase 2: Upgrade offload to v0.9.4

- Update the `OFFLOAD_VERSION` ARG in the Dockerfile from `0.9.2` to `0.9.4`
- Update all 7 version references in `.github/workflows/ci.yml`
- Add `post_patch_cmd` to all three offload TOML configs

### Phase 3: Validate

- Verify `Dockerfile.release` generation still works: `just _generate-release-dockerfile` and inspect the output
- Attempt `uv run mngr create @.modal -b --file=libs/mngr/imbue/mngr/resources/Dockerfile -b context-dir=.` (requires Modal credentials)
- Run `just test-offload` to validate the full pipeline

## Testing strategy

- **Dockerfile parsing**: The existing release test `test_mngr_create_with_default_dockerfile_on_modal` exercises the exact broken path. After removing the multi-stage build, this test should pass.
- **Offload pipeline**: `just test-offload` validates the full unit + integration test pipeline with the new `post_patch_cmd` and offload 0.9.4.
- **Release Dockerfile composition**: After the changes, run `just _generate-release-dockerfile` and verify:
  - The generated `Dockerfile.release` is single-stage
  - It contains the `post_file_setup.sh` COPY and RUN
  - The `Dockerfile.release.extras` content is appended after the base (minus CMD)
  - **This is a validation step that must be performed after implementation** -- `Dockerfile.release.extras` was not modified, but the composition with the refactored base must be verified
- **Idempotency**: `post_file_setup.sh` running twice in offload (base image + post thin-diff) should be safe since `uv sync` is a no-op when nothing changed. Offload's test suite will exercise this path implicitly.
- **CI**: The CI workflow changes are mechanical version bumps. CI runs will validate that offload 0.9.4 installs and runs correctly on GitHub Actions runners.

## Open questions

- **Rust compilation time**: Installing Rust + compiling offload from source in the Dockerfile adds ~3-5 minutes to image build time (vs. the multi-stage approach which cached the builder stage independently). This is acceptable because checkpoint caching means the Dockerfile only rebuilds when `build_inputs` change, but it's worth monitoring. If build times become a pain point, pre-building the offload binary and hosting it as a downloadable artifact is the escape hatch.
- **`Dockerfile.release` composition**: The `_generate-release-dockerfile` recipe strips CMD and appends extras. This should work with the refactored base, but must be validated post-implementation. If the `post_file_setup.sh` RUN or COPY ends up in an unexpected position relative to the extras content, the release Dockerfile may need adjustment.
- **offload 0.9.4 availability**: Confirm that offload 0.9.4 is published to crates.io before starting. If not, coordinate with the offload repo to publish first.
