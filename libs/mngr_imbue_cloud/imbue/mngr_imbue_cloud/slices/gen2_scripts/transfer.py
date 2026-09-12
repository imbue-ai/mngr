import base64
import shlex
from collections.abc import Mapping
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_slice_env_file
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import render_gen2_budget_guard_lines
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import render_gen2_ordinal_derivation_lines
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import GEN2_CIDATA_FILE_NAMES
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import Gen2SliceCidata
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import DEFAULT_SLICE_PORT_RANGE_END
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import DEFAULT_SLICE_PORT_RANGE_START
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_ALLOC_LOCK_RELPATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_BY_ORDINAL_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_CONTAINER_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_INSTANCES_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_SLICE_COUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_UNITS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_SSH_PORT_PLACEHOLDER

# The transfer conventions every stop/start box script (gen-1 and gen-2) shares:
# a per-instance transfer dir in the slice service user's home holding the
# detached script, its env (S3 creds + age material), the flat KEY=VALUE status
# file the driver polls, the log, and the pid file. The scripts are deliberately
# dumb (idempotent ``zstd | age | s5cmd`` pipelines); every state-machine decision
# stays with the driver.

# Everything transfer-related for one instance lives under this directory in
# the slice service user's home.
TRANSFER_DIR_ROOT: Final[str] = ".mngr-transfers"

# Box-wide lock serializing artifact downloads (one at a time per box, so a
# restore never competes with another restore for disk/network).
DOWNLOAD_LOCK_RELPATH: Final[str] = ".mngr-download.lock"
# How long a restore waits for the download lock before failing. Downloads
# run at ~1 GB/s against ~30 GB disks, so a legitimate queue clears in well
# under a minute; anything longer means the lock is stuck, and failing fast
# with a real error beats parking the machine behind the transfer timeout.
DOWNLOAD_LOCK_WAIT_SECONDS: Final[int] = 300

# Object names within a generation prefix.
DISK_OBJECT: Final[str] = "disk.zst.age"
DATADISK_OBJECT: Final[str] = "datadisk.zst.age"
META_OBJECT: Final[str] = "meta.tar.zst.age"

# Marker printed by a restore-reserve script on success, followed by the two
# chosen host ports (and, on gen-2, the ordinal). Failure markers mirror the
# bake reserve script's.
RESTORE_RESERVED_MARKER: Final[str] = "MNGR_RESTORE_RESERVED"
RESTORE_NO_PORTS_MARKER: Final[str] = "MNGR_RESTORE_NO_PORTS"
# Marker the gen-2 restore-reserve prints when the budget math says there is
# room but the storage filesystem's real free space cannot hold the slice.
RESTORE_NO_SPACE_MARKER: Final[str] = "MNGR_RESTORE_NO_SPACE"
# Marker the in-place resize script prints on success, followed by the
# (unchanged) two host ports and the ordinal.
GEN2_RESIZE_APPLIED_MARKER: Final[str] = "MNGR_RESIZE_APPLIED"


class TransferEnv(FrozenModel):
    """The env-file contents a transfer script sources (creds + object coordinates)."""

    s3_endpoint: str = Field(description="S3 endpoint URL")
    s3_region: str = Field(description="S3 region name")
    access_key_id: str = Field(description="S3 access key id")
    secret_access_key: str = Field(description="S3 secret access key")
    bucket: str = Field(description="Artifact bucket")
    key_prefix: str = Field(description="Object key prefix for this generation (e.g. <hex>/gen-2)")
    instance_name: str = Field(description="Slice VM instance name")
    age_recipient: str = Field(default="", description="age recipient for upload (empty on download)")
    age_identity: str = Field(default="", description="age identity for download (empty on upload)")


@pure
def transfer_dir(instance_name: str) -> str:
    return f"$HOME/{TRANSFER_DIR_ROOT}/{instance_name}"


