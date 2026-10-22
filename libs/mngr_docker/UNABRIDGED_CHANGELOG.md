# Unabridged Changelog - mngr_docker

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_docker/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-20

Configuring a host's sshd now logs, at info, the fingerprints of the keys that are about to replace the host's `authorized_keys` (the client key and any appended entries), along with the host id, provider and profile. A later key rejection can then be matched against the keys the host was last configured with.

## 2026-10-18

The docker provider reports its create steps through mngr's structured progress stream (`--format jsonl`): `docker.build` or `docker.pull`, `docker.start_container`, and `docker.wait_ssh` under the create's `creating_host` phase. The image build advances through its Dockerfile stages by parsing BuildKit's `[k/n]` stage markers in the plain build output, so a caller sees how many stages remain rather than a spinner.

## 2026-10-17

Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`).

- The per-host key layout (`keys/host_keys/<host uuid hex>/` holding `docker_ssh_key` and `host_key` with their public halves, pins in `keys/known_hosts`) is published as `DOCKER_HOST_ACCESS_MANIFEST` (`constants.py`, with the key-file names as constants), which the provider's own key dir and key paths resolve through, so the minds workspace record carries a container's access material without restating the layout; the deploy-time list of profile files kept out of images is built from the same names.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-12

A caller's bare `--memory` start arg (`mngr create -s --memory=16g`) is now brought under the no-swap policy: the provider appends a matching `--memory-swap` when the caller sets a memory cap without a swap cap. Before, with `default_memory` configured, the caller's larger memory cap left the defaults' smaller swap cap in place and docker refused to run the container.

Docker discovery now reports each host's recorded size (`DiscoveredHost.size`) from the host records it already reads, so the `mngr observe` stream carries it.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-10-02

The docker provider is now its own plugin, `imbue-mngr-docker`, split out of the core `imbue-mngr` package.

- `mngr create @.docker`, `mngr docker resize`, `mngr help docker_usage`, and the `[providers.docker]` settings are unchanged, but they need the plugin installed: `uv tool install imbue-mngr --with imbue-mngr-docker`, or `mngr plugin add imbue-mngr-docker` on an existing install. The install script offers the plugin when a Docker daemon answers.

- The backend and the `mngr docker` command group register lazily, so `mngr --help`, `mngr config`, and `mngr list` no longer import the docker SDK.

- The docker-specific error classes (`DockerBuildTimeoutError`, `DockerRuntimeNotRegisteredError`, `DockerGvisorEphemeralRootfsError`, `DockerConfigValidationError`, `InvalidContainerSizeError`) moved to `imbue.mngr_docker.errors`; their names, and so the `error_class` in jsonl output, are unchanged.

- Test support: the docker CLI and SDK resource guards, and the session-end sweep of leaked test containers, are declared through the plugin's own `resource_guards` entry point, so every pytest session in which the plugin is installed enforces the `docker` / `docker_sdk` marks and sweeps leaked containers.
