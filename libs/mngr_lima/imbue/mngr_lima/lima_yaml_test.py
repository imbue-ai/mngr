import subprocess
from pathlib import Path

import pytest
import yaml

from imbue.mngr.errors import MngrError
from imbue.mngr_lima.constants import lima_host_data_disk_label
from imbue.mngr_lima.lima_yaml import generate_default_lima_yaml
from imbue.mngr_lima.lima_yaml import load_user_lima_yaml
from imbue.mngr_lima.lima_yaml import merge_lima_yaml
from imbue.mngr_lima.lima_yaml import parse_build_args_for_yaml_path
from imbue.mngr_lima.lima_yaml import patch_root_authorized_keys_block_in_lima_yaml
from imbue.mngr_lima.lima_yaml import write_lima_yaml

# Independently spelled out (rather than imported from production) so the
# assertions still document the expected shape of the disabled-port-forwards
# rules rather than tautologically echoing the helper.
_EXPECTED_DISABLED_PORT_FORWARDS = [
    {
        "guestIPMustBeZero": True,
        "guestIP": "0.0.0.0",
        "proto": "any",
        "guestPortRange": [1, 65535],
        "ignore": True,
    },
    {
        "guestIP": "127.0.0.1",
        "proto": "any",
        "guestPortRange": [1, 65535],
        "ignore": True,
    },
]


def test_generate_default_lima_yaml(tmp_path: Path) -> None:
    volume_path = tmp_path / "volume"
    volume_path.mkdir()

    config = generate_default_lima_yaml(
        volume_host_path=volume_path,
        host_dir="/mngr",
    )

    assert "images" in config
    assert len(config["images"]) == 1
    assert "location" in config["images"][0]
    assert "arch" in config["images"][0]

    assert "mounts" in config
    assert len(config["mounts"]) == 1
    assert config["mounts"][0]["mountPoint"] == "/mngr"
    assert config["mounts"][0]["writable"] is True

    assert "provision" in config
    assert len(config["provision"]) == 1
    assert config["provision"][0]["mode"] == "system"

    assert config["portForwards"] == _EXPECTED_DISABLED_PORT_FORWARDS


def test_generate_default_lima_yaml_custom_image(tmp_path: Path) -> None:
    volume_path = tmp_path / "volume"
    volume_path.mkdir()

    config = generate_default_lima_yaml(
        volume_host_path=volume_path,
        host_dir="/mngr",
        custom_image_url="https://example.com/custom.qcow2",
    )

    assert config["images"][0]["location"] == "https://example.com/custom.qcow2"


def test_generate_default_lima_yaml_without_host_key_omits_key_block(tmp_path: Path) -> None:
    """When the optional keypair parameters are omitted, the provision script
    must NOT write any /etc/ssh/ssh_host_* file -- the helper's default leaves
    the guest's own host key untouched."""
    volume_path = tmp_path / "volume"
    volume_path.mkdir()
    config = generate_default_lima_yaml(volume_host_path=volume_path, host_dir="/mngr")
    script = config["provision"][0]["script"]
    assert "/etc/ssh/ssh_host_ed25519_key" not in script
    assert "MNGR_LIMA_HOST_PRIV_KEY" not in script


def test_generate_default_lima_yaml_with_host_key_injects_block(tmp_path: Path) -> None:
    """When a keypair is provided, the provision script must include both the
    private-key heredoc and the public-key heredoc, remove rsa/ecdsa keys, and
    trigger an sshd restart via SSH_KEY_CHANGED=1."""
    volume_path = tmp_path / "volume"
    volume_path.mkdir()
    fake_private = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC...\n-----END OPENSSH PRIVATE KEY-----\n"
    fake_public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPv... mngr-lima@host\n"
    config = generate_default_lima_yaml(
        volume_host_path=volume_path,
        host_dir="/mngr",
        host_private_key_pem=fake_private,
        host_public_key_openssh=fake_public,
    )
    script = config["provision"][0]["script"]
    # Both heredocs land in the script.
    assert "BEGIN OPENSSH PRIVATE KEY" in script
    assert "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPv" in script
    # The script removes other key types so sshd only presents our ed25519.
    assert "rm -f /etc/ssh/ssh_host_rsa_key" in script
    assert "rm -f /etc/ssh/ssh_host_ecdsa_key" in script
    # And flags the swap so the trailing restart fires.
    assert "SSH_KEY_CHANGED=1" in script


