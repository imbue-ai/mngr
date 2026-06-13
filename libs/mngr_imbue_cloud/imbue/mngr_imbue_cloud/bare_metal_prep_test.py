from imbue.mngr_imbue_cloud.bare_metal_prep import build_box_prep_script

_POOL_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTpoolkey mngr-pool"


def _script() -> str:
    return build_box_prep_script(pool_public_key=_POOL_PUB, lima_service_user="limahost", lima_version="2.1.2")


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
