# NixOS Docker Workspace Design

## Purpose

Minds needs a Docker workspace path that can evolve toward a confidential computing sandbox. The current Debian-based Docker workspace is useful and well understood, but it is not the best long-term foundation for an attestable, reproducible, and minimal workspace environment.

This document records the Docker/NixOS workspace design choice: what strategies were considered, why the NixOS/Nix-built route was selected, and what constraints follow from the current implementation in forever-claude-template.

## Background

The existing Docker workspace path is built from forever-claude-template's root `Dockerfile`. That image behaves like a Debian system:

- package installation assumes `apt` and `dpkg`
- system libraries are available through conventional FHS paths such as `/usr/lib` and `/lib64`
- OpenSSH, SFTP, supervisor, Playwright, Node, uv, Claude Code, and Python packages are installed through a mix of system packages, shell scripts, language package managers, and downloaded installers
- the Docker provider boots a container, connects over SSH, copies workspace state with SFTP, and starts the workspace services

The Docker/NixOS path preserves parity with the existing Debian-based workspace while moving the system toolchain toward a more declarative and auditable runtime. The goal is to make builds easier to reproduce, inspect, and reason about, creating a foundation that can later evolve into confidential-computing-capable workspace builds.

## Strategies Considered

### Strategy 1: Debian Base with Nix-Managed Tools

This strategy keeps the existing Debian image and uses Nix to install or pin selected tools.

What it buys us:

- lowest migration risk
- fewer changes to the Docker provider, SSH/SFTP setup, Playwright, Claude Code, and existing install scripts
- some reduction in tool drift by pinning more of the toolchain through Nix

Why not choose it:

- Debian remains the real base system
- Nix becomes an extra package source rather than the source of truth
- the final runtime still depends on broad Debian behavior and imperative setup

### Strategy 2: NixOS/Nix-Built Image with Explicit Compatibility Layer

This strategy builds the workspace image from a Nix-first base and declares the system toolchain through Nix. Compatibility with normal Linux paths and services is added only where the workspace needs it.

What it buys us:

- a real Nix-built system package layer pinned by `flake.lock`
- clearer visibility into which Linux compatibility paths the workspace actually needs
- a runtime that is easier to rebuild, inspect, and test than the Debian path
- a practical migration path that keeps existing workspace behavior working

This is the selected route for the Docker/NixOS workspace path.

What it costs:

- some Debian assumptions have to be recreated explicitly
- Playwright/Chromium, Claude Code, OpenSSH, npm globals, uv tools, native Python packages, SSH, and SFTP all need compatibility checks
- the first version has more setup plumbing than the Debian image

### Strategy 3: Fully Nix-Native Workspace with No FHS Compatibility Surface

This strategy packages the workspace as fully Nix-native software and avoids broad compatibility shims.

What it buys us:

- the cleanest long-term runtime
- fewer mutable install steps
- a smaller and more auditable dependency closure
- the strongest eventual fit for reproducible and attestable workspace builds

Why not choose it now:

- the current workspace still depends on external installers, authenticated tools, browser automation, npm/PyPI packages, and Docker provider SSH/SFTP behavior
- solving all of that in one step would turn this work into a much larger packaging migration
- it would delay proving whether the Docker/NixOS workspace path works end to end

## Decision

Use Strategy 2: a NixOS/Nix-built Docker workspace image with a small, explicit compatibility layer.

This gives Minds a real Nix-built workspace path while keeping the existing Docker workspace contract working. It avoids two weaker extremes: leaving Debian as the real base system, or forcing every tool and service to become fully Nix-native before we can validate the path.

## Why This Choice

This path gives us the most useful next step:

- the workspace still boots through the same Docker provider flow
- system packages come from a pinned Nix package set
- compatibility needs are visible instead of hidden inside the Debian base image
- each compatibility bridge can be covered by an image or provider contract test
- the result is easier to rebuild, inspect, and eventually measure for confidential-computing flows

