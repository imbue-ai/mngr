import subprocess
from pathlib import Path

from imbue.mngr_imbue_cloud.slices.bare_metal_prep import build_box_prep_script
from imbue.mngr_vps.host_setup import PINNED_DOCKER_APT_VERSION

_POOL_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTpoolkey mngr-pool"
_IMAGE_URL = (
    "https://cloud.debian.org/images/cloud/bookworm/20260601-2496/debian-12-genericcloud-amd64-20260601-2496.qcow2"
)


def _script() -> str:
    return build_box_prep_script(
        pool_public_key=_POOL_PUB,
        lima_service_user="limahost",
        lima_version="2.2.0",
        slice_base_image_url=_IMAGE_URL,
    )


def test_prep_script_stages_base_image_under_lima_user_via_file_path() -> None:
    script = _script()
    # The OS image is fetched once, validated as a real qcow2, and atomically moved
    # into place under the lima user's home (so the bake can boot it via file://
    # with no Debian-mirror dependency). Idempotent: skips if already present.
    assert _IMAGE_URL in script
    assert "/home/limahost/.cache/mngr-slice-base/debian-base.qcow2" in script
    assert "qemu-img info" in script
    assert 'if [ ! -f "$img" ]; then' in script


def test_prep_script_chowns_cache_dir_to_lima_user() -> None:
    script = _script()
    # The script runs as root; staging the image under ~/.cache must leave ~/.cache
    # owned by the lima user, or `limactl` (run as that user) cannot create
    # ~/.cache/lima and every VM start fails. The parent cache dir must be chowned,
    # not just the leaf image dir.
    assert 'cache_dir="$(dirname "$image_dir")"' in script
    assert 'chown limahost:limahost "$cache_dir" "$image_dir"' in script


def test_prep_script_installs_qemu_and_lima() -> None:
    script = _script()
    assert "qemu-system-x86" in script
    assert "lima-2.2.0-Linux-x86_64.tar.gz" in script
    assert "github.com/lima-vm/lima/releases/download/v2.2.0/" in script


def test_prep_script_never_invokes_limactl_as_root() -> None:
    # The script runs as root; limactl refuses to run as root, so it must only be
    # extracted, never executed, here. The boot-autostart heredoc is excluded:
    # its limactl calls run later as the lima user via the systemd unit's
    # User= directive, not during root prep.
    script = _script()
    assert "tar -C /usr/local" in script
    autostart_start = script.index("<<'MNGR_SLICES_AUTOSTART'")
    autostart_end = script.rindex("MNGR_SLICES_AUTOSTART")
    root_executed_script = script[:autostart_start] + script[autostart_end:]
    assert "limactl --version" not in root_executed_script
    assert "limactl start" not in root_executed_script
    assert "limactl list" not in root_executed_script


def test_prep_script_creates_service_user_with_kvm_and_pool_key() -> None:
    script = _script()
    assert "useradd -m -s /bin/bash limahost" in script
    assert "usermod -aG kvm limahost" in script
    assert _POOL_PUB in script
    assert "/home/limahost/.ssh/authorized_keys" in script


def test_prep_script_is_idempotent_guarded() -> None:
    script = _script()
    # Re-runnable: guards on the recorded lima version and the existing user.
    assert 'if [ "$(cat "$lima_version_marker" 2>/dev/null)" != "2.2.0" ]; then' in script
    assert "id limahost >/dev/null 2>&1" in script


def test_prep_script_upgrades_lima_when_installed_version_differs() -> None:
    # The lima install guard compares a marker file against the pinned release
    # (never `limactl --version` -- limactl refuses to run as root), so re-running
    # prep on a box with an older lima (or one prepped before the marker existed,
    # where the cat yields "") re-extracts the tarball and records the new version.
    script = _script()
    assert "lima_version_marker=/usr/local/share/lima/.mngr-installed-lima-version" in script
    marker_write_idx = script.index('printf \'%s\\n\' "2.2.0" > "$lima_version_marker"')
    extract_idx = script.index("tar -C /usr/local -xzf")
    assert extract_idx < marker_write_idx


def test_prep_script_installs_uv_for_service_user() -> None:
    script = _script()
    assert "astral.sh/uv/install.sh" in script
    assert "sudo -u limahost" in script


def test_prep_script_provisions_swapfile() -> None:
    # Slice hosts run RAM near capacity, so prep adds a real 32GiB swapfile (the
    # OS-install default of two tiny partitions is useless). Idempotent + in fstab.
    script = _script()
    assert "mkswap /swapfile" in script
    assert "swapon /swapfile" in script
    assert "32G" in script
    assert "/swapfile none swap sw 0 0" in script


def test_prep_script_retires_per_disk_swap_partitions() -> None:
    # The OS-install per-disk swap partitions sit OUTSIDE the md RAID mirrors, so a
    # single disk death loses their swapped-out pages and slowly SIGBUS-kills every
    # process on the box (the 2026-08-07 production nvme incident). Prep must turn
    # them off, drop them from fstab (keeping the mirrored swapfile's own line),
    # and wipe their swap signatures so nothing re-activates them at boot.
    script = _script()
    assert 'swapoff "$swap_partition"' in script
    assert 'awk \'!($3 == "swap" && $1 != "/swapfile")\' /etc/fstab' in script
    assert 'wipefs -a "$swap_partition"' in script
    # The partition swapoff loop only ever targets block devices, never the
    # swapfile it just enabled.
    assert "grep '^/dev/'" in script


