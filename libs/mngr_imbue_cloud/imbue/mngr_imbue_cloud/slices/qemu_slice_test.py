import hashlib
import shutil
import subprocess

import pytest
from inline_snapshot import snapshot

from imbue.mngr_imbue_cloud.errors import SliceReserveOutputError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_slice_env_file
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_meta_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_network_config
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_user_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_SLICE_COUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import derive_slice_network
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_UPLINK_SHAPING_PERCENT
from imbue.mngr_imbue_cloud.slices.qemu_slice import GEN2_DHCP_UNIT_SANDBOX_DIRECTIVES
from imbue.mngr_imbue_cloud.slices.qemu_slice import GEN2_UNIT_SANDBOX_DIRECTIVES
from imbue.mngr_imbue_cloud.slices.qemu_slice import build_qemu_boot_wait_script
from imbue.mngr_imbue_cloud.slices.qemu_slice import build_qemu_reserve_script
from imbue.mngr_imbue_cloud.slices.qemu_slice import build_qemu_start_command
from imbue.mngr_imbue_cloud.slices.qemu_slice import build_qemu_status_command
from imbue.mngr_imbue_cloud.slices.qemu_slice import parse_gen2_reserved_line
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_dhcp_config
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_dhcp_nftables_policy
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_dhcp_unit
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_helper_script
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_sudoers
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_unit_file


def test_unit_file_runs_qemu_sandboxed_as_the_per_slice_user() -> None:
    unit = render_slice_unit_file()
    # The instance parameter is the ordinal, so User= and every path use %i.
    assert "User=mngr-slice-%i" in unit
    assert "EnvironmentFile=/srv/mngr-slices/by-ordinal/%i/env" in unit
    # Foreground qemu under systemd -- which is what makes the full seccomp
    # sandbox valid (spawn=deny kills the -daemonize fork; spike-verified).
    assert "-sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny" in unit
    assert "-daemonize" not in unit
    # Root setup/teardown bracket the unprivileged VM process.
    assert "ExecStartPre=+/usr/local/sbin/mngr-slice-helper setup %i" in unit
    assert "ExecStop=+/usr/local/sbin/mngr-slice-helper powerdown %i" in unit
    assert "ExecStopPost=+/usr/local/sbin/mngr-slice-helper teardown %i" in unit
    # qemu keeps the slice user's OWN groups: Group=slicehost would hand a VM
    # escapee group access to every slice's files.
    assert "Group=" not in unit
    # Both qcow2 drives declare their format explicitly (never format-probed);
    # the cidata rides as a read-only raw virtio-blk disk (no SCSI controller
    # or CD-ROM emulation in the VM).
    assert unit.count("format=qcow2") == 2
    assert "-drive file=/srv/mngr-slices/by-ordinal/%i/cidata.iso,format=raw,readonly=on,if=virtio" in unit
    assert "virtio-scsi" not in unit
    assert "scsi-cd" not in unit
    # No default devices, no user config, nested virtualization hidden from the
    # guest, and the USB/SMM/S3/S4 surface removed.
    assert "-nodefaults" in unit
    assert "-no-user-config" in unit
    assert "-cpu host,vmx=off,svm=off" in unit
    assert "-machine q35,accel=kvm,usb=off,smm=off" in unit
    assert "-global ICH9-LPC.disable_s3=1" in unit
    assert "-global ICH9-LPC.disable_s4=1" in unit
    # The guest console lands in journald (bounded by its rate limiting), never
    # in a file a guest could grow without limit on the shared storage.
    assert "-chardev stdio,id=ser0,signal=off" in unit
    assert "-serial chardev:ser0" in unit
    assert "serial.log" not in unit
    # qemu's one writable runtime file (the QMP socket) lives in the run/
    # subdir the setup step creates for it.
    assert "-qmp unix:/srv/mngr-slices/by-ordinal/%i/run/qmp.sock,server=on,wait=off" in unit
    # `systemctl enable` at reserve + WantedBy is the whole boot-autostart story.
    assert "WantedBy=multi-user.target" in unit
    # A rebooted box never starts a VM before the DHCP server that addresses it
    # (Wants, not Requires: a DHCP server restart must not stop running VMs).
    assert "Wants=network-online.target mngr-slice-dhcp.service" in unit
    assert "After=network-online.target mngr-slice-dhcp.service" in unit
    assert "Requires=" not in unit
    # Restart stays off: a VM that dies is a workspace the connector must
    # notice, not something systemd silently brings back.
    assert "Restart=no" in unit


