Integrated Vault-backed secrets into the minds snapshot CI jobs and switched the in-sandbox forever-claude-template (FCT) agent-container build to depot.dev for faster, layer-cached builds.

- Sped up the minds snapshot build further: `playwright install` (chromium + apt deps, ~24s, source-independent) now runs in a cached pre-repo image layer instead of re-running on every commit, and the producer sandbox gets 8 CPUs (the in-sandbox FCT container build is CPU-bound). Verified the pre-code image layers (apt/node/uv/claude/depot CLI) are already cached; only the post-`COPY` deps layer rebuilds per commit.

- The minds snapshot build now uses the local **docker** builder by default for the in-sandbox forever-claude-template container, and `scripts/snapshot_minds_e2e_state.py` prints a per-phase timing summary (`PHASE_TIMING ...`). Measurements showed depot.dev was ~2.5 min slower end-to-end for this job (depot's remote cache helps the build, but `depot build --load` must export and download the whole image back into the ephemeral sandbox's docker daemon, which a local build avoids). Set the `MINDS_SNAPSHOT_BUILDER` repo variable to `depot` to switch back (the depot path, Vault role, and cache-hit test are all retained and gated on that mode).

- The snapshot image build now compiles the minds Tailwind stylesheet (`pnpm run build:css`) before driving the Electron workspace-creation flow; the gitignored `app.min.css` is otherwise absent in the image and its missing `.hidden` rule made the onboarding flow appear stuck, failing the build.

- Added a local `.github/actions/export-secrets` composite action (vendored from the private `imbue-ai/vault` repo): since this repo is public and the vault repo is private, GitHub cannot resolve the cross-repo `imbue-ai/vault/.github/actions/export-secrets@main` action, so the backing script is vendored and invoked locally.

- `build-minds-snapshot` now fetches a depot token from Vault via GitHub OIDC (role `minds_ci_build_gh`, gated on the `minds-ci-build` Environment) and builds the FCT container via depot's remote builder. `scripts/snapshot_minds_e2e_state.py` bakes the depot CLI into the snapshot image, forwards the depot credentials + `MNGR__PROVIDERS__DOCKER__BUILDER=DEPOT` into the Modal sandbox, and gained a `--require-depot` flag that hard-fails (no silent local fallback) when the token is missing.

- `test-minds-snapshot` fetches the depot token and `ANTHROPIC_API_KEY` from Vault (role `minds_ci_test_gh`, gated on the `minds-ci-test` Environment); the `test-offload-minds-snapshot` recipe forwards them into the offload sandbox.

- Both snapshot jobs skip fork PRs (which GitHub denies the OIDC `id-token` permission); same-repo branches and main run as before, still behind the `DISABLE_MINDS_SNAPSHOT_CI` kill switch.
