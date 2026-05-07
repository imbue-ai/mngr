# Restore Modal Dockerfile + Adopt offload v0.9.4 post_patch_cmd

## Goal

The mngr Dockerfile at `libs/mngr/imbue/mngr/resources/Dockerfile` was
modified for offload-on-Modal compatibility (multi-stage build for the
offload binary, repo-wide `COPY .` + post-COPY git/uv setup). Two
regressions resulted:

1. **Modal provider is broken for end users.** `mngr create
   <agent>@.modal -b file=Dockerfile` (and the bundled default
   Dockerfile path) fails because mngr's Modal image builder rejects
   multi-stage Dockerfiles
   (`libs/mngr_modal/imbue/mngr_modal/instance.py:3277` —
   `assert not dfp.is_multistage`).
2. **Offload setup and "real" mngr setup have drifted.** Offload
   caches a checkpoint base image and applies a thin diff per run.
   Setup that depends on per-commit source files (git normalization,
   `image_commit_hash`, `uv sync --all-packages`, the editable
   `mngr` install) currently lives in Dockerfile RUN layers after
   `COPY . /code/mngr/`, so the checkpoint cache cannot include them
   correctly. Without a `post_patch_cmd` hook, every drift between
   "what offload re-runs" and "what the Dockerfile re-runs" produces
   environment-vs-test divergence.

The primary goal is to restore Modal compatibility for the standard
mngr user. The secondary goal is to centralize all per-source setup in
a single shell script that runs both as the last Dockerfile RUN step
and as `post_patch_cmd` under offload, with the script itself
referenced from one source of truth.

The work depends on offload v0.9.4 (introduces `post_patch_cmd`).
Upgrading offload from 0.9.2 to 0.9.4 is a prerequisite step.

## Current State

- Current pin: offload `0.9.2` in `libs/mngr/imbue/mngr/resources/Dockerfile:8`
  and `.github/workflows/ci.yml` (test-offload + test-offload-acceptance
  jobs, plus `cargo-offload-0.9.2` cache key).
- Dockerfile has two `FROM` directives
  (`libs/mngr/imbue/mngr/resources/Dockerfile:7,11`) and uses
  `COPY . /code/mngr/` (line 87).
- Per-source setup currently runs as RUN layers below the COPY:
  git normalization + `image_commit_hash` write
  (`libs/mngr/imbue/mngr/resources/Dockerfile:119-134`), `WORKDIR` +
  `uv sync --all-packages` + editable mngr/mngr_modal/mngr_schedule/
  mngr_claude install + `uv tool install modal` (lines 138-141).
- mngr's modal provider rejects multi-stage Dockerfiles unconditionally
  (`libs/mngr_modal/imbue/mngr_modal/instance.py:3277` —
  `assert not dfp.is_multistage`).
- `.mngr/settings.toml` wires up the typical user flow:
  - `pre_command_scripts.create` runs
    `scripts/make_tar_of_repo.sh <image_commit_hash> .mngr/dev/build/`
    before each `mngr create`.
  - The `modal` create template sets
    `context-dir=.mngr/dev/build/` and `file=libs/mngr/imbue/mngr/resources/Dockerfile`.
  - Result: the Modal build context is a small directory containing
    just `current.tar.gz` (and a `.checkpoint` marker).