def test_unit_file_confines_qemu_with_the_systemd_sandbox() -> None:
    unit = render_slice_unit_file()
    for directive in GEN2_UNIT_SANDBOX_DIRECTIVES:
        assert directive in unit
    # Read-only everywhere except the QMP socket dir and the two disk images;
    # the env file and cidata deliberately stay read-only to qemu.
    # The `-` prefix: the bind mounts are set up for the root helper steps too,
    # and run/ does not exist until the setup step creates it at first start.
    assert (
        "ReadWritePaths=-/srv/mngr-slices/by-ordinal/%i/run -/srv/mngr-slices/by-ordinal/%i/disk.qcow2 "
        "-/srv/mngr-slices/by-ordinal/%i/datadisk.qcow2"
    ) in unit
    assert "by-ordinal/%i/env" not in unit.split("ReadWritePaths=")[1].splitlines()[0]
    assert "cidata.iso" not in unit.split("ReadWritePaths=")[1].splitlines()[0]
    # Exactly the two device nodes a KVM-only qemu with a tap needs.
    assert "DevicePolicy=closed" in unit
    assert unit.count("DeviceAllow=") == 2
    # The sandbox directives apply to qemu, not to the root helper steps (the
    # `+` prefix exempts those), and they follow the Exec lines.
    assert unit.index("ExecStopPost=") < unit.index("NoNewPrivileges=yes")


def test_unit_file_matches_the_prototype_verified_shape() -> None:
    assert render_slice_unit_file() == snapshot("""\
[Unit]
Description=mngr gen-2 slice VM (ordinal %i)
After=network-online.target mngr-slice-dhcp.service
Wants=network-online.target mngr-slice-dhcp.service
RequiresMountsFor=/srv/mngr-slices

[Service]
Type=simple
User=mngr-slice-%i
EnvironmentFile=/srv/mngr-slices/by-ordinal/%i/env
ExecStartPre=+/usr/local/sbin/mngr-slice-helper setup %i
ExecStart=/usr/bin/qemu-system-x86_64 \\
    -nodefaults \\
    -no-user-config \\
    -m ${MNGR_SLICE_MEMORY_MIB} \\
    -cpu host,vmx=off,svm=off \\
    -machine q35,accel=kvm,usb=off,smm=off \\
    -global ICH9-LPC.disable_s3=1 \\
    -global ICH9-LPC.disable_s4=1 \\
    -smp ${MNGR_SLICE_VCPUS} \\
    -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \\
    -drive file=/srv/mngr-slices/by-ordinal/%i/disk.qcow2,format=qcow2,if=none,discard=on,cache=none,aio=native,id=boot-disk \\
    -device virtio-blk-pci,drive=boot-disk,bootindex=1 \\
    -drive file=/srv/mngr-slices/by-ordinal/%i/datadisk.qcow2,format=qcow2,if=virtio,discard=on,cache=none,aio=native \\
    -drive file=/srv/mngr-slices/by-ordinal/%i/cidata.iso,format=raw,readonly=on,if=virtio \\
    -netdev tap,id=net0,ifname=${MNGR_SLICE_TAP},script=no,downscript=no \\
    -device virtio-net-pci,netdev=net0,mac=${MNGR_SLICE_MAC} \\
    -device virtio-rng-pci \\
    -display none \\
    -vga none \\
    -chardev stdio,id=ser0,signal=off \\
    -serial chardev:ser0 \\
    -qmp unix:/srv/mngr-slices/by-ordinal/%i/run/qmp.sock,server=on,wait=off \\
    -sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny
ExecStop=+/usr/local/sbin/mngr-slice-helper powerdown %i
ExecStopPost=+/usr/local/sbin/mngr-slice-helper teardown %i
TimeoutStopSec=180
Restart=no
NoNewPrivileges=yes
CapabilityBoundingSet=
RestrictNamespaces=yes
RestrictAddressFamilies=AF_UNIX
IPAddressDeny=any
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ProcSubset=pid
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
DevicePolicy=closed
DeviceAllow=/dev/kvm rw
DeviceAllow=/dev/net/tun rw
UMask=0077
MemoryDenyWriteExecute=yes
ReadWritePaths=-/srv/mngr-slices/by-ordinal/%i/run -/srv/mngr-slices/by-ordinal/%i/disk.qcow2 -/srv/mngr-slices/by-ordinal/%i/datadisk.qcow2

[Install]
WantedBy=multi-user.target
""")


