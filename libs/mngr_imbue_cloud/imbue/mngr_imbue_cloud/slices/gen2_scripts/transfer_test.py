import base64
import hashlib
import os
import subprocess
from pathlib import Path

from inline_snapshot import snapshot

from imbue.imbue_common.model_update import to_update
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import GEN2_CIDATA_FILE_NAMES
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import Gen2SliceCidata
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_CONTAINER_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_DISK_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_UNITS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.testing import assert_valid_bash
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import GEN2_RESIZE_APPLIED_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import RESTORE_NO_PORTS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import RESTORE_NO_SPACE_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import RESTORE_RESERVED_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import TRANSFER_DIR_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import TransferEnv
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import build_is_transfer_alive_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import build_launch_detached_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import build_read_status_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import parse_gen2_resize_applied_line
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import parse_gen2_restore_reserved_line
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import parse_status_text
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_gen2_download_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_gen2_resize_in_place_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_gen2_restore_reserve_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_transfer_env
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import script_prelude
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import transfer_dir

_INSTANCE = "mngr-slice-test-" + "a" * 32


def _render_reserve_script(cidata: Gen2SliceCidata | None = None) -> str:
    payload = base64.b64encode(b"payload").decode()
    return render_gen2_restore_reserve_script(
        instance_name=_INSTANCE,
        units=8,
        data_disk_gib=28,
        unit_budget_mib=120 * 1024,
        disk_budget_gib=400,
        required_free_bytes=100 * 1024**3,
        expected_meta_sha="cc33",
        env_template_b64=payload,
        cidata=cidata,
    )


def test_transfer_env_quotes_every_value_and_omits_an_empty_identity() -> None:
    env = TransferEnv(
        s3_endpoint="https://s3.example",
        s3_region="auto",
        access_key_id="AKIA",
        secret_access_key="se'cret",
        bucket="bucket",
        key_prefix="host-abc/gen-2",
        instance_name=_INSTANCE,
        age_recipient="age1recipient",
    )
    rendered = render_transfer_env(env)
    assert "export AWS_SECRET_ACCESS_KEY='se'\"'\"'cret'\n" in rendered
    assert "export WS_KEY_PREFIX=host-abc/gen-2\n" in rendered
    assert "WS_AGE_IDENTITY" not in rendered
    with_identity = render_transfer_env(
        env.model_copy_update(to_update(env.field_ref().age_identity, "AGE-SECRET-KEY-1"))
    )
    assert "export WS_AGE_IDENTITY=AGE-SECRET-KEY-1\n" in with_identity


def test_script_prelude_sources_the_env_and_publishes_status_atomically() -> None:
    prelude = script_prelude(_INSTANCE)
    assert_valid_bash(prelude)
    assert f'TD="$HOME/{TRANSFER_DIR_ROOT}/{_INSTANCE}"' in prelude
    assert '. "$TD/env"' in prelude
    assert 'mv "$STATUS.tmp" "$STATUS"' in prelude
    assert "trap 'fail \"command failed: $BASH_COMMAND\"' ERR" in prelude


def test_detached_launch_clears_the_stale_status_before_backgrounding() -> None:
    command = build_launch_detached_command(_INSTANCE, "upload.sh")
    assert_valid_bash(command)
    assert command.index("rm -f status") < command.index("setsid nohup bash upload.sh")
    assert f'echo $! > "{transfer_dir(_INSTANCE)}/pid"' in command
    assert_valid_bash(build_is_transfer_alive_command(_INSTANCE))
    assert build_read_status_command(_INSTANCE).endswith("|| true")


def test_parse_status_text_reads_the_flat_key_value_file() -> None:
    assert parse_status_text("STAGE=uploaded\n\nFINISHED=1\nSHA_DISK=abc=def\nnoise\n") == {
        "STAGE": "uploaded",
        "FINISHED": "1",
        "SHA_DISK": "abc=def",
    }


