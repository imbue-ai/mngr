Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile` (`OFFLOAD_VERSION`) from `0.9.13` to `0.9.14`, keeping the in-image `offload apply-diff` binary in lockstep with the CI pin.

Corrected the stale cross-reference in the comment above that pin: it told readers to keep `OFFLOAD_VERSION` in sync with `.github/workflows/ci.yml`, but the CI-side pin moved into the `.github/actions/setup-offload` composite action when the three offload jobs were de-duplicated, and `ci.yml` no longer names a version.