@pure
def render_transfer_env(env: TransferEnv) -> str:
    """The env file a transfer script sources. Written 0600, deleted when the transfer ends."""
    lines = [
        f"export AWS_ACCESS_KEY_ID={shlex.quote(env.access_key_id)}",
        f"export AWS_SECRET_ACCESS_KEY={shlex.quote(env.secret_access_key)}",
        f"export AWS_REGION={shlex.quote(env.s3_region)}",
        f"export WS_S3_ENDPOINT={shlex.quote(env.s3_endpoint)}",
        f"export WS_BUCKET={shlex.quote(env.bucket)}",
        f"export WS_KEY_PREFIX={shlex.quote(env.key_prefix)}",
        f"export WS_INSTANCE={shlex.quote(env.instance_name)}",
        f"export WS_AGE_RECIPIENT={shlex.quote(env.age_recipient)}",
    ]
    if env.age_identity:
        lines.append(f"export WS_AGE_IDENTITY={shlex.quote(env.age_identity)}")
    return "\n".join(lines) + "\n"


# Shared bash prelude: PATH (limactl/s5cmd live in /usr/local/bin, age too),
# the transfer dir, and the atomic KEY=VALUE status writer. ``status_kv``
# appends a key to the pending status; ``status_flush`` publishes atomically.
_SCRIPT_PRELUDE: Final[str] = """\
set -Eeuo pipefail
export PATH=/usr/local/bin:$HOME/.local/bin:$PATH
TD="$HOME/{transfer_dir_root}/{instance}"
mkdir -p "$TD"
. "$TD/env"
STATUS="$TD/status"
declare -A STATUS_KV
status_kv() {{ STATUS_KV["$1"]="$2"; }}
status_flush() {{
    : > "$STATUS.tmp"
    for key in "${{!STATUS_KV[@]}}"; do printf '%s=%s\\n' "$key" "${{STATUS_KV[$key]}}" >> "$STATUS.tmp"; done
    mv "$STATUS.tmp" "$STATUS"
}}
fail() {{
    status_kv STAGE failed
    status_kv FINISHED 1
    status_kv ERROR "$1"
    status_flush
    exit 1
}}
trap 'fail "command failed: $BASH_COMMAND"' ERR
"""


@pure
def script_prelude(instance_name: str) -> str:
    """The shared bash prelude every transfer script (gen-1 and gen-2) starts with."""
    return _SCRIPT_PRELUDE.format(transfer_dir_root=TRANSFER_DIR_ROOT, instance=instance_name)


@pure
def build_launch_detached_command(instance_name: str, script_filename: str) -> str:
    """Launch a transfer script detached from the SSH session, recording its pid.

    Any prior status file is removed first so pollers only ever observe the
    launched transfer's own status (a stale file from an earlier failed or
    unrelated transfer must not masquerade as this one's result).
    """
    td = transfer_dir(instance_name)
    # The brace group backgrounds only the script itself: the stale-status
    # removal happens synchronously, before the launch command returns, so a
    # poller can never race it.
    return (
        f'cd "{td}" && rm -f status && '
        f'{{ setsid nohup bash {shlex.quote(script_filename)} >> run.log 2>&1 & echo $! > "{td}/pid"; }}'
    )


@pure
def build_is_transfer_alive_command(instance_name: str) -> str:
    """Exit 0 when the recorded transfer pid is still running."""
    td = transfer_dir(instance_name)
    return f'[ -f "{td}/pid" ] && kill -0 "$(cat "{td}/pid")" 2>/dev/null'


@pure
def build_read_status_command(instance_name: str) -> str:
    """Print the transfer status file (empty output when it does not exist yet)."""
    td = transfer_dir(instance_name)
    return f'cat "{td}/status" 2>/dev/null || true'


