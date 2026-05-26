# Unabridged Changelog - mngr_lima

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_lima/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.

## 2026-05-20

- `mngr_lima`: drop ssh-keyscan from the host-creation flow. Each Lima VM now gets a pre-generated ed25519 host keypair injected into the guest via the Lima provision script (which writes `/etc/ssh/ssh_host_ed25519_key{,.pub}`, removes other host-key types, and restarts sshd before `limactl_start_new` returns). The host machine writes the matching `known_hosts` entry atomically using the public key it already has on disk -- no scan, no `Broken pipe` race during VM bring-up, no TOFU. Mirrors `mngr_vps_docker`'s cloud-init-driven host-key injection pattern, adapted to Lima's `provision[mode=system]` surface (Lima's `UserData` Go struct doesn't expose top-level `ssh_keys`). Per-host keys and the matching `known_hosts` file live under `<provider-dir>/keys/hosts/<host_id>/` so each VM has an isolated identity (no shared `known_hosts` accumulating stale `127.0.0.1:<old-port>` entries across restarts); `delete_host` cleans up that directory. `merge_lima_yaml` now extends `provision` and `mounts` instead of replacing them: a user-supplied `provision:` (e.g. to install extra packages) is appended after mngr's, and a user-supplied `mounts:` is appended after the `/mngr` volume mount -- so mngr's load-bearing entries (host-key injection in `provision`, the `/mngr` mount) are preserved. Lima runs `provision[mode=system]` scripts in list order, so mngr's host-key swap runs before any user script.

- `mngr_lima`: switch the serial-log tailer to `tail -F`. The previous `tail --follow=name --retry` is GNU-only; BSD tail (macOS) rejects it with "unrecognized option" and exits immediately, silently losing the serial-log diagnostics during Lima VM boot. `tail -F` is portable: GNU's `-F` is documented as equivalent to `--follow=name --retry`, and BSD's `-F` is documented to wait for a non-existent file to appear and follow it on creation. Empirically verified on both platforms (GNU coreutils 9.4 in a Lima Ubuntu 24.04 guest, and macOS aarch64).

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Fix Lima provider to actually disable guest -> host port forwarding. The previous empty `portForwards: []` did not suppress Lima's auto-appended fallback rule, so guest sockets on any interface (e.g. `0.0.0.0:8082`) leaked to host loopback and collided across coexisting VMs. The provider now emits two ignore rules -- one for `guestIP: 0.0.0.0` (with `guestIPMustBeZero: true`) and one for `guestIP: 127.0.0.1` -- because empirical testing on Lima 2.1.1 showed user-supplied rules match the guest bind address literally and neither rule alone catches both cases. `merge_lima_yaml` locks `portForwards` against user `--file` overrides. SSH is unaffected -- Lima manages it through a separate top-level config.