def test_gen2_restore_reserve_claims_budgets_ports_and_ordinal_without_enabling() -> None:
    script = _render_reserve_script()
    assert_valid_bash(script)
    # The two-budget capacity accounting replaced the slot-count guard.
    assert GEN2_NO_UNITS_MARKER in script
    assert GEN2_NO_DISK_MARKER in script
    assert "MNGR_RESTORE_BOX_FULL" not in script
    assert RESTORE_NO_PORTS_MARKER in script
    assert RESTORE_NO_SPACE_MARKER in script
    assert RESTORE_RESERVED_MARKER in script
    # The artifact's own three cidata files are copied verbatim (the
    # user-data carries the VM's pinned host key, the meta-data its stable
    # instance-id, the network-config is DHCP): nothing is rendered for the
    # new placement, so cloud-init never reruns there.
    for name in GEN2_CIDATA_FILE_NAMES:
        assert f'[ -f "$TD/meta/$WS_INSTANCE/{name}" ] || fail' in script
        assert f'cp "$TD/meta/$WS_INSTANCE/{name}" "$slice_dir/{name}"' in script
    assert "genisoimage" in script
    assert 'meta-data" | base64 -d' not in script
    assert 'substitute_ordinal_tokens > "$slice_dir/meta-data"' not in script
    assert 'substitute_ordinal_tokens > "$slice_dir/network-config"' not in script
    # The unit must NOT be enabled at reserve time: a box reboot must never
    # boot a half-restored VM (the download script enables after the disks land).
    assert "systemctl enable" not in script
    # Ports and the ordinal-derived values are substituted into the single env
    # template under the lock.
    assert GEN2_VM_SSH_PORT_PLACEHOLDER in script
    assert GEN2_CONTAINER_SSH_PORT_PLACEHOLDER in script
    assert "substitute_ordinal_tokens" in script


def test_gen2_restore_reserve_default_path_is_byte_identical_to_the_pinned_renderer() -> None:
    # The connector's restore depends on this exact script; the migrate's
    # opt-in parameter (a supplied cidata) must leave the default rendering
    # untouched.
    digest = hashlib.sha256(_render_reserve_script().encode()).hexdigest()
    assert digest == snapshot("bc1d252de1ea9a4adc05c487ad6a4c48fdabb211049dd1cf1493920b697eda69")


def test_gen2_restore_reserve_with_supplied_cidata_skips_the_meta_tar() -> None:
    cidata = Gen2SliceCidata(
        user_data="#cloud-config\nusers: []\n",
        meta_data="instance-id: mngr-slice-x\nlocal-hostname: mngr-slice-x\n",
        network_config="version: 2\n",
    )
    script = _render_reserve_script(cidata=cidata)
    assert_valid_bash(script)
    # The supplied files are written directly; nothing is fetched from S3.
    for name, content in cidata.content_by_file_name().items():
        encoded = base64.b64encode(content.encode()).decode()
        assert f'echo {encoded} | base64 -d > "$slice_dir/{name}"' in script
    assert "s5cmd" not in script
    assert "meta.enc" not in script
    assert "cc33" not in script
    assert 'rm -rf "$TD/meta"' not in script
    assert '"$TD/meta/$WS_INSTANCE/' not in script
    # The rest of the materialization (env template, cidata ISO, ordinal link) is unchanged.
    assert "substitute_ordinal_tokens" in script
    assert "genisoimage" in script
    assert 'ln -sfn "$slice_dir" "$BY_ORDINAL_DIR/$ordinal"' in script


def test_gen2_restore_reserve_reclaims_a_leftover_dir_before_the_capacity_guard() -> None:
    # A re-driven caller re-runs the reserve for the SAME instance name; a
    # leftover dir from the crashed attempt must be reclaimed (idempotent
    # unit stop/disable + ordinal-link and dir removal) under the lock BEFORE
    # the capacity guard, or the re-drive wedges on the mkdir and the stale
    # dir permanently eats the machine's budget share.
    script = _render_reserve_script()
    assert 'sudo /usr/bin/systemctl stop "mngr-slice@$stale_ordinal"' in script
    assert 'sudo /usr/bin/systemctl disable "mngr-slice@$stale_ordinal"' in script
    assert 'rm -rf "$slice_dir"' in script
    assert script.index('rm -rf "$slice_dir"') < script.index("# Two-budget capacity guard")


def test_gen2_download_script_verifies_shas_enables_and_waits() -> None:
    script = render_gen2_download_script(
        instance_name=_INSTANCE,
        ordinal=3,
        expected_sha_by_name={"DISK": "aa11", "DATADISK": "bb22", "META": "cc33"},
        vm_ssh_port=23000,
        container_ssh_port=23001,
        grow_data_disk_gib=None,
    )
    assert_valid_bash(script)
    assert "DISK aa11" in script
    assert "DATADISK bb22" in script
    assert 'sudo /usr/bin/systemctl enable "mngr-slice@3"' in script
    assert 'sudo /usr/bin/systemctl start "mngr-slice@3"' in script
    assert "wait_ssh 23000" in script
    assert "wait_ssh 23001" in script
    assert "flock 8" in script
    assert "qemu-img resize" not in script


