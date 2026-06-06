# Changelog - mngr_schedule

A concise, human-friendly summary of changes for the `mngr_schedule` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.0] - 2026-06-05

### Changed

- Changed: `mngr schedule add --verify quick|full` now works when the trigger's `mngr create` produces an agent inside the cron-runner's local provider; verification runs inside the container and reports back over a structured sentinel line.
- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. Previously-unpinned internal deps (`imbue-mngr`, `imbue-common`, `imbue-mngr-modal`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

### Fixed

- Fixed: PACKAGE-mode Modal deploy Dockerfile generator now installs the correct PyPI distribution names `imbue-mngr` and `imbue-mngr-schedule` instead of `mngr` / `mngr-schedule`, which do not resolve on PyPI. (Note: `imbue-mngr-schedule` is not yet published, so PACKAGE-mode schedule deploys still require publishing it.)