def test_provision_script_installs_host_key_before_apt(tmp_path: Path) -> None:
    """Host-key trust must be established before the network-dependent apt
    install: mngr pins the injected key and connects with strict host-key
    checking, so a transient apt mirror failure (which aborts the `set -e`
    script) must not be able to run *before* the key swap and leave the VM on
    its default keys -- that surfaces as a baffling "host key does not match".
    The package install is also wrapped in a retry to ride out mirror blips.
    """
    volume_path = tmp_path / "volume"
    volume_path.mkdir()
    fake_private = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC...\n-----END OPENSSH PRIVATE KEY-----\n"
    fake_public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPv... mngr-lima@host\n"
    config = generate_default_lima_yaml(
        volume_host_path=volume_path,
        host_dir="/mngr",
        host_private_key_pem=fake_private,
        host_public_key_openssh=fake_public,
    )
    script = config["provision"][0]["script"]
    # The host-key swap (and its sshd restart) must precede the apt install.
    key_swap_index = script.index("/etc/ssh/ssh_host_ed25519_key")
    apt_install_index = script.index("apt_get_retry install")
    assert key_swap_index < apt_install_index
    # apt is retried rather than run once, so a transient mirror failure does
    # not abort provisioning on the first attempt.
    assert "apt_get_retry update" in script
    assert "apt_get_retry install" in script


def test_write_lima_yaml(tmp_path: Path) -> None:
    config = {"images": [{"location": "test.qcow2", "arch": "x86_64"}]}
    output_path = tmp_path / "test.yaml"
    result = write_lima_yaml(config, output_path)
    assert result == output_path
    assert output_path.exists()
    content = output_path.read_text()
    assert "test.qcow2" in content


def test_write_lima_yaml_temp_file() -> None:
    config = {"images": [{"location": "test.qcow2"}]}
    result = write_lima_yaml(config)
    assert result.exists()
    assert result.suffix == ".yaml"
    # Clean up
    result.unlink()


def test_load_user_lima_yaml(tmp_path: Path) -> None:
    yaml_path = tmp_path / "user.yaml"
    yaml_path.write_text("cpus: 8\nmemory: 16GiB\n")
    config = load_user_lima_yaml(yaml_path)
    assert config["cpus"] == 8
    assert config["memory"] == "16GiB"


def test_merge_lima_yaml() -> None:
    base = {"images": [{"location": "default.qcow2"}], "cpus": 4}
    override = {"cpus": 8, "memory": "16GiB"}
    merged = merge_lima_yaml(base, override)
    assert merged["cpus"] == 8
    assert merged["memory"] == "16GiB"
    assert merged["images"] == [{"location": "default.qcow2"}]


def test_merge_lima_yaml_extends_provision_and_mounts_replaces_images() -> None:
    # provision: a user-supplied list must not silently drop mngr's host-key
    # injection. mngr's entries come first so its provision script runs before
    # any user script (Lima executes provision[mode=system] in list order).
    base = {"provision": [{"mode": "system", "script": "MNGR_HOST_KEY_INJECTION"}]}
    override = {"provision": [{"mode": "system", "script": "apt-get install -y postgres"}]}
    merged = merge_lima_yaml(base, override)
    assert len(merged["provision"]) == 2
    assert merged["provision"][0]["script"] == "MNGR_HOST_KEY_INJECTION"
    assert merged["provision"][1]["script"] == "apt-get install -y postgres"

    # mounts: extend with base first; mngr's /mngr mount must survive.
    base = {"mounts": [{"location": "/host/vol", "mountPoint": "/mngr", "writable": True}]}
    override = {"mounts": [{"location": "/host/data", "mountPoint": "/data", "writable": False}]}
    merged = merge_lima_yaml(base, override)
    assert len(merged["mounts"]) == 2
    assert merged["mounts"][0]["mountPoint"] == "/mngr"
    assert merged["mounts"][1]["mountPoint"] == "/data"

    # images: a user supplying images: clearly means to override -- still replace.
    base = {"images": [{"location": "default.qcow2"}]}
    override = {"images": [{"location": "custom.qcow2"}]}
    merged = merge_lima_yaml(base, override)
    assert merged["images"] == [{"location": "custom.qcow2"}]


def test_merge_lima_yaml_forces_port_forwards_disabled() -> None:
    base = {"portForwards": _EXPECTED_DISABLED_PORT_FORWARDS, "cpus": 4}
    user_override = {"portForwards": [{"guestPort": 8082, "hostPort": 8082}], "cpus": 8}
    merged = merge_lima_yaml(base, user_override)
    assert merged["cpus"] == 8
    assert merged["portForwards"] == _EXPECTED_DISABLED_PORT_FORWARDS