def test_helper_script_pins_the_units_resource_limits_as_runtime_properties() -> None:
    helper = render_slice_helper_script()
    # The unit file cannot read env values into resource directives, so the
    # root setup step pins them per start from the strict-shape env values.
    assert 'systemctl set-property --runtime "mngr-slice@$ORDINAL"' in helper
    # MemoryMax is exactly the machine's budget share (units + the 512 MiB
    # per-VM overhead), so a full box's caps sum to its budget; the guest boots
    # 512 MiB below its units so qemu fits underneath; guest RAM never reaches
    # the box swap; there is no MemoryHigh.
    assert '"MemoryMax=$(( MNGR_SLICE_UNITS * 1024 + 512 ))M"' in helper
    assert '"MemorySwapMax=0"' in helper
    assert "MemoryHigh=" not in helper
    # Weights are the units-proportional share, 100 at the default machine.
    assert '"CPUWeight=$(( MNGR_SLICE_UNITS * 100 / 8 ))"' in helper
    assert '"IOWeight=$(( MNGR_SLICE_UNITS * 100 / 8 ))"' in helper
    assert '"TasksMax=1024"' in helper
    # The properties are set only after the env values were validated.
    assert helper.index("MNGR_SLICE_UNITS=$(env_get MNGR_SLICE_UNITS") < helper.index("systemctl set-property")


def test_helper_script_installs_the_enforced_ceilings_and_management_block() -> None:
    helper = render_slice_helper_script()
    # Day-one enforcement: concurrent-connection ceiling, new-connection rate,
    # and the guest->box input drop (the 2026-08 incident's exact vector).
    assert "ct count over 50000 counter drop" in helper
    assert "limit rate over 300/second" in helper
    assert 'input iifname "$MNGR_SLICE_TAP"' in helper
    # Replies to BOX-initiated connections (the boot-wait probe, the image-cache
    # transfer) must pass before the drop; guest-initiated traffic is ct state
    # new and still has no path -- except the guest's DHCP requests to the box's
    # DHCP server on this tap, the one guest-initiated flow the box answers.
    established_accept = 'input iifname "$MNGR_SLICE_TAP" ct state established,related'
    input_drop = 'input iifname "$MNGR_SLICE_TAP" \\\n        counter drop'
    dhcp_accept = 'input iifname "$MNGR_SLICE_TAP" \\\n        udp sport 68 udp dport 67 \\\n        counter accept'
    assert established_accept in helper
    assert dhcp_accept in helper
    assert helper.index(established_accept) < helper.index(input_drop)
    assert helper.index(dhcp_accept) < helper.index(input_drop)
    # DHCP is admitted only on the input path from the tap; nothing opens
    # port 53 (the box's DHCP server serves no DNS) or DHCP in the forward chain.
    assert "dport 53" not in helper
    assert helper.count("udp dport 67") == 1
    # The diagnostic ICMP allowance reaches exactly the slice's own gateway,
    # not every box-owned address.
    assert 'ip daddr "$MNGR_SLICE_GATEWAY_IP" ip protocol icmp' in helper
    # DNAT lands in BOTH prerouting and output, so box-local connections to the
    # forwarded ports (e.g. the image-cache transfer) also reach the VM -- but
    # only for traffic addressed to the box itself, so a box-originated
    # connection to a remote host on a slice-range port is left alone.
    assert "for CHAIN in prerouting output; do" in helper
    assert helper.count('"$CHAIN" fib daddr type local') == 2
    # The loopback-addressed hairpin (the transfer targets 127.0.0.1:<vm_port>)
    # additionally needs route_localnet on the tap and box-source traffic into
    # the tap masqueraded -- a VM handed src 127.0.0.1 would reply to itself.
    # The masquerade is local-source only: forwarded client traffic keeps its
    # real source address.
    assert 'sysctl -q -w "net.ipv4.conf.$MNGR_SLICE_TAP.route_localnet=1"' in helper
    assert 'postrouting oifname "$MNGR_SLICE_TAP" fib saddr type local' in helper
    # Inter-VM isolation: DNAT'd (public-port hairpin) flows pass, direct
    # tap-to-tap traffic does not.
    assert "ct status dnat counter accept" in helper
    # Fair-share bandwidth is keyed off the declared uplink rate; the guard
    # skips it only for env files rendered before the uplink became mandatory.
    # The guarantee is the machine's units-proportional share of the box
    # (specs/slice-fleet).
    assert 'if [ -n "$MNGR_SLICE_UPLINK_MBPS" ]; then' in helper
    # The root and default classes run at the shaped rate and every machine's
    # guarantee and ceiling derive from it, so the shaper is the bottleneck.
    assert f"SHAPED_MBIT=$(( MNGR_SLICE_UPLINK_MBPS * {GEN2_UPLINK_SHAPING_PERCENT} / 100 ))" in helper
    assert 'rate "${SHAPED_MBIT}mbit" ceil "${SHAPED_MBIT}mbit"' in helper
    assert "GUARANTEED_MBIT=$(( SHAPED_MBIT * MNGR_SLICE_UNITS / MNGR_SLICE_TOTAL_UNITS ))" in helper
    assert 'rate "${GUARANTEED_MBIT}mbit" ceil "${SHAPED_MBIT}mbit"' in helper
    assert 'ceil "${MNGR_SLICE_UPLINK_MBPS}mbit"' not in helper
    assert "MNGR_SLICE_SLOT_COUNT" not in helper


