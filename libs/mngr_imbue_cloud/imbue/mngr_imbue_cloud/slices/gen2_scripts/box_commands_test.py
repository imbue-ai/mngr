import os
import subprocess
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_destroy_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_list_instance_observations_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_list_instances_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_slice_env_file
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import parse_slice_instance_observations
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import render_gen2_budget_guard_lines
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import render_gen2_ordinal_derivation_lines
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import render_gen2_port_pick_lines
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import MalformedBoxOutputError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_PUBLISHED_PORTS_ENV_KEY
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePortDeclaration
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePortProtocol
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePublishedPort
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import render_published_ports_env_template_value
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import render_published_ports_env_value
from imbue.mngr_imbue_cloud.slices.gen2_scripts.testing import assert_valid_bash

_SCREEN_DECLARATIONS = (
    SlicePortDeclaration(guest_port=59000, protocol=SlicePortProtocol.UDP),
    SlicePortDeclaration(guest_port=59000, protocol=SlicePortProtocol.TCP),
    SlicePortDeclaration(guest_port=8080, protocol=SlicePortProtocol.TCP),
)


def test_env_file_carries_every_per_vm_value() -> None:
    env_text = build_qemu_slice_env_file(
        instance_name="mngr-slice-dev-x-abc",
        ordinal=3,
        vcpus=2,
        units=8,
        total_units=120,
        data_disk_gib=28,
        vm_ssh_host_port=22010,
        container_ssh_host_port=22011,
        uplink_mbps=1000,
        published_ports_value=render_published_ports_env_value(
            (
                SlicePublishedPort(guest_port=59000, protocol=SlicePortProtocol.UDP, host_port=22012),
                SlicePublishedPort(guest_port=59000, protocol=SlicePortProtocol.TCP, host_port=22012),
            )
        ),
    )
    assert env_text == snapshot(
        """\
MNGR_SLICE_INSTANCE=mngr-slice-dev-x-abc
MNGR_SLICE_ORDINAL=3
MNGR_SLICE_VCPUS=2
MNGR_SLICE_UNITS=8
MNGR_SLICE_TOTAL_UNITS=120
MNGR_SLICE_MEMORY_MIB=7680
MNGR_SLICE_DATA_DISK_GIB=28
MNGR_SLICE_MAC=52:54:00:6d:00:03
MNGR_SLICE_TAP=mslice3
MNGR_SLICE_USER=mngr-slice-3
MNGR_SLICE_VM_IP=10.201.0.14
MNGR_SLICE_GATEWAY_IP=10.201.0.13
MNGR_SLICE_PREFIX_LENGTH=30
MNGR_SLICE_VM_SSH_HOST_PORT=22010
MNGR_SLICE_CONTAINER_SSH_HOST_PORT=22011
MNGR_SLICE_UPLINK_MBPS=1000
MNGR_SLICE_PUBLISHED_PORTS=[{"guest_port":59000,"protocol":"udp","host_port":22012},{"guest_port":59000,"protocol":"tcp","host_port":22012}]
"""
    )


def test_env_file_with_no_uplink_leaves_the_shaping_value_empty() -> None:
    env_text = build_qemu_slice_env_file(
        instance_name="i",
        ordinal=0,
        vcpus=1,
        units=8,
        total_units=120,
        data_disk_gib=28,
        vm_ssh_host_port=22000,
        container_ssh_host_port=22001,
        uplink_mbps=None,
        published_ports_value=render_published_ports_env_value(()),
    )
    assert "MNGR_SLICE_UPLINK_MBPS=\n" in env_text
    assert "MNGR_SLICE_PUBLISHED_PORTS=[]\n" in env_text