def test_generate_default_lima_yaml_bind_mount_mode_omits_additional_disks(tmp_path: Path) -> None:
    """Today's default (is_host_data_volume_exposed=True equivalent): the YAML has
    a 9p mount and no additionalDisks; the provisioning script does not contain
    the host-data-disk block."""
    volume_path = tmp_path / "volume"
    volume_path.mkdir()
    config = generate_default_lima_yaml(volume_host_path=volume_path, host_dir="/mngr")
    assert "additionalDisks" not in config
    assert "mounts" in config and len(config["mounts"]) == 1
    assert config["mounts"][0]["mountPoint"] == "/mngr"
    script = config["provision"][0]["script"]
    assert "/mnt/lima-" not in script
    assert "ln -sfn" not in script


def test_generate_default_lima_yaml_btrfs_mode_omits_mounts_adds_disk(tmp_path: Path) -> None:
    """When host_data_disk_name is set and volume_host_path is None, the YAML
    omits the `mounts:` block entirely, attaches a btrfs additionalDisk with
    format: true, and the provisioning script symlinks host_dir to Lima's
    auto-mount path for that disk."""
    del tmp_path
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc123-data",
        host_data_disk_size="100GiB",
    )
    assert "mounts" not in config
    assert "additionalDisks" in config
    assert len(config["additionalDisks"]) == 1
    disk_entry = config["additionalDisks"][0]
    assert disk_entry["name"] == "mngr-abc123-data"
    assert disk_entry["format"] is True
    assert disk_entry["fsType"] == "btrfs"
    assert disk_entry["size"] == "100GiB"

    script = config["provision"][0]["script"]
    # The symlink target is the disk's canonical mount path.
    assert "ln -sfn /mnt/lima-mngr-abc123-data /mngr" in script
    # We format + mount the disk ourselves (Lima can't on minimal images that lack
    # mkfs.btrfs): btrfs-progs is installed, the disk is formatted, and mounted at
    # the canonical path before host_dir is symlinked into it.
    assert "btrfs-progs" in script
    assert "mkfs.btrfs -f" in script
    assert "mountpoint -q /mnt/lima-mngr-abc123-data" in script
    # Opens the btrfs root for the Lima default non-root user (fresh mkfs.btrfs
    # leaves the root dir owned by root:root).
    assert "chmod 0777 /mnt/lima-mngr-abc123-data" in script
    # No intermediate bind-mount or fstab manipulation -- those caused
    # stacked-mount ordering quirks on reboot.
    assert "mount --bind" not in script
    assert "/etc/fstab" not in script


def test_generate_default_lima_yaml_disk_name_without_size_raises(tmp_path: Path) -> None:
    """host_data_disk_size is required whenever a disk name is set; the helper
    raises MngrError rather than silently producing a malformed YAML."""
    volume_path = tmp_path / "volume"
    volume_path.mkdir()
    with pytest.raises(MngrError):
        generate_default_lima_yaml(
            volume_host_path=volume_path,
            host_dir="/mngr",
            host_data_disk_name="mngr-abc-data",
            host_data_disk_size=None,
        )


def test_merge_lima_yaml_additional_disks_extends() -> None:
    """A user --file YAML adding its own additionalDisks must not silently drop
    mngr's btrfs host-data disk. _LIST_EXTEND_KEYS makes the merge concatenate
    rather than replace."""
    base = {"additionalDisks": [{"name": "mngr-host-data", "format": True, "fsType": "btrfs", "size": "100GiB"}]}
    override = {"additionalDisks": [{"name": "user-extra", "format": True, "fsType": "ext4", "size": "20GiB"}]}
    merged = merge_lima_yaml(base, override)
    assert len(merged["additionalDisks"]) == 2
    assert merged["additionalDisks"][0]["name"] == "mngr-host-data"
    assert merged["additionalDisks"][1]["name"] == "user-extra"


def test_parse_build_args_for_yaml_path() -> None:
    assert parse_build_args_for_yaml_path(("--file", "/path/to/config.yaml")) == Path("/path/to/config.yaml")
    assert parse_build_args_for_yaml_path(("--file=/path/to/config.yaml",)) == Path("/path/to/config.yaml")
    assert parse_build_args_for_yaml_path(("--other", "arg")) is None
    assert parse_build_args_for_yaml_path(()) is None