def test_helper_script_blocks_spoofed_sources_and_direct_to_mx_smtp() -> None:
    helper = render_slice_helper_script()
    # Anti-spoofing: only the VM's own address may enter from its tap, and the
    # drop comes before every other forward rule (a spoofed packet must never
    # reach the ceilings, counters, or fair-share mark -- and never be
    # forwarded, since the masquerade rewrites only saddr == vm_ip).
    spoof_drop = 'forward iifname "$MNGR_SLICE_TAP" \\\n        ip saddr != "$MNGR_SLICE_VM_IP" counter drop'
    assert spoof_drop in helper
    # The very first forward rule added is the spoof drop.
    assert helper.index(spoof_drop) == helper.index('nft add rule inet "$NFT_TABLE" forward') + len(
        'nft add rule inet "$NFT_TABLE" '
    )
    # Direct-to-MX SMTP is dropped (every tenant shares the box's public IP,
    # so one spammer poisons it for the whole box); blocked attempts feed a
    # named counter so the telemetry pipeline sees them. Authenticated
    # submission ports (587/465) are deliberately NOT blocked.
    assert 'tcp dport 25 \\\n        counter name "slice_${ORDINAL}_smtp_blocked" drop' in helper
    assert helper.index("tcp dport 25") < helper.index("ct count over")
    assert "dport 587" not in helper
    assert "dport 465" not in helper
    # The counter is pre-created like the other per-VM named counters.
    assert 'nft add counter inet "$NFT_TABLE" "slice_${ORDINAL}_smtp_blocked"' in helper


def test_helper_script_never_trusts_slice_user_writable_state_as_root() -> None:
    helper = render_slice_helper_script()
    # The env file is parsed value-by-value with strict shape validation, never
    # sourced: the helper runs as root and a VM escapee lands in the slice's
    # unix user, so nothing that user could influence may be executed.
    assert '. "$ENV_FILE"' not in helper
    assert 'source "$ENV_FILE"' not in helper
    assert 'MNGR_SLICE_USER=$(env_get MNGR_SLICE_USER "mngr-slice-$ORDINAL")' in helper
    assert "refusing: $key in $ENV_FILE is missing or malformed" in helper
    # The slice dir and env stay slicehost's; the VM's user is handed only its
    # disk media and a setgid run/ dir for its qmp socket and serial log.
    assert "chown -R" not in helper
    assert 'chown "slicehost:slicehost" "$SLICE_DIR/"' in helper
    assert 'chmod 751 "$SLICE_DIR/"' in helper
    assert 'chmod 2770 "$SLICE_DIR/run"' in helper
    # No chown/chmod ever touches the env file (it stays 600 slicehost).
    assert '"$SLICE_DIR"/env' not in helper