def test_env_file_template_uses_placeholders_for_every_ordinal_derived_value() -> None:
    # ordinal=None renders the single on-box template: the ordinal and its
    # derived values (MAC, tap, user, /30 addresses) all become placeholder
    # tokens the box substitutes under the reservation lock, so ONE template
    # ships instead of a 512-entry payload table.
    template = build_qemu_slice_env_file(
        instance_name="mngr-slice-dev-x-abc",
        ordinal=None,
        vcpus=2,
        units=16,
        total_units=120,
        data_disk_gib=56,
        vm_ssh_host_port="__MNGR_VM_SSH_PORT__",
        container_ssh_host_port="__MNGR_CONTAINER_SSH_PORT__",
        uplink_mbps=None,
        published_ports_value=render_published_ports_env_template_value(_SCREEN_DECLARATIONS),
    )
    assert "MNGR_SLICE_ORDINAL=__MNGR_SLICE_ORDINAL__\n" in template
    # One placeholder per distinct guest port: both protocols of 59000 share token 0.
    assert (
        'MNGR_SLICE_PUBLISHED_PORTS=[{"guest_port":59000,"protocol":"udp","host_port":__MNGR_PUBLISHED_PORT_0__},'
        '{"guest_port":59000,"protocol":"tcp","host_port":__MNGR_PUBLISHED_PORT_0__},'
        '{"guest_port":8080,"protocol":"tcp","host_port":__MNGR_PUBLISHED_PORT_1__}]\n'
    ) in template
    assert "MNGR_SLICE_MAC=__MNGR_SLICE_MAC__\n" in template
    assert "MNGR_SLICE_TAP=mslice__MNGR_SLICE_ORDINAL__\n" in template
    assert "MNGR_SLICE_USER=mngr-slice-__MNGR_SLICE_ORDINAL__\n" in template
    assert "MNGR_SLICE_VM_IP=__MNGR_SLICE_VM_IP__\n" in template
    assert "MNGR_SLICE_GATEWAY_IP=__MNGR_SLICE_GATEWAY_IP__\n" in template
    assert "MNGR_SLICE_UNITS=16\n" in template
    assert "MNGR_SLICE_MEMORY_MIB=15872\n" in template
    assert "MNGR_SLICE_DATA_DISK_GIB=56\n" in template