def test_gen2_download_script_grows_the_data_disk_before_the_boot() -> None:
    script = render_gen2_download_script(
        instance_name=_INSTANCE,
        ordinal=3,
        expected_sha_by_name={"DISK": "aa11", "DATADISK": "bb22", "META": "cc33"},
        vm_ssh_port=23000,
        container_ssh_port=23001,
        grow_data_disk_gib=56,
    )
    assert_valid_bash(script)
    grow_line = 'qemu-img resize -q "$SLICE_DIR/datadisk.qcow2" 56G'
    assert grow_line in script
    # The grow happens after the download lands and before the unit boots.
    assert script.index("DATADISK bb22") < script.index(grow_line) < script.index("systemctl start")


def test_gen2_resize_in_place_rechecks_budgets_and_grows_the_disk() -> None:
    script = render_gen2_resize_in_place_script(
        instance_name=_INSTANCE,
        units=16,
        vcpus=4,
        data_disk_gib=56,
        total_units=120,
        unit_budget_mib=120 * 1024,
        disk_budget_gib=400,
        uplink_mbps=1000,
    )
    assert_valid_bash(script)
    # The budget re-check excludes this machine's own current footprint.
    assert f'[ "$(basename "$(dirname "$env_file")")" = {_INSTANCE} ] && continue' in script
    assert GEN2_NO_UNITS_MARKER in script
    assert GEN2_NO_DISK_MARKER in script
    # The env rewrite preserves the recorded placement + ports and is atomic.
    assert "substitute_ordinal_tokens" in script
    assert 'mv "$slice_dir/env.tmp" "$slice_dir/env"' in script
    # Grow-only: the qcow2 is resized only when the target exceeds its
    # current virtual size.
    assert 'qemu-img resize -q "$slice_dir/datadisk.qcow2" 56G' in script
    assert f"if [ {56 * 1024**3} -gt " in script
    assert GEN2_RESIZE_APPLIED_MARKER in script


def test_gen2_virtual_size_parse_reads_the_top_level_key_only(tmp_path: Path) -> None:
    # qemu 10's info JSON nests child (file protocol) nodes that carry their
    # own virtual-size (the FILE's byte length); a whole-document capture
    # yields several numbers, which makes an integer guard silently false.
    # Execute the rendered parse against a realistic stub to pin it.
    qemu_10_style_json = (
        '{"children": [{"name": "file", "info": {"virtual-size": 197120, "filename": "d.qcow2"}}], '
        '"virtual-size": 30064771072, "filename": "d.qcow2", "format": "qcow2"}'
    )
    stub = tmp_path / "qemu-img"
    stub.write_text(f"#!/bin/bash\ncat <<'EOF'\n{qemu_10_style_json}\nEOF\n")
    stub.chmod(0o755)
    script = render_gen2_resize_in_place_script(
        instance_name=_INSTANCE,
        units=16,
        vcpus=4,
        data_disk_gib=56,
        total_units=120,
        unit_budget_mib=122880,
        disk_budget_gib=765,
        uplink_mbps=1000,
    )
    parse_lines = [line for line in script.splitlines() if line.startswith("current_bytes=")]
    assert len(parse_lines) == 1
    probe = f'{parse_lines[0]}\necho "$current_bytes"\n'
    # The stub dir rides in front of the inherited PATH (the parse also needs
    # a resolvable python3, whose location varies across test environments).
    result = subprocess.run(
        ["bash", "-c", probe],
        capture_output=True,
        text=True,
        env={"PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}", "slice_dir": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "30064771072"


def test_parse_gen2_resize_applied_line_extracts_ports_and_ordinal() -> None:
    assert parse_gen2_resize_applied_line("noise\nMNGR_RESIZE_APPLIED 23000 23001 5\n") == snapshot((23000, 23001, 5))
    assert parse_gen2_resize_applied_line("MNGR_RESIZE_APPLIED 23000") is None
    assert parse_gen2_resize_applied_line("") is None


def test_parse_gen2_restore_reserved_line_extracts_ports_and_ordinal() -> None:
    assert parse_gen2_restore_reserved_line("noise\nMNGR_RESTORE_RESERVED 23000 23001 5\n") == snapshot(
        (23000, 23001, 5)
    )
    assert parse_gen2_restore_reserved_line("MNGR_RESTORE_RESERVED 23000 23001") is None
    assert parse_gen2_restore_reserved_line("") is None
