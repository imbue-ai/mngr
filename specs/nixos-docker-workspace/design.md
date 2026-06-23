# NixOS Docker Workspace Design

## Purpose

Minds needs a Docker workspace path that can evolve toward a confidential computing sandbox. The current Debian-based Docker workspace is useful and well understood, but it is not the best long-term foundation for an attestable, reproducible, and minimal workspace environment.

This document records the Docker/NixOS workspace design choice: what strategies were considered, why the NixOS/Nix-built route was selected, and what constraints follow from the current implementation in forever-claude-template.

## Background

The existing Docker workspace path is built from forever-claude-template's root `Dockerfile`. That image behaves like a Debian system:

- package installation assumes `apt` and `dpkg`
- system libraries are available through conventional FHS paths such as `/usr/lib` and `/lib64`
- OpenSSH, SFTP, supervisor, Playwright, Node, uv, Claude Code, and Python packages are installed through a mix of system packages, shell scripts, language package managers, and downloaded installers
- the Docker provider expects to boot a container, connect over SSH, copy workspace state with SFTP, and start the workspace services

For the Docker/NixOS path, the goal is not only to make the workspace start. The deeper goal is to move the workspace toward a more declarative and auditable runtime that can support confidential computing.

Confidential computing changes the priority order. Compatibility still matters, but the environment must also become easier to measure, rebuild, reason about, and eventually attest before secrets are released to a workspace.

## Strategies Considered

### Strategy 1: Debian Base with Nix-Managed Tools

This strategy keeps the existing Debian container as the operating system shape and installs the workspace toolchain through Nix where practical.

In this model, Debian remains the compatibility layer:

- `/usr/bin`, `/usr/lib`, `/lib`, `/lib64`, `apt`, and `dpkg` continue to exist
- third-party binaries such as Claude Code and Playwright keep running in a familiar Linux userspace
- the Docker provider SSH/SFTP assumptions remain close to the current implementation
- Nix is used primarily to pin tools and reduce package drift

This is the lowest-risk migration path. It would likely reduce the amount of compatibility work needed for the existing workspace.

The downside is that it preserves the old OS contract. Debian remains the foundation, and Nix becomes an additional package-management layer rather than the source of truth for the workspace environment. That makes this strategy less compelling for confidential computing because the final environment still includes a broad mutable distro base with non-Nix provisioning assumptions.

This strategy is useful as a fallback or transitional experiment, but it is not the desired end state.

### Strategy 2: NixOS/Nix-Built Image with Explicit Compatibility Layer

This strategy builds the workspace image from a NixOS or Nix-first base and declares the system toolchain through Nix. Compatibility with FHS-expecting software is added intentionally, only where required.

In this model, the image is Nix-shaped first:

- system packages come from a pinned Nix package set
- workspace tools are declared rather than installed through ambient distro state
- Debian-specific behavior is treated as compatibility surface, not as the foundation
- FHS paths, dynamic linker paths, shared library paths, OpenSSH layout, SFTP layout, certificate paths, and fontconfig paths are provided deliberately where the workspace or third-party binaries require them

This is the selected route for the Docker/NixOS workspace path.

The main cost is that existing assumptions become visible. Tools such as Playwright/Chromium, Claude Code, OpenSSH, npm globals, uv-installed tools, and native Python dependencies expect normal Linux distribution paths. The Docker provider also expects SSH and SFTP behavior that a minimal Nix image does not provide automatically. Those assumptions must either be removed, wrapped, or made explicit in the image.

The benefit is that each compatibility bridge becomes part of the documented workspace contract. That is the right tradeoff for a future confidential sandbox: the environment becomes more declarative and easier to inspect, even if the first implementation requires more plumbing than the Debian path.

### Strategy 3: Fully Nix-Native Workspace with No FHS Compatibility Surface

This strategy goes further than Strategy 2. The workspace would be packaged as fully Nix-native software, and third-party tools would either be packaged through Nix or replaced with Nix-compatible equivalents. The image would avoid Debian and avoid broad FHS compatibility.

In this model:

- workspace services are packaged as Nix derivations
- Python, Node, browser, and CLI dependencies are built or wrapped by Nix
- mutable installation scripts are removed from the boot path
- downloaded binary installers are avoided or replaced with pinned derivations
- the runtime image contains only the closure needed to run the workspace

This is the cleanest long-term model for auditability and minimization.

It is not the right first implementation. The current workspace depends on external installers, subscription-authenticated tools, browser automation, npm/PyPI tools, and provider SSH/SFTP behavior. Forcing all of that to become fully Nix-native in one step would turn the prototype into a broad packaging migration and delay learning whether the NixOS workspace path is viable.

This strategy remains a future direction. Strategy 2 should be designed so that compatibility shims can shrink over time as more of the workspace becomes Nix-native.

## Decision