- Commit `f4b797b7b7` ("mngr_schedule: extract current.tar.gz
  producer-side") removed the Dockerfile's `if [ -f current.tar.gz ];
  then tar xzf` block. After that commit, all callers were expected to
  extract producer-side and hand off a real source tree as
  `context_dir`. The corresponding test (`test_mngr_create_with_default_dockerfile_on_modal`)
  was updated to do exactly that. **The `.mngr/settings.toml`
  pre_command_scripts hook was NOT updated** — it still ships the
  tarball, so the typical user flow is broken in two ways:
  1. The multistage assertion fires (the immediate visible failure).
  2. Even with multistage fixed, `COPY . /code/mngr/` lands a tarball,
     not source, and downstream RUN steps (git rev-parse, uv sync)
     would fail.
- The `mngr_schedule` deploy path uses a Python helper
  `unpack_current_tarball_in_place(dest_dir)` (in
  `libs/mngr_schedule/imbue/mngr_schedule/implementations/modal/deploy.py`)
  to extract producer-side.

## Reproducer (the test we iterate against)

The mngr-on-Modal invocation that we expect to work and currently
does not:

```
uv run mngr create test-modal-dockerfile@.modal -b file=libs/mngr/imbue/mngr/resources/Dockerfile
```

(equivalent to the existing release test
`libs/mngr_modal/imbue/mngr_modal/test_modal_create.py::test_mngr_create_with_default_dockerfile_on_modal`).

We expect this to fail today with an `AssertionError: Multistage
Dockerfiles are not supported yet` from
`_build_image_from_dockerfile_contents`. Success criterion: the
command builds the image, launches the sandbox, and reaches `Done.`.

## Plan

### Step 1: Upgrade offload pin from 0.9.2 to 0.9.4

- Bump `OFFLOAD_VERSION=0.9.2` -> `0.9.4` in
  `libs/mngr/imbue/mngr/resources/Dockerfile:8`.
- Bump `cargo install offload@0.9.2` -> `0.9.4` in
  `.github/workflows/ci.yml` (both the test-offload and
  test-offload-acceptance jobs, plus the cache key
  `cargo-offload-0.9.2` -> `cargo-offload-0.9.4` and the version
  check `offload --version | grep -q '0.9.2'` -> `'0.9.4'`).
- Verify `just test-offload` runs green on a no-op branch with the
  bump alone (warm cache; no functional changes yet).

### Step 2: Identify the Dockerfile setup work that depends on per-source files

The "post-source-touch" work currently lives across these blocks of
`libs/mngr/imbue/mngr/resources/Dockerfile`:

- L93 (`git config --system --add safe.directory '*'`) — system-wide,
  source-independent. Stays in the base image.
- L119-134 (git normalization, `image_commit_hash` write) —
  source-dependent. Moves into the new script.
- L138 (`WORKDIR /code/mngr/`) — Dockerfile-only. Stays.
- L141 (`uv sync --all-packages` + editable installs) —
  source-dependent. Moves into the new script.

The script (Step 5) also takes ownership of `current.tar.gz`
extraction so that the contract "build context is either a real
source tree or a `current.tar.gz` keyframe" is enforced in one
place.

### Step 3: Make the Dockerfile single-stage

Decision: collapse to a single `FROM python:3.12-slim` and install
the offload binary in the same stage via `cargo install
offload@${OFFLOAD_VERSION} --locked --root /opt/offload`. Drop the
`FROM rust:1-bookworm AS offload-builder` block.

Trade-offs accepted:
- Larger image during build (rust toolchain installed alongside
  Python). Acceptable because:
  - Image-build time is dominated by `uv sync`, not by rust install.
  - The rust toolchain layer caches; subsequent builds with the same
    `OFFLOAD_VERSION` skip it.
- Keeps mngr's modal provider unchanged (no need to teach
  `_build_image_from_dockerfile_contents` about multistage).
- Matches what the original Dockerfile likely did pre-offload-binary.

If image size becomes a problem later, switch to fetching the offload
release asset (no rust toolchain needed); not in scope for this plan.

### Step 4: Disposition of `COPY . /code/mngr/`

Decision: **restore the keyframe workflow**, but route extraction
through the new shared post-source-setup script (Step 5) so there is a
single normalization point.

Why restore now (not defer or reject):

- The typical user flow (`.mngr/settings.toml`'s `pre_command_scripts`
  + `create_templates.modal`) already produces a tarball-based
  `context-dir=.mngr/dev/build/` and is currently broken downstream of
  multistage. Fixing multistage without addressing the tarball
  contract leaves the typical user flow still broken.
- The infrastructure (`make_tar_of_repo.sh`, `unpack_current_tarball_in_place`)
  already exists and is well-tested.
- Layer-cache benefit on Modal is real and substantial when the
  keyframe commit doesn't change between builds: the entire
  source-COPY + post-source layer cache hits, saving the full
  `uv sync --all-packages` step (~30-90s).
- Restoring the in-image extraction codifies the contract:
  "the build context can be either a real source tree OR a
  `current.tar.gz` keyframe". Both producers (offload, mngr_schedule,
  the test, the typical user flow) work without coordination.

Concretely:

- The post-source-setup script (Step 5) is the single place that
  detects `current.tar.gz` and extracts it before doing any
  source-dependent work.
- `COPY . /code/mngr/` stays as-is in the Dockerfile. With
  `context-dir=.mngr/dev/build/`, that's a tiny COPY (one tarball);
  with `context-dir=<real source tree>`, that's a regular source COPY.
- The `.mngr/settings.toml` pre_command_scripts hook stays as-is
  (just runs `make_tar_of_repo.sh`). Producer-side extraction
  becomes optional, not required.

### Step 5: Introduce `scripts/post-source-setup.sh`

Behavior of `scripts/post-source-setup.sh`:

1. If `/code/mngr/current.tar.gz` exists, extract it in place and
   delete the tarball + any `.checkpoint` markers (mirrors
   `unpack_current_tarball_in_place`).
2. Normalize `/code/mngr/.git`:
   - If `.git` is a *file* (worktree pointer), drop it and re-init.
   - If `.git` is missing, init a fresh repo and commit.
   - If `.git` is a directory, leave it alone.
3. Ensure `origin` remote points at `https://github.com/imbue-ai/mngr.git`
   (only if missing).
4. Write `git rev-parse HEAD` to `.mngr/image_commit_hash`.
5. Run `uv sync --all-packages` and the editable
   `mngr`/`mngr_modal`/`mngr_schedule`/`mngr_claude` installs +
   `uv tool install modal`.

The script is idempotent through per-step guards (no `.done` marker
file): tarball extraction runs only when `current.tar.gz` is present,
`git init` only when `.git` is missing, `origin` remote added only
when missing, `uv sync` is naturally idempotent. Running the script
twice (e.g. once in the Dockerfile RUN and again in offload's
`full_build_fallback` `post_patch_cmd`) leaves the same image state.

The Dockerfile's last `RUN` line invokes this script and is
annotated:

```dockerfile
# The single allowed post-COPY RUN. All source-dependent setup
# MUST live in scripts/post-source-setup.sh. This script also runs as offload's
# post_patch_cmd in offload-modal*.toml. Adding additional RUN
# lines below this point causes drift between offload's
# post-patch step and a from-scratch Dockerfile build.
RUN bash scripts/post-source-setup.sh
```

`offload-modal.toml`, `offload-modal-acceptance.toml`, and
`offload-modal-release.toml` gain `post_patch_cmd = "bash scripts/post-source-setup.sh"`
under `[offload]`.

### Step 6: Verify both code paths succeed

- **Modal user path** (the reproducer in this plan): `uv run mngr
  create test-modal-dockerfile@.modal -b file=libs/mngr/imbue/mngr/resources/Dockerfile`
  must reach `Done.` and produce a working sandbox.
- **Offload path**: `just test-offload` must run green from a cold
  cache and from a warm cache.
- **Acceptance**: the existing release test
  `test_mngr_create_with_default_dockerfile_on_modal` and the related
  `test_mngr_create_with_dockerfile_on_modal` must pass.

### Step 7: Document drift prevention

- Add an explanatory comment in `scripts/post-source-setup.sh`
  describing the dual-call invariant (Dockerfile RUN + offload
  `post_patch_cmd`) and forbidding any other RUN lines below
  the script call in the Dockerfile.
- Mention the script in `libs/mngr/README.md` (or wherever the
  Dockerfile is referenced) so end users know it's part of the
  standard image build.

## Constraints

- The default Dockerfile must work for the standard end-user
  invocation (`mngr create <agent>@.modal -b file=...`) without any
  offload tooling involvement.
- All three build-context shapes must continue to produce a working
  image:
  - Real source tree (offload's exported repo, the test's unpacked
    tree, local docker builds).
  - `.mngr/dev/build/` directory containing only `current.tar.gz`
    (typical user flow via `.mngr/settings.toml`).
  - `mngr_schedule`'s producer-side-extracted source tree.
- Source-dependent setup must run identically in the Dockerfile RUN
  step and as offload's `post_patch_cmd`. Adding a second
  RUN step below the script call is forbidden by comment.
- Code must continue to work on macOS and Linux.
- No emojis anywhere in spec or code.

## Commit boundaries (one PR, multiple atomic commits)

Land as one PR with atomic commits in this order:

1. Bump offload `0.9.2` -> `0.9.4` in
   `libs/mngr/imbue/mngr/resources/Dockerfile` and
   `.github/workflows/ci.yml`. Verify CI green (no functional
   change yet — `post_patch_cmd` is supported but not used).
2. Drop the `FROM rust:1-bookworm AS offload-builder` block;
   install offload via `cargo install offload@${OFFLOAD_VERSION}
   --locked --root /opt/offload` in the single Python stage. Verify
   the reproducer (`uv run mngr create
   test-modal-dockerfile@.modal -b file=...`) reaches `Done.`.
3. Add `scripts/post-source-setup.sh` and move into it: tarball
   extraction, git normalization, `image_commit_hash` write,
   `uv sync --all-packages`, editable installs, and
   `uv tool install modal`. Replace the corresponding Dockerfile
   RUN blocks with a single `RUN bash scripts/post-source-setup.sh`,
   annotated with the no-other-RUN-lines comment.
4. Wire `post_patch_cmd = "bash scripts/post-source-setup.sh"`
   into `offload-modal.toml`, `offload-modal-acceptance.toml`,
   and `offload-modal-release.toml`. Verify `just test-offload`
   still green.

## Open Questions

- After Step 1 (offload bump), should we capture before/after CI
  timings (à la `specs/offload-v0.9.0-history/plan.md`)? Probably
  unnecessary since 0.9.2 -> 0.9.4 is a small bump, but worth
  noting if any timing regression appears.
- The mngr_schedule path (`unpack_current_tarball_in_place`) is now
  redundant with the in-script extraction in
  `scripts/post-source-setup.sh`. Whether to keep it or remove it
  is a follow-up cleanup, not in scope here. Documented for the
  follow-up.