def test_sudoers_scopes_the_service_user_to_exactly_the_slice_units() -> None:
    sudoers = render_slice_sudoers()
    lines = sudoers.strip().splitlines()
    # One exact-argument line per pre-created ordinal. No wildcard anywhere: a
    # sudoers '*' matches across whitespace, so 'mngr-slice@*' would also match
    # e.g. 'stop mngr-slice@0 ssh.service' and hand slicehost every unit as root.
    assert "*" not in sudoers
    assert len(lines) == GEN2_MAX_SLICE_COUNT
    assert lines[0] == snapshot(
        "slicehost ALL=(root) NOPASSWD: /usr/bin/systemctl start mngr-slice@0, "
        "/usr/bin/systemctl stop mngr-slice@0, /usr/bin/systemctl enable mngr-slice@0, "
        "/usr/bin/systemctl disable mngr-slice@0, /usr/bin/systemctl reset-failed mngr-slice@0"
    )
    assert lines[-1].endswith(f"/usr/bin/systemctl reset-failed mngr-slice@{GEN2_MAX_SLICE_COUNT - 1}")


def _reserve_script_for_test(units: int = 8, unit_budget_mib: int = 120 * 1024, disk_budget_gib: int = 400) -> str:
    user_data = build_qemu_slice_user_data(
        host_dir="/home/user/.mngr",
        root_authorized_public_keys=("ssh-ed25519 AAAAbake",),
        host_private_key_pem="pem",
        host_public_key_openssh="ssh-ed25519 AAAAhost",
    )
    return build_qemu_reserve_script(
        instance_name="mngr-slice-dev-x-abc",
        boot_disk_gib=GEN2_BOOT_DISK_GIB,
        data_disk_gib=30,
        units=units,
        unit_budget_mib=unit_budget_mib,
        disk_budget_gib=disk_budget_gib,
        port_range_start=22000,
        port_range_end=32000,
        user_data_text=user_data,
        meta_data_text=build_qemu_slice_meta_data("mngr-slice-dev-x-abc"),
        network_config_text=build_qemu_slice_network_config(),
        env_file_template_text=build_qemu_slice_env_file(
            instance_name="mngr-slice-dev-x-abc",
            ordinal=None,
            vcpus=2,
            units=units,
            total_units=120,
            data_disk_gib=30,
            vm_ssh_host_port="__MNGR_VM_SSH_PORT__",
            container_ssh_host_port="__MNGR_CONTAINER_SSH_PORT__",
            uplink_mbps=None,
        ),
    )


def test_reserve_script_guards_budgets_ports_and_real_free_space() -> None:
    script = _reserve_script_for_test()
    # Ordered guards: the two-budget accounting (memory units, then disk),
    # free ordinal, free ports, then the df guard (real free space must cover
    # the slice's full virtual size + margin).
    assert "MNGR_SLICE_NO_UNITS" in script
    assert "MNGR_SLICE_NO_DISK" in script
    assert "MNGR_SLICE_NO_PORTS" in script
    # The new machine's memory footprint (8 units + the 512MiB per-VM overhead)
    # is checked against the box's MiB budget.
    assert f"used_budget_mib + {8 * 1024 + 512} )) -gt {120 * 1024}" in script
    # Its disk footprint (20GiB boot + 30GiB data) is checked against the disk budget.
    assert f"used_disk_gib + {GEN2_BOOT_DISK_GIB} + 30 )) -gt {400}" in script
    # 20 + 30 + 2 GiB margin.
    assert f"-lt {(GEN2_BOOT_DISK_GIB + 30 + 2) * 1024**3}" in script
    assert "MNGR_SLICE_NO_SPACE" in script
    # Copy semantics via reflink (instant on XFS, plain copy elsewhere), sized
    # to the boot budget; the unit is enabled (boot autostart) but NOT started.
    assert 'cp --reflink=auto "$BASE_IMAGE" "$slice_dir/disk.qcow2"' in script
    assert "qemu-img resize -q" in script
    assert 'sudo /usr/bin/systemctl enable "mngr-slice@$ordinal"' in script
    assert "systemctl start" not in script


