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
        lima_version="2.1.2",
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
    assert "lima-2.1.2-Linux-x86_64.tar.gz" in script
    assert "github.com/lima-vm/lima/releases/download/v2.1.2/" in script


def test_prep_script_never_invokes_limactl_as_root() -> None:
    # The script runs as root; limactl refuses to run as root, so it must only be
    # extracted, never executed, here.
    script = _script()
    assert "tar -C /usr/local" in script
    assert "limactl --version" not in script
    assert "limactl start" not in script


def test_prep_script_creates_service_user_with_kvm_and_pool_key() -> None:
    script = _script()
    assert "useradd -m -s /bin/bash limahost" in script
    assert "usermod -aG kvm limahost" in script
    assert _POOL_PUB in script
    assert "/home/limahost/.ssh/authorized_keys" in script


def test_prep_script_is_idempotent_guarded() -> None:
    script = _script()
    # Re-runnable: guards on existing limactl and existing user.
    assert "command -v limactl >/dev/null 2>&1" in script
    assert "id limahost >/dev/null 2>&1" in script


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