The main tradeoff is that some Linux distribution behavior has to be recreated deliberately. OpenSSH, SFTP, certificates, dynamic linker paths, shared libraries, fontconfig, Playwright, npm globals, uv tools, and native Python dependencies all expect normal Linux paths. Making those expectations explicit is extra work, but it turns them into a documented contract instead of ambient behavior.

Strategy 2 keeps the migration practical now and still leaves room to shrink the compatibility layer over time.

## Current Implementation

The Docker/NixOS implementation currently lives in forever-claude-template and is selected by the `docker-nixos` create template.

The current implementation pieces are:

- `nix/Dockerfile`: the NixOS Dockerfile. It keeps the Docker build context at the forever-claude-template repo root while using a filename IDEs recognize as a Dockerfile.
- `scripts/setup_system_nixos.sh`: the NixOS-specific parallel to `scripts/setup_system.sh`. It builds the Nix profile, verifies the closure manifest, creates compatibility paths, configures SSH/SFTP, installs uv and Claude Code, seeds GitHub host keys, and installs global tools.
- `flake.nix` and `flake.lock`: the Nix package set declaration and lock file. The flake tracks stable `nixos-26.05`; the current locked nixpkgs rev is `34268251cf5547d39063f2c5ea9a196246f7f3a6` with nar hash `sha256-G3tw/IXmaH6IQ2upZvhuN9sG8CkuX+BLuJDpE8hz0Ds=`.
- `nix/fct-workspace-env.nix`: the declared system package environment.
- `nix/fct-workspace-closure.aarch64-linux.txt`: the checked-in Nix closure manifest for the currently verified Apple Silicon/Docker Desktop path.
- `scripts/generate_nix_closure_manifest.sh`: the intentional regeneration path for closure manifests.
- `test_docker_image_contract.py`: the opt-in heavyweight image contract test.
- `just minds-build-fct-nixos`: the mngr build gate that verifies the Nix profile closure and runs the heavyweight image contract against `nix/Dockerfile`.
- Minds UI launch mode `docker/nixos`: routes workspace creation through `--template main --template docker-nixos`.

The NixOS Dockerfile is intentionally separate from the root Debian `Dockerfile`. Normal Docker workspaces can continue using the Debian path while Docker/NixOS workspaces exercise the Nix-built image.

## Current Reproducibility Posture

The current Docker/NixOS path improves structure and visibility, pins the Nix package universe through a committed flake lock, pins the Docker base image by digest, and verifies the realized Nix system closure against a checked-in manifest during the image build. It is still not fully reproducible because some third-party installers are still fetched live.

Current state:

- **Base image:** digest-pinned. `nix/Dockerfile` uses `nixos/nix:2.34.7@sha256:bf1d938835ab96312f098fa6c2e9cab367728e0aad0646ee3e02a787c80d8fb8`, so the build resolves the same multi-platform image index instead of trusting tag movement.
- **Nix packages:** declared and lock-pinned. `flake.nix` defines the FCT workspace environment from stable `nixos-26.05`, and `flake.lock` pins the exact `nixpkgs` revision used to resolve packages such as OpenSSH, Python, Node 24, Git, supervisor, and browser runtime libraries.
- **Nix closure manifest:** checked in and build-verified for generated platforms. The Dockerfile builds the Nix profile, writes `/etc/fct-workspace/nix-closure.txt`, and the `fct-nix-profile` stage diffs it against `nix/fct-workspace-closure.<system>.txt` from the source tree. The currently checked-in manifest covers `aarch64-linux`; `x86_64-linux` should be added from a real x86 builder before x86 is treated as supported by the safe build path.
- **Manifest regeneration:** explicit. `scripts/generate_nix_closure_manifest.sh` builds the `fct-nix-profile-generate` target and copies `/etc/fct-workspace/nix-closure.txt` back into `nix/fct-workspace-closure.<system>.txt` for intentional updates:

  ```bash
  cd .external_worktrees/forever-claude-template
  scripts/generate_nix_closure_manifest.sh
  ```

  On a native x86 builder, the same command writes `nix/fct-workspace-closure.x86_64-linux.txt`. Use `FCT_DOCKER_PLATFORM=linux/amd64` only when the builder needs an explicit Docker platform override.