def test_reserve_script_substitutes_the_single_ordinal_template_on_the_box() -> None:
    script = _reserve_script_for_test()
    # ONE template for the env file (no per-ordinal case table): the box
    # derives the chosen ordinal's MAC and /30 with shell arithmetic and
    # substitutes the placeholder tokens. The cidata files are written as
    # given -- nothing in them depends on the placement.
    assert "substitute_ordinal_tokens" in script
    assert script.count("| substitute_ordinal_tokens") == 1
    assert 'base64 -d > "$slice_dir/network-config"' in script
    assert 'case "$1" in' not in script
    assert "printf '52:54:00:6d:%02x:%02x'" in script
    assert "s/__MNGR_SLICE_ORDINAL__/$ordinal/g" in script
    assert "s/__MNGR_SLICE_VM_IP__/$vm_ip/g" in script


def test_dhcp_config_serves_only_dhcp_with_one_address_range_per_ordinal() -> None:
    config = render_slice_dhcp_config()
    lines = config.splitlines()
    # DHCP only: nothing may ever listen for DNS on the box's public interface.
    assert "port=0" in lines
    # Bound dynamically to the taps as the helper creates and deletes them.
    assert "bind-dynamic" in lines
    assert "interface=mslice*" in lines
    # Leases keyed by the ordinal-derived MAC, so a re-carve of the same
    # ordinal (a new VM, hence a new client-id) is never blocked by the
    # previous VM's lease on the range's single address.
    assert "dhcp-ignore-clid" in lines
    assert "dhcp-authoritative" in lines
    # Unconfigured guests are answered by broadcast (each tap has exactly one
    # guest) and candidate addresses are never ICMP-probed: the two things
    # dnsmasq would otherwise need CAP_NET_ADMIN (ARP-cache injection) and
    # CAP_NET_RAW for, which the unit's bounding set does not carry.
    assert "dhcp-broadcast" in lines
    assert "no-ping" in lines
    assert "user=" not in config
    assert "dhcp-option=option:dns-server,1.1.1.1,8.8.8.8" in lines
    range_lines = [line for line in lines if line.startswith("dhcp-range=")]
    assert len(range_lines) == GEN2_MAX_SLICE_COUNT
    # Each range is the ordinal's own /30 address, with the /30 netmask so
    # dnsmasq matches it to the tap the request arrived on.
    assert range_lines[0] == "dhcp-range=10.201.0.2,10.201.0.2,255.255.255.252,12h"
    assert range_lines[3] == "dhcp-range=10.201.0.14,10.201.0.14,255.255.255.252,12h"
    assert range_lines[-1] == snapshot("dhcp-range=10.201.7.254,10.201.7.254,255.255.255.252,12h")
    for ordinal, line in enumerate(range_lines):
        vm_ip = derive_slice_network(ordinal).vm_ip
        assert line == f"dhcp-range={vm_ip},{vm_ip},255.255.255.252,12h"
    assert hashlib.sha256(config.encode()).hexdigest() == snapshot(
        "48811444e6d2d871076cf81e648062ba9f50eac5b37aed21eac39d552f4efa0a"
    )


@pytest.mark.skipif(shutil.which("dnsmasq") is None, reason="dnsmasq required")
def test_dhcp_config_passes_dnsmasq_own_syntax_check() -> None:
    # The prep runs this exact check before installing the config and the unit's
    # ExecStartPre runs it before every start, so an option dnsmasq rejects
    # would abort every gen-2 box prep.
    result = subprocess.run(
        ["dnsmasq", "--test", "--conf-file=-"], input=render_slice_dhcp_config(), capture_output=True, text=True
    )
    assert result.returncode == 0, f"dnsmasq rejected the rendered config: {result.stderr}"


def test_dhcp_unit_runs_dnsmasq_in_the_foreground_on_exactly_the_rendered_config() -> None:
    unit = render_slice_dhcp_unit()
    # Our own unit, never the distro's (which reads /etc/dnsmasq.conf): the
    # rendered config is the only one consulted, and it is checked first.
    assert "ExecStartPre=/usr/sbin/dnsmasq --test --conf-file=/etc/mngr/slice-dhcp.conf" in unit
    assert "ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=/etc/mngr/slice-dhcp.conf" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "dnsmasq.conf" not in unit.replace("slice-dhcp.conf", "")
    # The udp/67 policy is loaded (nftables.service) before the socket exists.
    assert "After=network-online.target nftables.service" in unit
    assert "Wants=network-online.target nftables.service" in unit