def test_generate_default_lima_yaml_without_root_key_omits_root_login() -> None:
    """Without root_authorized_public_key, the provisioning script must not enable
    root login or authorize a root key -- the default non-root path is untouched."""
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc-data",
        host_data_disk_size="100GiB",
    )
    script = config["provision"][0]["script"]
    assert "PermitRootLogin" not in script
    assert "/root/.ssh/authorized_keys" not in script


def test_generate_default_lima_yaml_with_root_key_enables_root_login() -> None:
    """When a root client key is provided, the provisioning script enables
    key-based root login and authorizes that key for root."""
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc-data",
        host_data_disk_size="100GiB",
        root_authorized_public_key="ssh-ed25519 AAAAROOTKEY mngr-lima-root",
    )
    script = config["provision"][0]["script"]
    assert "PermitRootLogin prohibit-password" in script
    assert "/root/.ssh/authorized_keys" in script
    assert "ssh-ed25519 AAAAROOTKEY mngr-lima-root" in script
    # The btrfs disk is still formatted + mounted; root mode doesn't change that.
    assert "mkfs.btrfs -f" in script
    assert "ln -sfn /mnt/lima-mngr-abc-data /mngr" in script


_ROOT_KEY = "ssh-ed25519 AAAAROOTKEY mngr-lima-root"
_FOREIGN_KEY = "ssh-rsa AAAALEASEDKEY someone-elses-lease"


def _root_key_block(root_authorized_public_key: str) -> str:
    """Slice the root-authorized-keys step out of the generated provisioning script.

    The surrounding script cannot run in a test (apt-get, mkfs.btrfs, mount), but
    this step can, so the assertions below exercise the real generated bash rather
    than matching substrings of it.
    """
    script = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc-data",
        host_data_disk_size="100GiB",
        root_authorized_public_key=root_authorized_public_key,
    )["provision"][0]["script"]
    end_marker = "chown -R root:root /root/.ssh"
    start = script.index("mkdir -p /root/.ssh")
    return script[start : script.index(end_marker) + len(end_marker)]


def _run_root_key_block(block: str, ssh_dir: Path) -> None:
    """Run the block unprivileged against ``ssh_dir`` in place of ``/root/.ssh``.

    Two concessions let it run as a normal user: the path is redirected and
    ``chown`` is stubbed out. The shell quoting, the already-present guard and the
    trailing-newline guard -- the parts that can actually be wrong -- are run
    verbatim as the VM runs them.
    """
    subprocess.run(
        ["bash", "-c", "set -eu\nchown() { :; }\n" + block.replace("/root/.ssh", str(ssh_dir))],
        check=True,
    )


def test_root_key_block_preserves_keys_added_after_the_carve(tmp_path: Path) -> None:
    """Lima replays provisioning on every VM start (a start regenerates cidata with a
    fresh cloud-init instance-id), so this step must add its own line and leave the
    rest of authorized_keys alone. It used to truncate the file, which silently
    dropped the imbue_cloud connector's lease-time injection of the owner's key on
    the first restart after their lease -- locking them out of the VM for good while
    the workspace kept working through the container's separate sshd."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    authorized_keys = ssh_dir / "authorized_keys"
    authorized_keys.write_text(f"{_FOREIGN_KEY}\n")

    _run_root_key_block(_root_key_block(_ROOT_KEY), ssh_dir)

    assert authorized_keys.read_text().splitlines() == [_FOREIGN_KEY, _ROOT_KEY]


def test_root_key_block_is_idempotent_across_restarts(tmp_path: Path) -> None:
    """Re-running must not accumulate duplicate lines -- provisioning replays on every start."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    block = _root_key_block(_ROOT_KEY)

    _run_root_key_block(block, ssh_dir)
    _run_root_key_block(block, ssh_dir)

    assert (ssh_dir / "authorized_keys").read_text().splitlines() == [_ROOT_KEY]


def test_root_key_block_does_not_corrupt_a_file_lacking_a_trailing_newline(tmp_path: Path) -> None:
    """Appending to a file whose last line has no newline would otherwise fuse both keys
    onto one line, breaking the existing key as well as ours."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    authorized_keys = ssh_dir / "authorized_keys"
    authorized_keys.write_text(_FOREIGN_KEY)

    _run_root_key_block(_root_key_block(_ROOT_KEY), ssh_dir)

    assert authorized_keys.read_text().splitlines() == [_FOREIGN_KEY, _ROOT_KEY]


def _revert_root_key_block_to_truncating_form(script: str, key: str) -> str:
    """Swap the generated appending root-key step back to the historical truncating one."""
    truncating_block = f"""\