def _run_port_pick(declarations: tuple[SlicePortDeclaration, ...], tmp_path: Path, fake_ss: str) -> str:
    """Run the port-pick block against a fake ``ss`` and a recorded env file; return the picked ports and the env rewrite."""
    instances_dir = tmp_path / "instances"
    recorded_env = instances_dir / "existing" / "env"
    recorded_env.parent.mkdir(parents=True)
    recorded_env.write_text(
        "MNGR_SLICE_VM_SSH_HOST_PORT=22000\nMNGR_SLICE_CONTAINER_SSH_HOST_PORT=22001\n"
        f'{GEN2_PUBLISHED_PORTS_ENV_KEY}=[{{"guest_port":1,"protocol":"tcp","host_port":22002}}]\n'
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ss_path = bin_dir / "ss"
    ss_path.write_text(fake_ss)
    ss_path.chmod(0o755)
    template = render_published_ports_env_template_value(declarations)
    script = (
        "set -euo pipefail\n"
        f'INSTANCES_DIR="{instances_dir}"\n'
        + render_gen2_port_pick_lines(
            port_range_start=22000,
            port_range_end=22010,
            no_ports_marker="NO_PORTS",
            published_port_declarations=declarations,
        )
        + 'echo "PICKED $vm_port $container_port$published_ports_suffix"\n'
        + f"echo '{template}' | substitute_published_port_tokens\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_port_pick_skips_tcp_and_udp_listeners_and_every_recorded_port(tmp_path: Path) -> None:
    # 22003 is a TCP listener and 22004 a UDP listener: a published port
    # serves both protocols, so neither family may hold it. 22000-22002 are
    # recorded (the sshd ports and a published host port of another slice).
    fake_ss = (
        '#!/bin/bash\ncase "$*" in *-Htln*) echo "LISTEN 0 128 0.0.0.0:22003 0.0.0.0:*";; '
        '*-Hlun*) echo "UNCONN 0 0 0.0.0.0:22004 0.0.0.0:*";; esac\n'
    )
    output = _run_port_pick(_SCREEN_DECLARATIONS, tmp_path, fake_ss)
    assert "PICKED 22005 22006 22007 22008\n" in output
    # Both protocols of 59000 got the same box port; 8080 got the next one.
    assert (
        '[{"guest_port":59000,"protocol":"udp","host_port":22007},'
        '{"guest_port":59000,"protocol":"tcp","host_port":22007},'
        '{"guest_port":8080,"protocol":"tcp","host_port":22008}]\n'
    ) in output


def test_port_pick_without_declarations_picks_only_the_sshd_ports(tmp_path: Path) -> None:
    output = _run_port_pick((), tmp_path, "#!/bin/bash\ntrue\n")
    assert "PICKED 22003 22004\n" in output
    assert "[]\n" in output


def test_port_pick_refuses_with_the_marker_when_the_range_is_exhausted(tmp_path: Path) -> None:
    script = f'set -euo pipefail\nINSTANCES_DIR="{tmp_path}"\n' + render_gen2_port_pick_lines(
        port_range_start=22000,
        port_range_end=22002,
        no_ports_marker="NO_PORTS",
        published_port_declarations=_SCREEN_DECLARATIONS,
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 3
    assert "NO_PORTS" in result.stderr


def test_gen2_ordinal_derivation_lines_match_the_python_derivations() -> None:
    # The bash arithmetic must agree with slice_mac_address / derive_slice_network
    # for every ordinal; spot-check the constants it embeds.
    lines = render_gen2_ordinal_derivation_lines()
    assert "printf '52:54:00:6d:%02x:%02x' $(( ordinal >> 8 )) $(( ordinal & 255 ))" in lines
    assert '"10.201.$(( address_offset >> 8 )).$(( (address_offset & 255) + 2 ))"' in lines
    assert '"10.201.$(( address_offset >> 8 )).$(( (address_offset & 255) + 1 ))"' in lines
    assert_valid_bash("ordinal=3\n" + lines)


def test_budget_guard_counts_pre_sizing_env_files_at_the_default_size() -> None:
    guard = render_gen2_budget_guard_lines(
        units=16,
        data_disk_gib=56,
        unit_budget_mib=120 * 1024,
        disk_budget_gib=400,
        excluded_instance_name="",
    )
    # An env file predating the sizing columns falls back to the default
    # machine size (8 units, 44GiB data disk) so a mixed box stays counted.
    assert "inst_units=8" in guard
    assert "inst_disk=44" in guard
    assert "continue" not in guard.split('[ -e "$env_file" ] || continue')[1]
    # The new machine's memory footprint (16 units + the 512MiB per-VM overhead)
    # is checked against the box's MiB budget, its disk footprint (10GiB boot +
    # 56GiB data) against the disk budget.
    assert f"used_budget_mib + {16 * 1024 + 512} )) -gt {120 * 1024}" in guard
    assert "used_disk_gib + 10 + 56 )) -gt 400" in guard


def test_budget_guard_can_exclude_one_instance_for_in_place_resizes() -> None:
    guard = render_gen2_budget_guard_lines(
        units=16,
        data_disk_gib=56,
        unit_budget_mib=120 * 1024,
        disk_budget_gib=400,
        excluded_instance_name="mngr-slice-dev-x-abc",
    )
    assert '[ "$(basename "$(dirname "$env_file")")" = mngr-slice-dev-x-abc ] && continue' in guard


def test_destroy_script_targets_the_recorded_ordinal_and_tolerates_partial_state() -> None:
    destroy_script = build_qemu_destroy_script("mngr-slice-dev-x-abc")
    assert_valid_bash(destroy_script)
    assert "/srv/mngr-slices/instances/mngr-slice-dev-x-abc" in destroy_script
    # Separate stop + disable (sudoers argument matching forbids `disable --now`),
    # each tolerant of the target already being gone.
    assert 'sudo /usr/bin/systemctl stop "mngr-slice@$ordinal" || true' in destroy_script
    assert 'sudo /usr/bin/systemctl disable "mngr-slice@$ordinal" || true' in destroy_script
    assert 'rm -f "/srv/mngr-slices/by-ordinal/$ordinal"' in destroy_script
    assert 'rm -rf "$slice_dir"' in destroy_script


def test_list_instances_command_tolerates_a_box_without_slices() -> None:
    assert build_qemu_list_instances_command() == snapshot("ls -1 /srv/mngr-slices/instances 2>/dev/null || true")


def test_parse_slice_instance_observations_reads_state_and_age_from_the_box_clock() -> None:
    output = (
        "MNGR_SLICE_NOW 1000000\n"
        "mngr-slice-dev-a-0123456789abcdef active 999400\n"
        "mngr-slice-dev-a-fedcba9876543210 inactive 990000\n"
        "mngr-slice-dev-a-00000000deadbeef unknown 999990\n"
    )
    observations = parse_slice_instance_observations(output)
    assert [(o.instance_name, o.is_active, o.age_seconds) for o in observations] == [
        ("mngr-slice-dev-a-0123456789abcdef", True, 600.0),
        ("mngr-slice-dev-a-fedcba9876543210", False, 10000.0),
        ("mngr-slice-dev-a-00000000deadbeef", False, 10.0),
    ]


def test_parse_slice_instance_observations_rejects_a_malformed_line() -> None:
    with pytest.raises(MalformedBoxOutputError):
        parse_slice_instance_observations("MNGR_SLICE_NOW 1000000\nmngr-slice-dev-a-0123456789abcdef active\n")
    # A line before the clock marker cannot be aged.
    with pytest.raises(MalformedBoxOutputError):
        parse_slice_instance_observations("mngr-slice-dev-a-0123456789abcdef active 999400\n")
    with pytest.raises(MalformedBoxOutputError):
        parse_slice_instance_observations("MNGR_SLICE_NOW 1000000\nmngr-slice-dev-a-0123456789abcdef active soon\n")


def test_build_qemu_list_instance_observations_command_is_valid_bash() -> None:
    command = build_qemu_list_instance_observations_command()
    assert "MNGR_SLICE_NOW" in command and "systemctl is-active" in command
    assert_valid_bash(command)