def test_dhcp_unit_confines_dnsmasq_as_an_unprivileged_user_with_the_systemd_sandbox() -> None:
    unit = render_slice_dhcp_unit()
    for directive in GEN2_DHCP_UNIT_SANDBOX_DIRECTIVES:
        assert directive in unit
    # Never root: the unit starts dnsmasq as the dedicated service user, and
    # the only capability it ever holds is the one that binds udp/67. In
    # particular no CAP_NET_ADMIN (which could rewrite the mngr_slices rules
    # and routes) and no CAP_NET_RAW.
    assert "User=mngr-dhcp" in unit
    assert "Group=mngr-dhcp" in unit
    assert "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in unit
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in unit
    assert "CAP_NET_ADMIN" not in unit
    assert "CAP_NET_RAW" not in unit
    assert "CAP_SETUID" not in unit
    # Read-only everywhere except the lease directory, which systemd creates
    # for the service user; no raw packet sockets; no IP traffic beyond the
    # slice /30s, the unconfigured-guest source and the broadcast address.
    assert "ProtectSystem=strict" in unit
    assert "StateDirectory=mngr-slice-dhcp" in unit
    assert "ReadWritePaths=" not in unit
    assert "RestrictAddressFamilies=AF_INET AF_NETLINK AF_UNIX" in unit
    assert "AF_PACKET" not in unit
    assert "IPAddressAllow=10.201.0.0/16 0.0.0.0/32 255.255.255.255/32" in unit
    assert "IPAddressDeny=any" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "PrivateDevices=yes" in unit
    assert "SystemCallFilter=@system-service" in unit
    assert "MemoryDenyWriteExecute=yes" in unit
    # The sandbox directives follow the Exec lines, like the slice unit's.
    assert unit.index("ExecStart=") < unit.index("User=mngr-dhcp")


def test_dhcp_nftables_policy_drops_udp_67_from_every_interface_but_the_taps() -> None:
    policy = render_slice_dhcp_nftables_policy()
    # Its own table (never the slice helper's or the lockdown's), with the
    # idempotent add-then-delete-then-add replace pattern so re-loading the
    # file converges.
    assert "add table inet mngr_slice_dhcp" in policy
    assert "delete table inet mngr_slice_dhcp" in policy
    assert policy.count("add table inet mngr_slice_dhcp") == 2
    rule_lines = [line for line in policy.splitlines() if not line.startswith("#")]
    assert all("mngr_slices" not in line and "mngr_mgmt" not in line for line in rule_lines)
    # Only the server port drops, and only off the taps: the taps' own DHCP
    # requests still reach the server, and the box's own uplink DHCP client
    # (the supplier assigns the public address by DHCP) keeps its udp/68 replies.
    assert 'iifname != "mslice*" udp dport 67 counter drop' in policy
    assert policy.count("dport") == 1
    assert "dport 68" not in policy
    assert "type filter hook input priority -10; policy accept;" in policy


def test_parse_gen2_reserved_line_round_trips() -> None:
    assert parse_gen2_reserved_line("noise\nMNGR_SLICE_RESERVED 22010 22011 3\n") == (22010, 22011, 3)


def test_parse_gen2_reserved_line_rejects_malformed_output() -> None:
    with pytest.raises(SliceReserveOutputError):
        parse_gen2_reserved_line("MNGR_SLICE_RESERVED 22010 oops 3")
    with pytest.raises(SliceReserveOutputError):
        parse_gen2_reserved_line("nothing here")


def test_start_and_status_commands_target_the_recorded_ordinal() -> None:
    assert build_qemu_start_command(3) == snapshot("sudo /usr/bin/systemctl start mngr-slice@3")
    status_command = build_qemu_status_command("mngr-slice-dev-x-abc")
    assert "/srv/mngr-slices/instances/mngr-slice-dev-x-abc" in status_command
    assert "echo absent" in status_command


def test_boot_wait_script_polls_the_routed_vm_address() -> None:
    script = build_qemu_boot_wait_script(ordinal=1, timeout_seconds=100)
    # The tap address is the box's direct path to the guest (no NAT involved),
    # so a banner there proves the guest itself is up.
    assert "/dev/tcp/10.201.0.6/22" in script
    assert "MNGR_SLICE_BOOT_TIMEOUT" in script
    # Wall-time deadlined so the timeout marker always lands before the SSH
    # session's own (only slightly larger) deadline kills the script.
    assert 'while [ "$SECONDS" -lt 100 ]; do' in script
