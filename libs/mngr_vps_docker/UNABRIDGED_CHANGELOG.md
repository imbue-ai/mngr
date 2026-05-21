# Unabridged Changelog - mngr_vps_docker

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/mngr_vps_docker/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

`rsync` added to `mngr_vps_docker.cloud_init.generate_cloud_init_user_data`'s
package list for belt-and-suspenders symmetry on cloud-init backends (paired
with `mngr_ovh`'s `install_required_outer_packages` on the non-cloud-init OVH
path).

- Refactors `VpsDockerProvider` to lift the shared parallel-SSH discovery into the base class behind a new `_list_provider_vps_hostnames()` seam method (concrete in the base, returns `[]`; overridden by concrete providers); `mngr_vultr` now only contributes the tag-listing.
- Widens `os_id` in the VPS Docker base to `int | str` so providers (like OVH) can carry friendly image names through the existing build-args parser without disrupting integer-id providers (like Vultr).