@pure
def parse_status_text(text: str) -> dict[str, str]:
    """Parse the flat KEY=VALUE status file a transfer script writes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


# Gen-2 restore-reserve / download / in-place resize


@pure
def render_gen2_restore_reserve_script(
    *,
    instance_name: str,
    # The machine's restore size (its share of the box's two budgets).
    units: int,
    data_disk_gib: int,
    # The candidate box's budgets (from its row: ram_gb / disk_gb).
    unit_budget_mib: int,
    disk_budget_gib: int,
    # Real free bytes the storage filesystem must hold before the reserve
    # proceeds (the carve-time df guard's restore twin); 0 disables the guard.
    required_free_bytes: int,
    expected_meta_sha: str,
    # The env file as a single template (base64): the ordinal is chosen on-box
    # under the lock and its derived values (and the ports) substituted there.
    env_template_b64: str,
    # When set, the reserve writes these three files as the slice's cidata
    # instead of fetching them from the artifact's meta tar; the meta object
    # and ``expected_meta_sha`` are then not consulted at all.
    cidata: Gen2SliceCidata | None = None,
) -> str:
    """The synchronous gen-2 restore-reserve: claim the budgets + ports + an ordinal under the box lock.

    Mirrors the gen-2 bake reserve's guarantees: under the box-wide allocation
    flock it enforces the two-budget capacity accounting against the recorded
    env files, picks the lowest free ordinal and two free host ports, runs the
    df guard, then durably claims the slot by materializing the slice dir
    (the cidata ISO rebuilt from the artifact's own user-data, meta-data and
    network-config, copied verbatim -- the instance-id stays stable and the
    network is DHCP, so cloud-init never reruns on the new placement -- plus
    the env file with the chosen ordinal and ports). The unit is NOT enabled
    here -- the download script enables + starts it once the disks have
    landed, so a box reboot can never boot a half-restored VM.

    Convergent for a re-driven caller: a leftover dir under this same instance
    name (debris from an earlier crashed restore attempt -- a live placement is
    never re-reserved, since an existing VM either restarts in place or sits on
    a box the candidate list excludes) is reclaimed under the lock before the
    capacity guard, so the re-drive claims a fresh slot instead of wedging on
    the stale one.

    Prints ``MNGR_RESTORE_RESERVED <vm_port> <container_port> <ordinal>``.
    """
    budget_guard_lines = render_gen2_budget_guard_lines(
        units=units,
        data_disk_gib=data_disk_gib,
        unit_budget_mib=unit_budget_mib,
        disk_budget_gib=disk_budget_gib,
        excluded_instance_name="",
    )
    if cidata is None:
        cidata_block = "".join(
            f'[ -f "$TD/meta/$WS_INSTANCE/{name}" ] || fail "artifact meta tar did not contain the gen-2 {name}"\n'
            f'cp "$TD/meta/$WS_INSTANCE/{name}" "$slice_dir/{name}"\n'
            for name in GEN2_CIDATA_FILE_NAMES
        )
        meta_fetch_block = f"""\
# Fetch + verify the meta tar (the manifest pins its ciphertext sha).
umask 077
IDF="$TD/identity"
printf '%s\\n' "$WS_AGE_IDENTITY" > "$IDF"
s5cmd --endpoint-url "$WS_S3_ENDPOINT" cat "s3://$WS_BUCKET/$WS_KEY_PREFIX/{META_OBJECT}" > "$TD/meta.enc"
actual=$(sha256sum "$TD/meta.enc" | awk '{{print $1}}')
if [ "$actual" != {shlex.quote(expected_meta_sha)} ]; then
    rm -f "$IDF" "$TD/meta.enc"
    fail "sha256 mismatch for {META_OBJECT}: got $actual"
fi
rm -rf "$TD/meta"
mkdir -p "$TD/meta"
age -d -i "$IDF" < "$TD/meta.enc" | zstd -q -d | tar -x -C "$TD/meta"
rm -f "$IDF" "$TD/meta.enc"
"""
        meta_cleanup_line = 'rm -rf "$TD/meta"\n'
    else:
        cidata_block = "".join(
            f'echo {shlex.quote(base64.b64encode(content.encode()).decode())} | base64 -d > "$slice_dir/{name}"\n'
            for name, content in cidata.content_by_file_name().items()
        )
        meta_fetch_block = "umask 077\n"
        meta_cleanup_line = ""
    port_block = f"""\
pick_port() {{
    local p
    for ((p={DEFAULT_SLICE_PORT_RANGE_START}; p<{DEFAULT_SLICE_PORT_RANGE_END}; p++)); do
        if ! grep -qx "$p" "$used_ports_file"; then
            echo "$p"
            return 0
        fi
    done
    return 1
}}
vm_port=$(pick_port) || {{ echo "{RESTORE_NO_PORTS_MARKER}" >&2; exit 3; }}
echo "$vm_port" >> "$used_ports_file"
container_port=$(pick_port) || {{ echo "{RESTORE_NO_PORTS_MARKER}" >&2; exit 3; }}
"""
    return (
        script_prelude(instance_name)
        + f"""\
exec 9> "$HOME/{GEN2_ALLOC_LOCK_RELPATH}"
flock 9

INSTANCES_DIR={GEN2_INSTANCES_DIR}
BY_ORDINAL_DIR={GEN2_BY_ORDINAL_DIR}
mkdir -p "$INSTANCES_DIR" "$BY_ORDINAL_DIR"

