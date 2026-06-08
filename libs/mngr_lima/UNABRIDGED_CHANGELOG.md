# Unabridged Changelog - mngr_lima

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_lima/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-02

Collapsed a redundant `except` clause: `except (LimaCommandError, MngrError, OSError)` is now
`except (MngrError, OSError)` (since `LimaCommandError` is already a `MngrError` subclass). No
behavior change.

## 2026-05-29

Added an opt-in btrfs host-data volume mode to the Lima provider. The
new `is_host_data_volume_exposed: bool = True` field on `LimaProviderConfig`
(and the matching field persisted on `LimaHostConfig` in the per-host
record) controls how `host_dir` is backed:

- `True` (default) keeps today's behavior: `host_dir` is a 9p bind mount
  of `~/.mngr/providers/lima/<name>/volumes/<host_id>/` from the host
  machine. The host can read `host_dir` contents directly even while
  the VM is stopped, and `get_volume_for_host()` returns a usable
  `HostVolume`.

- `False` attaches a Lima-managed btrfs `additionalDisk`
  (`mngr-<host_id_hex>-data`, 100GiB default logical size, qcow2 sparse
  storage under `~/.lima/_disks/`) and symlinks `host_dir` directly to
  Lima's auto-mount path for that disk (`/mnt/lima-<disk_name>`); the
  9p mount is omitted entirely. This makes `host_dir` snapshottable as
  a single consistent btrfs filesystem. `get_volume_for_host()` returns
  `None` in this mode; callers (events API, mngr_claude session
  preservation, mngr_tmr, mngr_file) already degrade gracefully.

The chosen value is locked on the per-host record at create time so
`stop_host` / `start_host` always replay the same layout. Records that
predate the field default to `True`, preserving today's behavior for
all existing Lima hosts. `destroy_host` and `delete_host` now also
remove the named Lima disk when a host was created in btrfs mode.

A new `host_data_disk_size` config field (default `"100GiB"`) and new
`limactl_disk_create` / `limactl_disk_delete` helpers in `limactl.py`
round out the plumbing. `create_host` pre-creates the named disk via
`limactl disk create` before starting the VM (Lima's `additionalDisks
+ format: true` only auto-formats an already-existing disk). The
provisioning script `chmod 0777`s the btrfs root after the bind-mount
so the Lima default non-root user can write to `host_dir` without
sudo. Snapshot/backup API support stays out of scope for this change
(`supports_snapshots` remains `False`).

User-visible: minds workspaces running on Lima hosts can now be backed up
off-site (restic) when a backup provider is selected at creation time; the
local btrfs snapshot path these hosts use is what the backup service reads
from.

(No code change in this project in this PR; the integration lives in the
minds app and the forever-claude-template `host_backup` service.)

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

- `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.

## 2026-05-20

- `mngr_lima`: drop ssh-keyscan from the host-creation flow. Each Lima VM now gets a pre-generated ed25519 host keypair injected into the guest via the Lima provision script (which writes `/etc/ssh/ssh_host_ed25519_key{,.pub}`, removes other host-key types, and restarts sshd before `limactl_start_new` returns). The host machine writes the matching `known_hosts` entry atomically using the public key it already has on disk -- no scan, no `Broken pipe` race during VM bring-up, no TOFU. Mirrors `mngr_vps_docker`'s cloud-init-driven host-key injection pattern, adapted to Lima's `provision[mode=system]` surface (Lima's `UserData` Go struct doesn't expose top-level `ssh_keys`). Per-host keys and the matching `known_hosts` file live under `<provider-dir>/keys/hosts/<host_id>/` so each VM has an isolated identity (no shared `known_hosts` accumulating stale `127.0.0.1:<old-port>` entries across restarts); `delete_host` cleans up that directory. `merge_lima_yaml` now extends `provision` and `mounts` instead of replacing them: a user-supplied `provision:` (e.g. to install extra packages) is appended after mngr's, and a user-supplied `mounts:` is appended after the `/mngr` volume mount -- so mngr's load-bearing entries (host-key injection in `provision`, the `/mngr` mount) are preserved. Lima runs `provision[mode=system]` scripts in list order, so mngr's host-key swap runs before any user script.

- `mngr_lima`: switch the serial-log tailer to `tail -F`. The previous `tail --follow=name --retry` is GNU-only; BSD tail (macOS) rejects it with "unrecognized option" and exits immediately, silently losing the serial-log diagnostics during Lima VM boot. `tail -F` is portable: GNU's `-F` is documented as equivalent to `--follow=name --retry`, and BSD's `-F` is documented to wait for a non-existent file to appear and follow it on creation. Empirically verified on both platforms (GNU coreutils 9.4 in a Lima Ubuntu 24.04 guest, and macOS aarch64).

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Fix Lima provider to actually disable guest -> host port forwarding. The previous empty `portForwards: []` did not suppress Lima's auto-appended fallback rule, so guest sockets on any interface (e.g. `0.0.0.0:8082`) leaked to host loopback and collided across coexisting VMs. The provider now emits two ignore rules -- one for `guestIP: 0.0.0.0` (with `guestIPMustBeZero: true`) and one for `guestIP: 127.0.0.1` -- because empirical testing on Lima 2.1.1 showed user-supplied rules match the guest bind address literally and neither rule alone catches both cases. `merge_lima_yaml` locks `portForwards` against user `--file` overrides. SSH is unaffected -- Lima manages it through a separate top-level config.