- **Claude Code:** version-pinned, but not hash-pinned. The image downloads `https://claude.ai/install.sh` and runs it for `CLAUDE_CODE_VERSION`, so the build still trusts the remote installer and artifact at build time.
- **uv:** version-pinned, but not hash-pinned. The image downloads the uv installer for `UV_VERSION`, but does not verify the installer or release artifact hash.
- **npm and PyPI tools:** version-pinned, but registry-dependent. `latchkey` and `modal` versions are specified, but install still depends on registry resolution unless backed by lockfiles, hashes, or an internal artifact mirror.
- **Workspace code:** copied from the Docker build context. Its reproducibility depends on the exact source tree used for the build.

That means the current implementation is better described as:

```text
Base image:        digest-pinned
Nix packages:      declared and nixpkgs-locked through flake.lock
Nix closure:       compared against checked-in platform manifest
Claude Code:       version-pinned, installer/artifact not hash-pinned
uv:                version-pinned, installer not hash-pinned
npm/PyPI tools:    version-pinned, registry-dependent
workspace code:    copied from build context
```

This is useful because a large part of the system package surface now resolves through a deterministic Nix input and is compared against a reviewed closure manifest. The remaining non-reproducible pieces are explicit trust boundaries.

Only `aarch64-linux` has been checked in so far. The attempted Docker Desktop amd64-emulated generation failed in the Nix build sandbox, so `x86_64-linux` remains unsupported until generated and reviewed from a suitable x86 builder.

## Safe Build Gate

The local gate for the current implementation is:

```bash
just minds-build-fct-nixos
```

The recipe is intentionally heavyweight and opt-in. It is the local validation path for the FCT Docker/NixOS image, not part of the normal Minds packaging build.

By default, it uses the repos2 FCT worktree at `.external_worktrees/forever-claude-template`. It also accepts an explicit FCT checkout path and an optional output image tag:

```bash
just minds-build-fct-nixos .external_worktrees/forever-claude-template fct-nixos-contract:local
```

The recipe performs two checks:

1. Enters the repos2 forever-claude-template worktree by default.
2. Confirms that `nix/Dockerfile` exists.
3. Builds `nix/Dockerfile` to the `fct-nix-profile` target and tags that intermediate image as `fct-nixos-profile-verify:local` unless `FCT_NIX_PROFILE_IMAGE_TAG` is set.
4. Verifies the checked-in Nix closure manifest during that target build. If the realized Nix closure differs from `nix/fct-workspace-closure.<system>.txt`, the Docker build fails before the full workspace image is built.
5. Runs the heavyweight Docker/NixOS image contract test against the full workspace image with `FCT_DOCKER_IMAGE_CONTRACT=1`, `FCT_DOCKERFILE=nix/Dockerfile`, and `FCT_DOCKER_IMAGE_TAG` set to the recipe's `tag` argument.

This is deliberately separate from normal `just minds-build`. The normal Minds build packages application code and FCT state for runtime workspace creation; it does not build and contract-test the FCT Docker/NixOS image. `just minds-build-fct-nixos` is the explicit safe-build gate for the Docker/NixOS path: it proves the closure manifest still matches and that the resulting image still satisfies the runtime contract.

## Maintainer Workflow

Common maintenance tasks should keep dependency changes explicit and reviewable.

### Verify the Current Image

Use the safe-build gate when changing the Docker/NixOS image, Nix package set, compatibility shims, or workspace boot contract:

```bash
just minds-build-fct-nixos
```

This verifies the checked-in closure manifest and runs the heavyweight image contract test. It should pass before merging Docker/NixOS image changes.

### Update Nix-Managed System Packages

Use this process when changing `flake.lock` or `nix/fct-workspace-env.nix`:

1. Make the intended Nix package change in the FCT worktree.
2. Regenerate the closure manifest:

   ```bash
   cd .external_worktrees/forever-claude-template
   scripts/generate_nix_closure_manifest.sh
   ```

3. Review the `nix/fct-workspace-closure.<system>.txt` diff. The new or removed store paths should match the intended package change.
4. Run the safe-build gate from the mngr repo:

   ```bash
   just minds-build-fct-nixos
   ```