# Reclaim this instance's own leftover dir (an earlier crashed restore
# attempt), mirroring the idempotent destroy script, so a re-driven
# supervisor converges instead of failing the mkdir below forever.
slice_dir="$INSTANCES_DIR/{instance_name}"
if [ -d "$slice_dir" ]; then
    stale_ordinal=$(grep -s '^MNGR_SLICE_ORDINAL=' "$slice_dir/env" 2>/dev/null | cut -d= -f2 || true)
    if [ -n "$stale_ordinal" ]; then
        sudo /usr/bin/systemctl stop "mngr-slice@$stale_ordinal" || true
        sudo /usr/bin/systemctl disable "mngr-slice@$stale_ordinal" || true
        sudo /usr/bin/systemctl reset-failed "mngr-slice@$stale_ordinal" 2>/dev/null || true
        rm -f "$BY_ORDINAL_DIR/$stale_ordinal"
    fi
    rm -rf "$slice_dir"
fi

# Two-budget capacity guard: memory units and disk, summed from the recorded
# env files (the reclaimed leftover above no longer counts).
{budget_guard_lines}
# Lowest free ordinal from the recorded env files.
used_ordinals=$(grep -sh '^MNGR_SLICE_ORDINAL=' "$INSTANCES_DIR"/*/env 2>/dev/null | cut -d= -f2 || true)
ordinal=""
for candidate in $(seq 0 {GEN2_MAX_SLICE_COUNT - 1}); do
    if ! printf '%s\\n' "$used_ordinals" | grep -qx "$candidate"; then
        ordinal="$candidate"
        break
    fi
done
if [ -z "$ordinal" ]; then
    echo "{GEN2_NO_UNITS_MARKER} no free ordinal" >&2
    exit 4
fi

# Free host ports: bound TCP ports + every recorded env file's ports.
used_ports_file=$(mktemp)
trap 'rm -f "$used_ports_file"' EXIT
ss -Htln 2>/dev/null | awk '{{print $4}}' | sed 's/.*://' | grep -E '^[0-9]+$' >> "$used_ports_file" || true
grep -sh '_SSH_HOST_PORT=' "$INSTANCES_DIR"/*/env 2>/dev/null | cut -d= -f2 \\
    | grep -E '^[0-9]+$' >> "$used_ports_file" || true
{port_block}
# The df guard: real free space must cover the incoming slice, whatever the
# slot math says (leaks and staging consume space outside its model).
if [ {required_free_bytes} -gt 0 ]; then
    available_bytes=$(df --output=avail -B1 "$INSTANCES_DIR" | tail -1 | tr -d ' ')
    if [ "$available_bytes" -lt {required_free_bytes} ]; then
        echo "{RESTORE_NO_SPACE_MARKER} available=$available_bytes required={required_free_bytes}" >&2
        exit 5
    fi
fi

{meta_fetch_block}
# Materialize the slice dir: the cidata files as the artifact carried them
# (nothing in them depends on the placement) and the env file with the chosen
# ordinal and ports. The disks land via the download script.
{render_gen2_ordinal_derivation_lines()}
mkdir "$slice_dir"
{cidata_block}\
echo {shlex.quote(env_template_b64)} | base64 -d | substitute_ordinal_tokens \\
    | sed "s/{GEN2_VM_SSH_PORT_PLACEHOLDER}/$vm_port/g; s/{GEN2_CONTAINER_SSH_PORT_PLACEHOLDER}/$container_port/g" \\
    > "$slice_dir/env"
genisoimage -quiet -output "$slice_dir/cidata.iso" -volid cidata -joliet -rock \\
    "$slice_dir/user-data" "$slice_dir/meta-data" "$slice_dir/network-config"
ln -sfn "$slice_dir" "$BY_ORDINAL_DIR/$ordinal"
{meta_cleanup_line}
echo "{RESTORE_RESERVED_MARKER} $vm_port $container_port $ordinal"
"""
    )


@pure
def render_gen2_download_script(
    *,
    instance_name: str,
    ordinal: int,
    expected_sha_by_name: Mapping[str, str],
    vm_ssh_port: int,
    container_ssh_port: int,
    # A pending disk grow's GiB target: the downloaded data disk is
    # qemu-img-resized to it before the boot (the in-guest oneshot grows the
    # filesystem on boot). None restores the disk at its uploaded size.
    grow_data_disk_gib: int | None,
) -> str:
    """The detached gen-2 download script: restore both disks, enable + boot the unit, wait for sshd."""
    expected_disk = shlex.quote(expected_sha_by_name["DISK"])
    expected_datadisk = shlex.quote(expected_sha_by_name["DATADISK"])
    grow_block = (
        f'qemu-img resize -q "$SLICE_DIR/datadisk.qcow2" {grow_data_disk_gib}G\n'
        if grow_data_disk_gib is not None
        else ""
    )
    return (
        script_prelude(instance_name)
        + f"""\
# One artifact download at a time per box (restores are fast; queue briefly).
exec 8> "$HOME/{DOWNLOAD_LOCK_RELPATH}"
flock 8

SLICE_DIR="{GEN2_INSTANCES_DIR}/$WS_INSTANCE"
IDF="$TD/identity"
umask 077
printf '%s\\n' "$WS_AGE_IDENTITY" > "$IDF"
trap 'rm -f "$IDF"' EXIT

download_one() {{
    local object="$1" target="$2" name="$3" expected="$4"
    s5cmd --endpoint-url "$WS_S3_ENDPOINT" cat "s3://$WS_BUCKET/$WS_KEY_PREFIX/$object" \\
        | tee >(sha256sum | awk '{{print $1}}' > "$TD/$name.sha") \\
        | age -d -i "$IDF" \\
        | zstd -q -d -f -o "$target"
    for _ in $(seq 1 50); do
        [ -s "$TD/$name.sha" ] && break
        sleep 0.1
    done
    actual="$(cat "$TD/$name.sha")"
    if [ "$actual" != "$expected" ]; then
        fail "sha256 mismatch for $object: got $actual, expected $expected"
    fi
}}

wait_ssh() {{
    local port="$1"
    for _ in $(seq 1 150); do
        if timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port && head -c 4 <&3" 2>/dev/null | grep -q SSH; then
            return 0
        fi
        sleep 2
    done
    return 1
}}

status_kv STAGE downloading
status_kv FINISHED 0
status_flush

download_one "{DISK_OBJECT}" "$SLICE_DIR/disk.qcow2" DISK {expected_disk}
download_one "{DATADISK_OBJECT}" "$SLICE_DIR/datadisk.qcow2" DATADISK {expected_datadisk}
{grow_block}\

status_kv STAGE booting
status_flush
sudo /usr/bin/systemctl enable "mngr-slice@{ordinal}"
sudo /usr/bin/systemctl start "mngr-slice@{ordinal}"

wait_ssh {vm_ssh_port} || fail "VM sshd did not come up on port {vm_ssh_port}"
wait_ssh {container_ssh_port} || fail "container sshd did not come up on port {container_ssh_port}"
status_kv STAGE started
status_kv FINISHED 1
status_flush
"""
    )


@pure
def render_gen2_resize_in_place_script(
    *,
    instance_name: str,
    # The machine's target size (units drive the env rewrite; the data disk is
    # grown to ``data_disk_gib`` when larger than the current virtual size).
    units: int,
    vcpus: int,
    data_disk_gib: int,
    total_units: int,
    unit_budget_mib: int,
    disk_budget_gib: int,
    uplink_mbps: int | None,
) -> str:
    """The synchronous in-place resize: re-check the budgets, rewrite the env, grow the disk.

    Run (as the slice service user) against a STOPPED machine still holding
    its slot on the origin box, before the restart re-enables the unit. Under
    the allocation flock it re-sums both budgets EXCLUDING this instance (which
    already holds a slot), refuses with the shared NO_UNITS / NO_DISK markers
    when the target does not fit, rewrites the env file at the target size
    (ports and ordinal preserved from the recorded env), and ``qemu-img
    resize``s the data disk when the target is a grow. The unit restart (and
    the in-guest oneshots) then apply the new size.

    Prints ``MNGR_RESIZE_APPLIED <vm_port> <container_port> <ordinal>``.
    """
    env_template = build_qemu_slice_env_file(
        instance_name=instance_name,
        ordinal=None,
        vcpus=vcpus,
        units=units,
        total_units=total_units,
        data_disk_gib=data_disk_gib,
        vm_ssh_host_port=GEN2_VM_SSH_PORT_PLACEHOLDER,
        container_ssh_host_port=GEN2_CONTAINER_SSH_PORT_PLACEHOLDER,
        uplink_mbps=uplink_mbps,
    )
    env_template_b64 = base64.b64encode(env_template.encode()).decode()
    budget_guard_lines = render_gen2_budget_guard_lines(
        units=units,
        data_disk_gib=data_disk_gib,
        unit_budget_mib=unit_budget_mib,
        disk_budget_gib=disk_budget_gib,
        excluded_instance_name=instance_name,
    )
    target_data_disk_bytes = data_disk_gib * 1024**3
    return (
        script_prelude(instance_name)
        + f"""\
exec 9> "$HOME/{GEN2_ALLOC_LOCK_RELPATH}"
flock 9

INSTANCES_DIR={GEN2_INSTANCES_DIR}
slice_dir="$INSTANCES_DIR/{instance_name}"
[ -f "$slice_dir/env" ] || fail "no recorded env file for {instance_name}; cannot resize in place"
ordinal=$(grep -s '^MNGR_SLICE_ORDINAL=' "$slice_dir/env" | cut -d= -f2 || true)
case "$ordinal" in ''|*[!0-9]*) fail "recorded env file has no usable ordinal" ;; esac
vm_port=$(grep -s '^MNGR_SLICE_VM_SSH_HOST_PORT=' "$slice_dir/env" | cut -d= -f2 || true)
container_port=$(grep -s '^MNGR_SLICE_CONTAINER_SSH_HOST_PORT=' "$slice_dir/env" | cut -d= -f2 || true)
case "$vm_port" in ''|*[!0-9]*) fail "recorded env file has no usable VM port" ;; esac
case "$container_port" in ''|*[!0-9]*) fail "recorded env file has no usable container port" ;; esac