Use Strategy 2: a NixOS/Nix-built Docker workspace image with an explicit compatibility layer.

This gives Minds a real NixOS/Nix-built path instead of a Debian image with Nix bolted on, while avoiding an all-at-once rewrite of the workspace packaging model.

## Why This Choice

### Confidential Computing Needs a Clear Runtime Artifact

A confidential computing sandbox eventually needs an environment that can be measured and attested. The useful question is not only "did the container start?" but "what exact software stack is running before secrets are released?"

A NixOS/Nix-built image is a better fit for that question than a Debian image provisioned by shell scripts. It gives us a path toward a smaller, more declarative, and more reproducible runtime artifact.

### The Existing Debian Contract Is Too Implicit

The Debian image works partly because many assumptions are provided by the base distribution:

- shared libraries are discoverable in conventional places
- dynamic linker paths exist
- OpenSSH service layout is conventional
- system package names match script expectations
- browser dependencies can be installed through apt

Those assumptions are convenient, but they are not explicit. Moving to NixOS forces us to name them. That is uncomfortable during the first implementation, but useful for hardening.

### Compatibility Shims Become Contract Tests

The compatibility work should not live as unexplained Dockerfile trivia. Each shim should correspond to a workspace requirement:

- Docker provider can authenticate over SSH
- Docker provider can use SFTP
- workspace services start under supervisor
- Python imports needed by the workspace resolve
- Playwright/Chromium can launch and render text-heavy pages
- HTTPS, git, uv, Node, Claude Code, mngr, latchkey, modal, and supporting tools are available
- the first-boot seed path can move the baked workspace onto the Docker volume

The image contract and provider boot tests give us a place to encode those requirements. That makes the Docker/NixOS path safer to iterate on.

### Debian Plus Nix Is a Compatibility Shortcut, Not the Desired Sandbox Model

Debian plus Nix would probably be easier to ship quickly. It may still be useful if the NixOS path hits a blocking issue.

However, it does not give the same benefits for a confidential sandbox. The runtime would still be fundamentally Debian-shaped, with Nix acting as a package source for some tools. That weakens the audit and attestation story because the full workspace behavior still depends on a broad general-purpose distribution and imperative provisioning scripts.

### Fully Nix-Native Is Too Large for the First Step

The fully Nix-native model is attractive, but it would require solving too many problems at once:

- packaging or replacing all downloaded binary installers
- moving workspace service setup out of shell scripts
- making browser automation Nix-native
- reworking provider assumptions around SSH/SFTP and service management
- changing how local development syncs forever-claude-template and vendored mngr checkout

Strategy 2 lets us keep learning while preserving a path toward Strategy 3.

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
- **Nix closure manifest:** checked in and build-verified for generated platforms. The Dockerfile builds the Nix profile, writes `/etc/fct-workspace/nix-closure.txt`, and the `fct-nix-profile` stage diffs it against `nix/fct-workspace-closure.<system>.txt` from the source tree. The currently checked-in manifest covers `aarch64-linux`.
- **Manifest regeneration:** explicit. `scripts/generate_nix_closure_manifest.sh` builds the `fct-nix-profile-generate` target and copies `/etc/fct-workspace/nix-closure.txt` back into `nix/fct-workspace-closure.<system>.txt` for intentional updates.
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

The x86_64-linux manifest is intentionally not checked in yet. The earlier attempt to generate it on Docker Desktop amd64 emulation failed with `unable to load seccomp BPF program: Invalid argument`. Treat x86_64-linux as unsupported by the safe build path until a manifest is generated from a suitable x86 builder and reviewed.

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

The path toward stronger reproducibility is:

1. Keep stable `nixpkgs` pinned through `flake.lock` and update it intentionally.
2. Keep checked-in Nix closure manifests in sync with intentional `flake.lock` or package-list changes, and fail builds when the closure changes unexpectedly.
3. Generate and check in closure manifests for each supported Linux architecture before treating that architecture as part of the safe build path.
4. Replace curl installers with Nix derivations or fixed-hash downloads.
5. Replace live npm/PyPI installs with lockfile-backed, hash-backed, or internally mirrored installs where practical.
6. Produce a manifest or SBOM for the final image.
7. Sign the built image.
8. Require the signed image digest or expected measurement in the confidential sandbox attestation policy.

For external dependencies such as Claude Code, the ideal long-term state is a Nix package or fixed-output derivation for the exact approved artifact. If the build continues to use a live installer, the image can still be compatible, but it should not be treated as strongly reproducible.

## Not Decided Here

This document does not decide:

- which confidential VM runtime Minds should use
- how remote attestation policies are represented
- how secrets are released after attestation
- whether Lima should boot a full NixOS guest or use a Nix-managed guest environment
- how much of forever-claude-template should become fully Nix-native

Those decisions should build on the Docker/NixOS workspace contract once the basic runtime path is reliable.