mkdir -p /root/.ssh
chmod 700 /root/.ssh
cat > /root/.ssh/authorized_keys <<'MNGR_LIMA_ROOT_KEY'
{key}
MNGR_LIMA_ROOT_KEY
chmod 600 /root/.ssh/authorized_keys
chown -R root:root /root/.ssh"""
    start = script.index("mkdir -p /root/.ssh")
    end = script.index("chown -R root:root /root/.ssh") + len("chown -R root:root /root/.ssh")
    return script[:start] + truncating_block + script[end:]


def test_patch_root_authorized_keys_block_fixes_a_pre_fix_config_idempotently() -> None:
    """A stored lima.yaml carrying the historical truncating root-key step is rewritten
    to the appending form with the same key; a config already in that form is left alone."""
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc-data",
        host_data_disk_size="100GiB",
        root_authorized_public_key=_ROOT_KEY,
    )
    pre_fix_provision = [
        {"mode": "system", "script": _revert_root_key_block_to_truncating_form(entry["script"], _ROOT_KEY)}
        for entry in config["provision"]
    ]
    pre_fix_text = yaml.dump({**config, "provision": pre_fix_provision}, default_flow_style=False, sort_keys=False)

    patched_text = patch_root_authorized_keys_block_in_lima_yaml(pre_fix_text)

    assert patched_text is not None
    patched_script = yaml.safe_load(patched_text)["provision"][0]["script"]
    assert "cat > /root/.ssh/authorized_keys" not in patched_script
    assert f"grep -qxF '{_ROOT_KEY}'" in patched_script
    assert patch_root_authorized_keys_block_in_lima_yaml(patched_text) is None


def test_patch_root_authorized_keys_block_ignores_unrelated_yaml() -> None:
    assert patch_root_authorized_keys_block_in_lima_yaml("- just\n- a\n- list\n") is None
    assert patch_root_authorized_keys_block_in_lima_yaml("images: []\n") is None


def test_generate_default_lima_yaml_volume_home_path_symlinks_home_not_host_dir() -> None:
    """With volume_home_path set, the provisioning script symlinks the home path to
    the disk and mkdir's host_dir inside it, instead of symlinking host_dir."""
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/home/user/.mngr",
        host_data_disk_name="mngr-abc123-data",
        host_data_disk_size="100GiB",
        volume_home_path="/home/user",
    )
    script = config["provision"][0]["script"]
    assert "ln -sfn /mnt/lima-mngr-abc123-data /home/user" in script
    assert "ln -sfn /mnt/lima-mngr-abc123-data /home/user/.mngr" not in script
    assert "mkdir -p /home/user/.mngr" in script


def test_generate_default_lima_yaml_without_volume_home_path_symlinks_host_dir() -> None:
    """The default (no volume_home_path) keeps today's host_dir symlink and adds no mkdir."""
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc123-data",
        host_data_disk_size="100GiB",
    )
    script = config["provision"][0]["script"]
    assert "ln -sfn /mnt/lima-mngr-abc123-data /mngr" in script
    assert "mkdir -p /mngr\n" not in script


def test_provision_script_labels_data_disk_for_lima(tmp_path: Path) -> None:
    """The in-guest format applies the exact filesystem label Lima's boot script
    probes for, and heals unlabeled disks formatted before the label existed --
    otherwise Lima re-enters its first-time disk setup (sfdisk + failing mkfs)
    on every boot and cloud-final fails every boot."""
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc123-data",
        host_data_disk_size="100GiB",
    )
    script = config["provision"][0]["script"]

    assert "mkfs.btrfs -f -L lima-mngr-abc123-data" in script
    assert "btrfs filesystem label /mnt/lima-mngr-abc123-data lima-mngr-abc123-data" in script


def test_lima_host_data_disk_label_matches_lima_expectation() -> None:
    assert lima_host_data_disk_label("mngr-abc-data") == "lima-mngr-abc-data"


def test_provision_script_disables_per_source_penalties_only_when_supported() -> None:
    """The provision script must turn off sshd's per-source penalties on guests that support them.

    Under qemu user-mode networking every host connection shares one NAT source
    address, so OpenSSH >= 9.8's default penalties lock out the whole host after
    a single grace-timeout. The keyword is fatal to older sshds, so the write
    must be gated on the running sshd actually understanding it.
    """
    config = generate_default_lima_yaml(volume_host_path=None, host_dir="/mngr")
    script = config["provision"][0]["script"]
    assert "PerSourcePenalties no" in script
    assert "sshd -T 2>/dev/null | grep -qi '^persourcepenalties'" in script
