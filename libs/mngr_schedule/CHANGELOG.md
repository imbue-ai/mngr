# Changelog - mngr_schedule

A concise, human-friendly summary of changes for the `mngr_schedule` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.2.0] - 2026-10-22

### Fixed

- Fixed: `mngr schedule remove` finds and stops the schedule's Modal app; it matched `modal app list --json` rows on the wrong keys, logged "Modal app not found in environment", and left the app running.
- Fixed: `mngr schedule add --provider modal` creates the per-user Modal environment on a fresh install instead of failing with "Provider 'modal' has no state yet".
- Fixed: Scheduled deploys no longer drop the deployer's `config.toml` and profile files when the mngr host dir is outside `$HOME`.
- Fixed: `--include-project-settings` stages project-local settings from `.<root name>` instead of always `.mngr`, so they reach the container under a non-default `MNGR_ROOT_NAME`.
- Fixed: Trigger verification no longer times out when the Modal container's `mngr list` reports an unreachable provider; the container's mngr no longer installs the docker plugin.
- Fixed: EDITABLE/SKIP-mode deploys resolve the mngr Dockerfile from the installed `imbue.mngr` package, so they work when the monorepo is vendored inside another git repository.

## [v0.1.6] - 2026-06-18

## [v0.1.5] - 2026-06-16

## [v0.1.4] - 2026-06-16

## [v0.1.3] - 2026-06-15

### Fixed

- Fixed: `mngr schedule remove` now passes `--yes` when stopping a schedule's Modal app, so it no longer aborts with "no interactive terminal detected" under newer Modal CLIs when run non-interactively (e.g. from a deploy script).

## [v0.1.2] - 2026-06-13

### Added

- Added: `--timezone` option on `mngr schedule add` pinning the IANA timezone the `--schedule` cron expression is interpreted in (e.g. `--timezone America/Los_Angeles`). Previously cron was always interpreted in the deploying machine's local timezone, so the same schedule could fire at different wall-clock times depending on where it was deployed from. Validated against the IANA timezone database at deploy time. Modal provider only; passing it with `--provider local` is an error.

### Changed

- Changed: Replaced direct ValueError raises in modal deploy upload-spec parsing with a dedicated `UploadSpecError` exception type.

## [v0.1.1] - 2026-06-08

### Fixed

- Fixed: `imbue-mngr-schedule` is now auto-discovered as a publishable package by the release tooling and will be offered for first publication to PyPI on the next release. Fixes a latent bug where the install wizard already listed it (it is in the mngr install catalog), so a user picking it hit a PyPI 404.

## [v0.1.0] - 2026-06-05

### Changed

- Changed: `mngr schedule add --verify quick|full` now works when the trigger's `mngr create` produces an agent inside the cron-runner's local provider; verification runs inside the container and reports back over a structured sentinel line.
- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. Previously-unpinned internal deps (`imbue-mngr`, `imbue-common`, `imbue-mngr-modal`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

### Fixed

- Fixed: PACKAGE-mode Modal deploy Dockerfile generator now installs the correct PyPI distribution names `imbue-mngr` and `imbue-mngr-schedule` instead of `mngr` / `mngr-schedule`, which do not resolve on PyPI. (Note: `imbue-mngr-schedule` is not yet published, so PACKAGE-mode schedule deploys still require publishing it.)