5. Commit the package change, lockfile change, and manifest update together.

### Add x86 Closure Support

Generate `nix/fct-workspace-closure.x86_64-linux.txt` on a suitable x86 builder:

```bash
cd .external_worktrees/forever-claude-template
scripts/generate_nix_closure_manifest.sh
```

Use `FCT_DOCKER_PLATFORM=linux/amd64` only when the builder needs an explicit Docker platform override. After generating the manifest, review it and run `just minds-build-fct-nixos` on x86 before treating x86 as supported.

### Update Non-Nix Tool Versions

Tools such as Claude Code, uv, latchkey, modal, npm packages, and PyPI packages are currently version-pinned but still depend on live installers or registries. When updating them:

1. Change the pinned version in the relevant setup file.
2. Run `just minds-build-fct-nixos`.
3. Review any changed lockfiles, generated assets, or contract-test behavior.
4. Treat the live installer or registry as an explicit trust boundary until that dependency is moved to a fixed-hash or mirrored source.

### Debug a Closure Mismatch

If `just minds-build-fct-nixos` fails during closure verification, do not update the manifest automatically. First decide whether the closure changed for an expected reason:

- if the change is intentional, regenerate the manifest, review the diff, and commit it with the dependency change
- if the change is unexpected, inspect `flake.lock`, `nix/fct-workspace-env.nix`, and any Dockerfile/setup-script changes before accepting it

## Design Principles

The Docker/NixOS path should follow these principles:

1. The image should be Nix-first. Debian compatibility should be added only when required.
2. Compatibility shims should be explicit and tested.
3. The Docker/NixOS path should remain separate from the existing Docker path until it is proven stable.
4. The workspace contract should be described in terms of observable behavior, not distro identity.
5. Mutable install steps should be reduced over time, but not all at once.
6. The design should keep confidential computing in mind: fewer ambient dependencies, clearer provenance, and an easier attestation story.

## Consequences

Choosing this route means the first Docker/NixOS image needs compatibility work that a Debian image gets for free:

- dynamic linker compatibility for downloaded Linux binaries
- shared-library availability for browser automation and native Python packages
- fontconfig and certificate paths for Playwright/Chromium and HTTPS tools
- OpenSSH and SFTP layout expected by the Docker provider
- environment propagation for SSH sessions
- replacement or fallback behavior for scripts that assume `apt` or `dpkg`
- careful testing of services that depend on browser, Python, Node, git, HTTPS, or terminal behavior

These are acceptable costs because they expose the true workspace contract.

## Future Direction

Near-term work should keep the current safe-build path honest:

1. Update `flake.lock` intentionally, not as a side effect of normal builds.
2. Regenerate closure manifests only when `flake.lock` or `nix/fct-workspace-env.nix` changes intentionally, then review and commit the diff.
3. Generate and check in `nix/fct-workspace-closure.x86_64-linux.txt` from a suitable x86 builder before treating x86 as supported.
4. Keep `just minds-build-fct-nixos` as a verification gate, not a manifest generator.

Hardening work can then reduce the remaining live dependencies:

1. Replace curl-based installers with Nix derivations or fixed-hash downloads where practical.
2. Move npm and PyPI installs toward lockfile-backed, hash-backed, or internally mirrored installs.
3. Produce an SBOM or image manifest for the final workspace image.
4. Sign the built image.
5. Use the signed image digest or expected runtime measurement in the confidential sandbox attestation policy.

For external tools such as Claude Code, the strongest end state is an approved artifact represented as a Nix package or fixed-output derivation. Until then, the image can be compatible and useful, but live installers remain explicit trust boundaries.

## Not Decided Here

This document does not decide:

- which confidential VM runtime Minds should use
- how remote attestation policies are represented
- how secrets are released after attestation
- whether Lima should boot a full NixOS guest or use a Nix-managed guest environment
- how much of forever-claude-template should become fully Nix-native

Those decisions should build on the Docker/NixOS workspace contract once the basic runtime path is reliable.
