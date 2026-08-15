import yaml

from imbue.mngr_imbue_cloud.slices.key_repair import build_vm_root_key_repair_script
from imbue.mngr_imbue_cloud.slices.key_repair import parse_repair_script_output
from imbue.mngr_imbue_cloud.slices.lima_slice import build_slice_lima_yaml
from imbue.mngr_lima.lima_yaml import patch_root_authorized_keys_block_in_lima_yaml

_ROOT_KEY = "ssh-rsa AAAABAKEKEY slice-bake"
_POOL_KEY = "ssh-ed25519 AAAAPOOL pool-management"


def _pre_fix_root_key_block(key: str) -> str:
    """The truncating block the pre-fix generator wrote (reproduced verbatim, since the
    live generator now emits the appending form the patch must produce)."""
    return f"""\
mkdir -p /root/.ssh
chmod 700 /root/.ssh
cat > /root/.ssh/authorized_keys <<'MNGR_LIMA_ROOT_KEY'
{key}
MNGR_LIMA_ROOT_KEY
chmod 600 /root/.ssh/authorized_keys
chown -R root:root /root/.ssh"""


def _generate_pre_fix_slice_lima_yaml() -> str:
    """A slice lima.yaml as a pre-fix bake stored it on the box.

    Built with the real (fixed) generator, then the root-key step is reverted
    to the historical truncating form -- so the fixture tracks the surrounding
    provision script as it actually evolves.
    """
    config = build_slice_lima_yaml(
        host_dir="/mngr",
        vcpus=4,
        memory_mib=16384,
        disk_gib=100,
        boot_disk_gib=30,
        disk_name="mngr-slice-test-data",
        root_authorized_public_key=_ROOT_KEY,
        host_private_key_pem="-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----",
        host_public_key_openssh="ssh-ed25519 AAAAHOSTKEY vm-host",
        vm_ssh_host_port=22010,
        container_ssh_host_port=22011,
        extra_root_authorized_keys=(_POOL_KEY,),
    )
    fixed_scripts = [entry["script"] for entry in config["provision"]]
    appending_block_start = "MNGR_LIMA_AK=/root/.ssh/authorized_keys"
    reverted_provision = []
    for script in fixed_scripts:
        if appending_block_start in script and _POOL_KEY not in script:
            start = script.index("mkdir -p /root/.ssh")
            end = script.index("chown -R root:root /root/.ssh") + len("chown -R root:root /root/.ssh")
            reverted_provision.append(
                {"mode": "system", "script": script[:start] + _pre_fix_root_key_block(_ROOT_KEY) + script[end:]}
            )
        else:
            reverted_provision.append({"mode": "system", "script": script})
    reverted_config = {**config, "provision": reverted_provision}
    return yaml.dump(reverted_config, default_flow_style=False, sort_keys=False)


def test_patch_fixes_a_real_pre_fix_slice_config_and_keeps_everything_else() -> None:
    """End to end over a genuine slice config: the truncating step becomes the appending
    form with the same key, the pool-key step and every non-provision key survive."""
    pre_fix_text = _generate_pre_fix_slice_lima_yaml()
    original_config = yaml.safe_load(pre_fix_text)

    patched_text = patch_root_authorized_keys_block_in_lima_yaml(pre_fix_text)

    assert patched_text is not None
    patched_config = yaml.safe_load(patched_text)
    scripts = [entry["script"] for entry in patched_config["provision"]]
    joined = "\n".join(scripts)
    assert "cat > /root/.ssh/authorized_keys" not in joined
    assert f"grep -qxF '{_ROOT_KEY}'" in joined
    assert _POOL_KEY in joined
    for key in (k for k in original_config if k != "provision"):
        assert patched_config[key] == original_config[key]
    assert len(patched_config["provision"]) == len(original_config["provision"])
    # A second pass finds nothing to patch (idempotence across sweep re-runs).
    assert patch_root_authorized_keys_block_in_lima_yaml(patched_text) is None


def test_repair_script_copies_keys_and_reports_markers() -> None:
    script = build_vm_root_key_repair_script()
    assert "docker ps -aq --filter label=com.imbue.mngr.host-id" in script
    assert "docker cp" in script
    assert "grep -qxF" in script

    assert parse_repair_script_output("MNGR_KEY_REPAIR_OK added=2\n") == (
        True,
        "root authorized_keys re-asserted from the container copy (added=2)",
    )
    is_repaired, detail = parse_repair_script_output("MNGR_KEY_REPAIR_NO_CONTAINER\n")
    assert is_repaired and detail is not None and "no workspace container" in detail
    is_repaired_on_garbage, _garbage_detail = parse_repair_script_output("ssh: connection reset\n")
    assert not is_repaired_on_garbage