def test_prep_script_fstab_filter_drops_partition_swap_and_keeps_everything_else(tmp_path: Path) -> None:
    # Execute the script's own fstab-filtering awk (extracted verbatim, not
    # re-typed) against a realistic OVH fstab: the two UUID swap-partition lines
    # must go, while the root/boot mounts, comments, and the mirrored swapfile
    # line all survive byte-for-byte.
    script = _script()
    awk_line = next(line for line in script.splitlines() if line.startswith("awk "))
    awk_command = awk_line.split(" /etc/fstab")[0]
    fstab = tmp_path / "fstab"
    kept_lines = [
        "# /etc/fstab: static file system information.",
        "UUID=aaaa-root\t/\text4\terrors=remount-ro\t0\t1",
        "UUID=bbbb-boot\t/boot\text4\tdefaults\t0\t2",
        "/swapfile none swap sw 0 0",
    ]
    dropped_lines = [
        "UUID=cccc-swap0\tswap\tswap\tdefaults\t0\t0",
        "UUID=dddd-swap1\tswap\tswap\tdefaults\t0\t0",
    ]
    fstab.write_text("\n".join(kept_lines[:3] + dropped_lines + kept_lines[3:]) + "\n")
    result = subprocess.run(
        ["bash", "-c", f"{awk_command} {fstab}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "\n".join(kept_lines) + "\n"


def test_prep_script_retires_swap_partitions_after_enabling_the_swapfile() -> None:
    # Order matters: the mirrored swapfile must be active before the partitions
    # are swapped off, so their in-use pages migrate somewhere durable instead of
    # competing for RAM on a box running near capacity.
    script = _script()
    assert script.index("swapon /swapfile") < script.index('swapoff "$swap_partition"')


def test_prep_script_pins_unattended_upgrades_to_never_auto_reboot() -> None:
    # Slice boxes host user workspaces; an unattended-upgrades auto-reboot would
    # take every slice on the box down unannounced (and nothing restarts them).
    # The Debian default is already "false", but prep pins it explicitly so config
    # drift cannot flip it.
    script = _script()
    assert "/etc/apt/apt.conf.d/99mngr-no-auto-reboot" in script
    assert 'Unattended-Upgrade::Automatic-Reboot "false";' in script


def test_prep_script_installs_boot_autostart_for_slice_vms() -> None:
    # After a box reboot nothing else restarts the lima slice VMs (no linger, no
    # lima boot integration), so prep installs a boot unit that starts them all.
    # It must run as the lima user (lima refuses root) and be enabled, not
    # started: prep runs on live boxes where the VMs are already up or were
    # stopped on purpose.
    script = _script()
    assert "/usr/local/sbin/mngr-slices-autostart.sh" in script
    assert "/etc/systemd/system/mngr-slices-autostart.service" in script
    assert "User=limahost" in script
    assert "WantedBy=multi-user.target" in script
    assert "systemctl enable mngr-slices-autostart.service" in script
    assert "systemctl start mngr-slices-autostart" not in script


def test_prep_script_autostart_targets_only_stopped_slice_instances() -> None:
    # The boot script must start every stopped slice VM (leased or available)
    # and nothing else: not running instances (re-running the unit must be a
    # no-op) and not non-slice VMs someone parked on the box.
    script = _script()
    assert "limactl list --format '{{.Name}} {{.Status}}'" in script
    assert 'awk -v prefix="mngr-slice-" \'index($1, prefix) == 1 && $2 == "Stopped" {print $1}\'' in script


def test_prep_script_autostart_bounds_parallelism_and_retries() -> None:
    # A full box cold-booting 14 QEMU VMs at once is a boot storm, and fully
    # serial keeps users down for ~10 minutes, so starts are capped at a small
    # concurrency. Each instance gets one retry; a VM that still fails must fail
    # the unit (xargs propagates the failure) so the breakage is visible in
    # systemd rather than swallowed.
    script = _script()
    assert "xargs -n1 -P 4" in script
    assert "retrying" in script
    autostart_body = script[script.index("MNGR_SLICES_AUTOSTART") : script.rindex("MNGR_SLICES_AUTOSTART")]
    assert "|| true" not in autostart_body
    # A failing `limactl list` must fail the unit too, not read as "no VMs to
    # start": errexit+pipefail with no stderr suppression on the listing.
    assert "set -euo pipefail" in autostart_body
    assert "2>/dev/null" not in autostart_body


def test_prep_script_installs_libguestfs_for_image_customization() -> None:
    # virt-customize (from libguestfs-tools) is how we pre-install Docker + inotify
    # into the golden image; it must be among the box apt packages.
    script = _script()
    assert "libguestfs-tools" in script


def test_prep_script_preinstalls_pinned_docker_and_inotify_into_golden_image() -> None:
    script = _script()
    # The image is customized offline with virt-customize over the network, running an
    # in-guest script that installs the SAME pinned Docker the OVH path pins, plus
    # inotify-tools -- so each slice VM's first-boot guards (presence-only) skip them.
    assert "virt-customize -a" in script
    assert "--network" in script
    assert "--run /tmp/mngr-slice-image-customize.sh" in script
    assert f'docker-ce="{PINNED_DOCKER_APT_VERSION}"' in script
    assert "download.docker.com/linux/debian" in script
    assert "inotify-tools" in script


def test_prep_script_customizes_before_atomic_publish() -> None:
    # The customize must run on the temp copy and only move it into place on success,
    # so a partial/failed customize never becomes the staged base image.
    script = _script()
    customize_idx = script.index("virt-customize -a")
    publish_idx = script.index('mv "$img.tmp" "$img"')
    assert customize_idx < publish_idx
    # The finished image is chowned to the lima user that limactl reads it as.
    assert 'chown limahost:limahost "$img.tmp"' in script
