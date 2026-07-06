# Managed git binary for the minds desktop app

## Overview

- The minds desktop app already bundles git so end users need zero prerequisites (`specs/electron-desktop-app/concise.md` promised "a platform-appropriate git binary ... fully self-contained"). What ships today is only partially *managed*:
  - Windows (not shipped): MinGit 2.49.0, version-pinned and SHA256-verified (`apps/minds/scripts/download-binaries.js`) -- already managed.
  - macOS arm64 (the shipped platform): the real Xcode CLT git resolved via `xcrun --find git` on the ToDesktop build runner, copied with its `libexec/git-core` helpers and templates. Works, but the version is whatever the runner's CLT has: unpinned, unverifiable, not reproducible, and the build breaks if the runner lacks CLT.
  - Linux (not shipped yet): `cp $(which git)` of the bare binary -- no `libexec/git-core`, so `git clone https://...` cannot work.
- Replace the macOS and Linux acquisition with pinned, SHA256-verified downloads of [dugite-native](https://github.com/desktop/dugite-native) -- the relocatable git distribution GitHub Desktop builds precisely for embedding in Electron apps. Current release: tag `v2.53.0-3` (git 2.53.0, published 2026-04-14), with per-target tarballs and published `.sha256` companion assets.
- Scope decisions (2026-07-06):
  - Ship and test darwin-arm64 and linux-x64. The manifest additionally carries darwin-x64, linux-arm64, and windows-x64 entries so those targets are one flag away, but they are not shipped or CI-gated.
  - Provenance is pinned SHA256 hashes against dugite-native's GitHub release assets (no artifact mirroring for now).
  - A weekly CI workflow nags (via a GitHub issue) when the pinned release falls behind dugite-native's latest, which tracks upstream git security releases.
  - Credential helpers stay out of scope (Tier 0 below); the payload retains git-credential-manager so enabling them later is configuration, not bundling.
- Invariants:
  - Zero prerequisites for end users; no Xcode CLT dependency at build or run time.
  - The bundled git is visible only to the app's backend subprocess tree (the existing `PATH` prepend in `apps/minds/electron/backend.js`); it never shadows the user's own git or rewrites their config.
  - Acquisition fails loudly on hash mismatch or unknown target; there is no silent fallback to a machine-provided git.
  - Acquisition is data-driven (a JSON manifest) plus one download function, decoupled from ToDesktop specifics, so future build-process changes (`ELECTRON_BUNDLING_AUDIT.md` remediation) consume the same manifest without rework. The FCT container build work (`specs/faster-minds-build/concise.md`) is unaffected: agent-side git remains the container image's concern.
- Empirical facts this design relies on (verified 2026-07-06 by downloading and running `v2.53.0-3` payloads):
  - Published `.sha256` values match independently recomputed hashes for darwin-arm64 and linux-x64.
  - macOS binaries link only system libraries (`/usr/lib/libz`, `libiconv`, `libcurl`, `libexpat`, system frameworks) -- fully relocatable, no Homebrew/CLT dependencies. Linux binaries are dynamically linked against glibc (fine for mainstream desktop distros; musl is out of scope).
  - Payload layout: `bin/{git,scalar}`, `libexec/git-core/` (includes `git-remote-https`, `git-lfs` 12 MB, `git-credential-manager` 2.7.3, `git-credential-cache`, `git-credential-store`), `share/git-core/templates/`, `etc/gitconfig`, and on Linux `ssl/cacert.pem`. Compressed ~57-60 MB, unpacked ~140 MB per platform.
  - `git-credential-manager --version` executes standalone from the payload (end-to-end auth flows were not exercised).

**Warning:** dugite-native binaries are built with an empty prefix. Run bare, `git --exec-path` resolves to `//libexec/git-core`, and `git clone https://...` fails with `'remote-https' is not a git command` (reproduced locally; plain `PATH` prepending is NOT sufficient, even though local-only operations happen to work because builtins live in the main binary). The environment contract below is a hard requirement, and the reason the current CLT-copy approach cannot be "just swapped" without runtime changes.

## Expected behavior

- `pnpm build` (and ToDesktop's `beforeInstall` hook re-running `scripts/download-binaries.js` on the build server) downloads the manifest-pinned dugite-native tarball for the build platform, verifies its SHA256, and extracts it to `resources/git/`. Hash mismatch or a platform absent from the manifest aborts the build. `xcrun`, CLT, and `which git` are no longer consulted.
- The packaged app's backend subprocess tree resolves `git` to `resources/git/bin/git` (unchanged path, `apps/minds/electron/paths.js:30`) and receives the full git environment contract. The user's own shells and tools are untouched.
- Dev mode (`pnpm start`) uses the identical payload: the `prestart` `ensure-binaries.js` hook already requires `resources/git/bin/git` and triggers the same downloader.
- `git --version` inside the app reports exactly the manifest's `gitVersion` (2.53.0 at pin time), on every platform, on every build, regardless of which machine built it. The backend logs the bundled git version once at startup for supportability.
- Linux is continuously proven in CI: an acceptance test (running on Linux via offload) downloads, verifies, extracts, and exercises the linux-x64 payload -- including an HTTPS clone through `git-remote-https` -- even though a packaged Linux app is not shipping yet. When the ToDesktop `linux` target is eventually enabled, git is a solved problem, not a stub.
- Once a week, CI compares the pinned tag against dugite-native's latest release. If behind, it opens (or updates) a single tracking issue; when the pin catches up, the workflow closes it.
- When git publishes a security release: dugite-native cuts a release within days; the nag issue appears within a week; a maintainer follows the update runbook (bump manifest + hashes, let CI verify); the fix ships with the next app release via ToDesktop auto-update.

## Changes

### New file: `apps/minds/scripts/git-manifest.json`

Single source of truth for the pinned git payload, consumed by `download-binaries.js` (Node), the tests (Python), and the freshness workflow (jq):

```json
{
  "dugiteNativeTag": "v2.53.0-3",
  "gitVersion": "2.53.0",
  "targets": {
    "darwin-arm64": { "asset": "dugite-native-v2.53.0-f49d009-macOS-arm64.tar.gz",  "sha256": "e561cfc80c755e6f3e938653e81efcd025c9827a5b76dd42778b1159b3fab437", "shipped": true  },
    "darwin-x64":   { "asset": "dugite-native-v2.53.0-f49d009-macOS-x64.tar.gz",    "sha256": "caf27c36b8834969550535bcd5e58186f970e080d1e175e76d9c1de3aac409ed", "shipped": false },
    "linux-x64":    { "asset": "dugite-native-v2.53.0-f49d009-ubuntu-x64.tar.gz",   "sha256": "b3a85433c8dfde76d21b90938ad2f971653deff4340b1b4d347258c63250eafc", "shipped": true  },
    "linux-arm64":  { "asset": "dugite-native-v2.53.0-f49d009-ubuntu-arm64.tar.gz", "sha256": "d562ad433ed0dc1907f44a92fc701597bc577c48d07fe69ee7adddfee836ef4c", "shipped": false },
    "win32-x64":    { "asset": "dugite-native-v2.53.0-f49d009-windows-x64.tar.gz",  "sha256": "f843a87a693bfdabed83b8492bca59db6f64d1168c74d23e2c8dfb7388a97142", "shipped": false }
  }
}
```

- Download URL is derived: `https://github.com/desktop/dugite-native/releases/download/<dugiteNativeTag>/<asset>`. Asset filenames embed a dugite-native commit short-SHA (`f49d009`), so they must be recorded verbatim, not templated from the version.
- `shipped: true` marks targets that release verification and CI acceptance tests gate on. Non-shipped entries exist so dev machines (e.g. an Intel Mac) and future platform bring-up get the managed path for free.
- Hashes above were taken from the published `.sha256` release assets on 2026-07-06; darwin-arm64 and linux-x64 were additionally recomputed from independently downloaded bytes. Implementation must re-verify all five the same way (pinning defends against future substitution, not against copying a wrong value in).

### `apps/minds/scripts/download-binaries.js`

- Rewrite `downloadGit`'s darwin and linux branches into one manifest-driven path: map `(platform, arch)` to a manifest target key, download, `verifyChecksum` against the manifest hash, extract into `resources/git/` preserving the payload layout (`bin/`, `libexec/`, `share/`, `etc/`, and `ssl/` on Linux), and assert `resources/git/bin/git` exists. Delete `copyGitCoreDereferencingSymlinks` and the `xcrun`/`which git` logic. **Note:** the tarball is rooted flat (`bin/` etc. at archive root) -- extract *without* `--strip-components=1`, unlike the uv/lima archives.
- `downloadGit` writes the pinned tag to `resources/git/.dugite-tag`, and `ensure-binaries.js` treats a missing or mismatched marker as a missing binary. Without this, dev machines carrying the old CLT-copied payload would pass the existence check and never upgrade.
- The Windows branch keeps pinned MinGit unchanged for now (working, verified, and untestable here since no Windows target is wired up); the `win32-x64` manifest entry documents the intended future unification. `downloadGit` does not consume it yet.
- A requested target with no manifest entry is a hard error (same spirit as the existing "No pinned SHA256" error).
- git's hashes move out of `EXPECTED_SHA256` into the manifest; uv/restic/desync/MinGit stay where they are. Consolidating all binaries into manifests is left to the build-process overhaul.

### `apps/minds/electron/backend.js` -- runtime environment contract

Wherever the backend child environment is constructed (the existing `PATH` prepend at `backend.js:248`, both dev and packaged modes), additionally export, with `<gitRoot>` = `resources/git`:

- `GIT_EXEC_PATH=<gitRoot>/libexec/git-core` -- required; see Warning above.
- `GIT_TEMPLATE_DIR=<gitRoot>/share/git-core/templates` -- otherwise every `init`/`clone` warns `templates not found in //share/git-core/templates`.
- `GIT_CONFIG_SYSTEM=<gitRoot>/etc/gitconfig` -- the payload's system-level config, which chain-`[include]`s the machine's real `/etc/gitconfig`, so machine-level configuration still applies while the payload provides sane defaults (upstream designed the file for exactly this use).
- `GIT_SSL_CAINFO=<gitRoot>/ssl/cacert.pem` -- Linux only; the Linux build does not use the system trust store. macOS links the system libcurl and must NOT get this override.

These are set only in the backend subprocess environment, never globally. Implementation must confirm mngr does not blanket-forward `GIT_*` variables into remote/container sessions (remote environments are constructed explicitly, so no leakage is expected -- verify, don't assume).

### Licensing

- Stage a `NOTICE` file into `resources/git/` recording: git 2.53.0 (GPLv2) with a source URL, dugite-native tag URL, git-credential-manager 2.7.3 (MIT), git-lfs (MIT), and the GPLv2 text (git's `COPYING`). We distribute git binaries *today* without this; the swap is the moment to fix it.

### New file: `.github/workflows/minds-git-freshness.yml`

- Weekly cron plus `workflow_dispatch`, on ubuntu. Reads `dugiteNativeTag` from the manifest, fetches `repos/desktop/dugite-native/releases/latest` via `gh api`, and:
  - If behind: create-or-update a single issue (matched by stable title; deliberately no label dependency, so the workflow never needs label provisioning) containing the latest tag and release notes excerpt.
  - If current: close any open nag issue.
- Rationale: dugite-native's releases track upstream git security releases within days, and nagging on what is actually pinnable is the actionable signal. Watching git CVE feeds directly is deliberately omitted (it would nag about versions we cannot pin yet).

### Update runbook (also lands in `apps/minds/docs/desktop-app.md`)

1. Pick the new dugite-native tag from the nag issue.
2. Update `git-manifest.json`: tag, `gitVersion`, all five asset names (mind the embedded short-SHA), and hashes from the `.sha256` assets.
3. Independently download each tarball and recompute its SHA256; compare against step 2.
4. Run the bundled-git acceptance test locally on a mac; CI covers linux-x64.
5. Ship through the normal release process; the freshness workflow closes the nag issue on its next run.

### Docs

- Rewrite the git bullet of the "Bundled binaries" section in `apps/minds/docs/desktop-app.md` (lines 99-107): it currently says git is "copied from the build machine; a statically-linked distribution should be used for production", and line 235 still claims `libexec/git-core/` is skipped -- stale on both counts after this change (and the libexec claim is already stale today for macOS). Document the env contract and the runbook there.
- Update the header comment of `download-binaries.js` (its git section) to describe the dugite-native flow.

### Test updates

- `apps/minds/scripts/build_test.py` (unit, no network): manifest schema validation -- five well-formed entries, 64-hex hashes, asset names consistent with `dugiteNativeTag`'s version, `shipped` flags exactly `{darwin-arm64, linux-x64}`; `downloadGit`'s platform map covers every shipped target.
- New `apps/minds/test_bundled_git.py` (`@pytest.mark.acceptance`; runs on Linux in offload CI, and on macs when run locally): for the current platform's manifest entry, download the asset, verify SHA256, extract to a temp dir, then with the full env contract applied assert:
  - `git --version` equals `gitVersion`;
  - `git clone` of a local fixture repo succeeds with no template warnings;
  - an HTTPS clone through `git-remote-https` succeeds against a *local* self-signed HTTPS server serving a dumb-HTTP bare repo, with `GIT_SSL_CAINFO` pointed at the test CA -- this hermetically proves helper dispatch and TLS wiring (the exact failure mode the Warning describes) without depending on external networks beyond the release download itself.
- The existing `minds-launch-to-msg.yml` e2e on the self-hosted mac runner remains the end-to-end proof for the packaged darwin-arm64 bundle.
- The first implementation PR must produce a beta ToDesktop build and pass the launch-to-msg e2e against it before promoting, explicitly checking the two open questions below: notarization of the new payload, and the recorded upload-size delta.

## Credential helpers: cost tiers (decided: Tier 0)

Local git today performs only anonymous HTTPS clones of public template repos and ssh pushes into agent hosts (system ssh, user keys -- unaffected by this change). No local credential flows exist.

- **Tier 0 (this design, ~0 cost):** no credential support; the invariant "local app git does anonymous HTTPS + ssh only" is documented. Behavioral delta vs. the CLT payload: Apple's git ships `git-credential-osxkeychain`, dugite-native does not, so a user whose global config names `osxkeychain` would see helper-not-found noise -- but only on credential-requiring local operations, which minds does not perform.
- **Tier 1 (~1-2 days eng + product UX decisions):** enable the bundled git-credential-manager 2.7.3 via our `GIT_CONFIG_SYSTEM` file (`[credential] helper = manager`), choose its credential store per platform (macOS keychain; Linux secretservice vs. file), and decide when interactive browser-OAuth prompts may appear. The plumbing is small because the payload already contains a GCM that executes standalone (`--version` verified; end-to-end auth flows must be validated as part of Tier 1); the real cost is UX design and the ongoing support surface of auth flows.
- **Tier 2 (~1-2 weeks):** minds-native credential helper/`GIT_ASKPASS` shim that sources tokens from minds' own account system (natural fit with latchkey later). Best UX and control; only justified when private repos become a product feature.

## Rationale and alternatives considered

- **Status quo (copy CLT git):** unpinned, unreproducible, runner-coupled; the exact deficiency this design removes.
- **Build git ourselves in CI:** maximal provenance control, but permanent per-platform build infrastructure plus tracking git security releases; contradicts the repo's lean-on-maintained-upstreams ethos. The manifest keeps this door open (only URLs and hashes would change).
- **Runtime download (sculptor's managed-tools pattern):** sculptor downloads/pins/verifies fast-moving tools (the claude CLI) at runtime and deliberately leaves git to the system. Wrong fit here: git is slow-moving, needed before first use, and ToDesktop auto-update already refreshes bundles; runtime download would add offline-first-launch failure modes.
- **System git (sculptor's git approach):** acceptable for a developer audience; breaks minds' zero-prerequisites promise.
- **Trust model:** dugite-native joins the already-trusted set of upstream binary providers (astral-sh uv, restic, folbricht desync, lima-vm, git-for-windows), with the same pinned-hash discipline. Mirroring assets into an imbue-owned bucket is deferred; the trigger to revisit is any availability or immutability incident with GitHub release assets.

## Out of scope

- Agent/container-side git (Docker images, provisioned hosts, FCT) -- unchanged, remains the image's concern.
- Windows bring-up (MinGit path stays as-is until a Windows target exists).
- Payload pruning: git-lfs (12 MB), scalar (1.9 MB), GCM (0.3 MB) ride along unmodified; upstream-tested payload integrity beats size optimization at current scale. Revisit trigger: upload-size pressure.
- Artifact mirroring; credential Tiers 1-2; `GIT_CONFIG_GLOBAL` isolation (user global config keeps applying, as today).
- Related cleanup worth a separate PR: `build.js` downloads Lima with *no* checksum verification, unlike every other binary.

## Open questions

1. Upload size: compressed payload is ~57 MB vs. the smaller CLT copy today; `todesktop.js` `uploadSizeLimit` is 600. Measure the packaged before/after during implementation and bump the limit or invoke the pruning knob if needed.
2. Code signing: today ToDesktop signs the CLT-copied git binaries without explicit `additionalBinariesToSign` entries; the dugite payload has ~400 executables in `libexec/git-core` plus .NET pieces. Expect automatic deep-signing to handle it (it does for GitHub Desktop); verify on the first beta build and add explicit entries only if notarization rejects.
3. Confirm no `GIT_*` env forwarding into remote agent sessions (expected none; verify during implementation).
