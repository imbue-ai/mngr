# Unabridged Changelog - mngr_vultr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_vultr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

`mngr_vultr` now only contributes the tag-listing; the shared parallel-SSH discovery has been lifted into the `VpsDockerProvider` base class behind a new `_list_provider_vps_hostnames()` seam method.