# Two-budget re-check, excluding this machine's own current footprint (it
# already holds a slot; only the TARGET size must fit).
{budget_guard_lines}
# Rewrite the env file at the target size, preserving placement (ordinal,
# addresses) and ports.
{render_gen2_ordinal_derivation_lines()}
umask 077
echo {shlex.quote(env_template_b64)} | base64 -d | substitute_ordinal_tokens \\
    | sed "s/{GEN2_VM_SSH_PORT_PLACEHOLDER}/$vm_port/g; s/{GEN2_CONTAINER_SSH_PORT_PLACEHOLDER}/$container_port/g" \\
    > "$slice_dir/env.tmp"
mv "$slice_dir/env.tmp" "$slice_dir/env"

# Grow-only disk: resize the qcow2 when the target exceeds its virtual size
# (the in-guest oneshot grows the filesystem on the next boot). Top-level JSON
# key only -- qemu 10 nests child nodes carrying their own virtual-size, and a
# multi-number capture makes the integer test silently false (an if condition
# is exempt from errexit), skipping the grow.
current_bytes=$(qemu-img info --output=json "$slice_dir/datadisk.qcow2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])')
if [ {target_data_disk_bytes} -gt "$current_bytes" ]; then
    qemu-img resize -q "$slice_dir/datadisk.qcow2" {data_disk_gib}G
fi

echo "{GEN2_RESIZE_APPLIED_MARKER} $vm_port $container_port $ordinal"
"""
    )


@pure
def _parse_marker_line_with_ports_and_ordinal(stdout: str, marker: str) -> tuple[int, int, int] | None:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker):
            parts = stripped.split()
            if len(parts) == 4 and all(part.isdigit() for part in parts[1:]):
                return int(parts[1]), int(parts[2]), int(parts[3])
            return None
    return None


@pure
def parse_gen2_resize_applied_line(stdout: str) -> tuple[int, int, int] | None:
    """Parse ``MNGR_RESIZE_APPLIED <vm> <container> <ordinal>`` from the resize script's stdout."""
    return _parse_marker_line_with_ports_and_ordinal(stdout, GEN2_RESIZE_APPLIED_MARKER)


@pure
def parse_gen2_restore_reserved_line(stdout: str) -> tuple[int, int, int] | None:
    """Parse ``MNGR_RESTORE_RESERVED <vm> <container> <ordinal>`` from a gen-2 reserve's stdout."""
    return _parse_marker_line_with_ports_and_ordinal(stdout, RESTORE_RESERVED_MARKER)
