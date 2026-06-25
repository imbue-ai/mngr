# Unabridged Changelog - mngr_vultr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_vultr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-22

An unauthenticated Vultr provider now errors instead of silently reporting zero agents.

Previously, with no API key configured, the Vultr provider printed an ad-hoc `WARNING: Vultr API key not configured, skipping VPS discovery` and returned an empty listing (exit 0). It now raises the shared `ProviderNotAuthorizedError` at construction, so an enabled-but-unauthenticated Vultr provider is reported consistently with the other cloud providers (one consistent error line in `mngr list`, contributing a non-zero exit) rather than vanishing. The bespoke warning print has been removed.

## 2026-06-19

Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename: the VPS
provider is no longer Docker-only, so the package and its shape-agnostic base
classes dropped "Docker" from their names (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` ->
`VpsError`). Import-only change; no behavior difference.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

Trimmed the README to user-relevant content and tightened it for concision.

Aligned the config field descriptions with the README table so the `Vultr-specific configuration` table and the Pydantic `Field(description=...)` strings match.

Corrected the create example to use the provider-prefixed `--vultr-region` / `--vultr-plan` build-arg flags.

## 2026-06-16

## Vultr provider

- The Vultr release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing modal/gcp/aws/ovh disables. Without it, `mngr list` inside the Vultr lifecycle tests would enumerate the newly-added azure provider and exit non-zero when Azure credentials weren't resolvable in that subprocess, failing the Vultr tests for a non-Vultr reason.

Removed the dead VPS client methods `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` from `VultrVpsClient`. These had no production callers and are being dropped from the shared `VpsClientInterface`. The corresponding unit and release tests were removed as well.

## 2026-06-15

## Internal: disable the new `gcp` provider in Vultr release-test settings

- The Vultr release tests write a `settings.toml` that disables every other remote provider so the create-host preflight does not trip resolving their credentials. With the new `gcp` provider now registered as a remote backend, it is added to that disable-set (matching the existing modal/aws/ovh/imbue_cloud entries). No behavioral change for Vultr.

## 2026-06-12

## AWS provider support: shared VPS-Docker base refactor

- Adopts the new `_fetch_provider_instances` hook on `VpsDockerProvider`; the per-class `_list_instances_cached` override is gone (cache scaffolding now lives on the base).
- `VultrVpsClient` carries `os_id` locally (a field on the client) now that the shared `VpsClientInterface.create_instance` no longer accepts it. `--vps-os=` build arg removed; per-host overrides require a separate Vultr provider instance with its own `default_os_id`.
- `get_build_args_help()` no longer carries the stale "OS image is set via default_os_id..." block — that described the removed shared build arg, not current Vultr behavior.
- Picks up the shared `wait_for_instance_active` interface change (now a default method on `VpsClientInterface`).
- `is_for_host_creation` flag removed; the Vultr backend's `del`-of-`is_for_host_creation` is removed. No behavior change.
- **Per-host build args renamed**: `--vps-region=` is now `--vultr-region=`; `--vps-plan=` is now `--vultr-plan=`. The old `--vps-*` prefix raises a migration error. `--git-depth=` stays shared.

- **Vultr release test create timeout raised 300s -> 600s.** `_run_mngr`'s default subprocess timeout was too tight for a slow Vultr provision (provisioning alone can take ~90s; the full create adds cloud-init + Docker build + rsync), causing intermittent spurious `subprocess.TimeoutExpired` failures unrelated to any real defect.

## 2026-06-11

Replaced a direct ValueError raise in Vultr provider config with a dedicated custom exception type.

## 2026-06-10

Raised the stale coverage floor from 60% to 70% to match the coverage CI already measures (~72%).

## 2026-06-08

Tests now isolate $HOME the same way as every other mngr plugin: the project
conftest calls `register_plugin_test_fixtures(globals())`, which brings in the
autouse `setup_test_mngr_env` fixture. Previously this plugin's tests did not
redirect $HOME, so running them on their own could read or write the real
`~/.mngr` / `~/.claude.json`. Internal test-infrastructure change only; no
user-facing behavior change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-05-29

User-visible: minds workspaces running on Vultr (docker-on-VPS) hosts can now
be backed up off-site (restic) when a backup provider is selected at creation
time.

(No code change in this project in this PR; the integration lives in the
minds app and the forever-claude-template `host_backup` service.)

Vultr hosts created by `mngr create --provider vultr` now back their per-host
unified docker volume with a btrfs subvolume on a loop-mounted btrfs filesystem
on the VPS (`/mngr-btrfs/<host_id_hex>` on `/var/lib/mngr-btrfs.img`). This
makes future consistent snapshotting of the agent data via
`btrfs subvolume snapshot -r` possible. See `mngr_vps_docker`'s changelog for
the full mechanism.

**Breaking change:** existing vultr hosts created before this release cannot
be discovered or managed after upgrade. Destroy and recreate them.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

`mngr_vultr` now only contributes the tag-listing; the shared parallel-SSH discovery has been lifted into the `VpsDockerProvider` base class behind a new `_list_provider_vps_hostnames()` seam method.
