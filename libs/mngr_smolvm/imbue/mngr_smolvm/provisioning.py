import shlex
import socket
from typing import Final

from imbue.imbue_common.pure import pure

# Guest environment variable carrying the sshd host private key PEM. The key
# is injected via `smolvm machine exec --secret-file`, which resolves the file
# host-side into the exec environment, so the key never appears in host argv
# (argv is world-readable via /proc/<pid>/cmdline).
HOST_PRIVATE_KEY_ENV_VAR: Final[str] = "MNGR_SSH_HOST_KEY"


@pure
def build_ssh_provisioning_script(
    host_public_key_openssh: str,
    client_authorized_public_key: str,
) -> str:
    """Build the idempotent in-guest script that installs and starts sshd.

    Runs as root inside the VM (bare mode) or the workload container (image
    mode) via `smolvm machine exec`. Installs openssh and the base packages
    mngr expects on a host (tmux, git, rsync, jq, curl) using whichever
    package manager the guest has, injects the pre-generated sshd host key
    (so the host-side known_hosts entry is valid without a keyscan),
    authorizes mngr's client key for root, and starts sshd with a dedicated
    config file. Safe to re-run on every host start: package installs are
    skipped when sshd is already present and an already-running sshd is left
    alone.

    The private key is NOT embedded in the script (which travels through
    host-side argv); it is read from the ``HOST_PRIVATE_KEY_ENV_VAR``
    environment variable, injected per exec via ``--secret-file``. smolvm's
    secret injection strips the file's trailing newline, so the script
    re-appends exactly one when writing the key file.
    """
    public_key_quoted = shlex.quote(host_public_key_openssh.strip())
    authorized_key_quoted = shlex.quote(client_authorized_public_key.strip())
    return f"""set -e
if [ -z "${{{HOST_PRIVATE_KEY_ENV_VAR}:-}}" ]; then
    echo "{HOST_PRIVATE_KEY_ENV_VAR} is not set (expected via smolvm machine exec --secret-file)" >&2
    exit 8
fi
if ! command -v sshd >/dev/null 2>&1; then
    if command -v apk >/dev/null 2>&1; then
        apk add -q openssh tmux git rsync jq curl bash
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq >/dev/null
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server tmux git rsync jq curl >/dev/null
    else
        echo "no supported package manager (apk/apt-get) found in guest" >&2
        exit 7
    fi
fi
mkdir -p /etc/ssh /run/sshd /var/empty /root/.ssh
chown root /var/empty /run/sshd
chmod 755 /var/empty /run/sshd
# Unprivileged rootfs builds can leave /root owned by the build user; sshd's
# StrictModes rejects authorized_keys under a foreign-owned home directory.
chown root:root /root /root/.ssh
chmod 700 /root /root/.ssh
# host_dir is a virtiofs mount owned by the host-side user in the exposed
# layout; git (running as root in the guest) refuses such repos without a
# safe.directory exception. The whole VM is single-user, so allow all.
git config --global --add safe.directory '*' 2>/dev/null || true
printf '%s\\n' "${{{HOST_PRIVATE_KEY_ENV_VAR}}}" > /etc/ssh/ssh_host_ed25519_key
unset {HOST_PRIVATE_KEY_ENV_VAR}
chmod 600 /etc/ssh/ssh_host_ed25519_key
printf '%s\\n' {public_key_quoted} > /etc/ssh/ssh_host_ed25519_key.pub
touch /root/.ssh/authorized_keys
grep -qF {authorized_key_quoted} /root/.ssh/authorized_keys || printf '%s\\n' {authorized_key_quoted} >> /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
cat > /etc/ssh/sshd_config_mngr <<'SSHDCFG'
Port 22
HostKey /etc/ssh/ssh_host_ed25519_key
PermitRootLogin prohibit-password
PasswordAuthentication no
MaxSessions 100
MaxStartups 100:30:200
Subsystem sftp internal-sftp
PidFile /run/sshd_mngr.pid
SSHDCFG
# Start sshd, tolerating an instance that is already listening. A pidfile
# liveness check is unreliable here: each smolvm exec session observes its
# own PID namespace, so recorded pids are meaningless across sessions. sshd
# itself is the truthful probe -- a second start fails with "Address already
# in use", which means we are already done.
sshd_output=$("$(command -v sshd)" -f /etc/ssh/sshd_config_mngr 2>&1) && sshd_rc=0 || sshd_rc=$?
if [ "$sshd_rc" != "0" ]; then
    case "$sshd_output" in
        *"already in use"*) : ;;
        *) echo "$sshd_output" >&2; exit "$sshd_rc" ;;
    esac
fi
echo MNGR_PROVISION_OK
"""


@pure
def build_shutdown_script(host_dir: str, poweroff_sentinel_path: str) -> str:
    """Build the shutdown.sh script content installed on smolvm hosts.

    The script asks the smolvm guest agent for a clean VM shutdown by
    creating the poweroff sentinel file (the agent watches for it, syncs
    filesystems, and exits the VM).
    """
    return f"""#!/bin/sh
# Auto-generated shutdown script for mngr smolvm host.
# Touches the smolvm poweroff sentinel; the guest agent syncs and powers off.

LOG_FILE="{host_dir}/logs/shutdown.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() {{
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
    echo "$*"
}}

log "=== Shutdown script started ==="
log "STOP_REASON: ${{1:-PAUSED}}"

touch {poweroff_sentinel_path}
"""


def allocate_free_tcp_port() -> int:
    """Allocate a free localhost TCP port for the VM's sshd forward.

    Binds port 0 to let the OS choose, then releases it. There is a small
    window where another process could grab the port before smolvm binds it;
    machine creation fails loudly in that case and can be retried.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return int(port)
