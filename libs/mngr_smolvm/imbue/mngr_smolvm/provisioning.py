import shlex
import socket

from imbue.imbue_common.pure import pure


@pure
def build_ssh_provisioning_script(
    host_private_key_pem: str,
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
    """
    private_key_quoted = shlex.quote(host_private_key_pem)
    public_key_quoted = shlex.quote(host_public_key_openssh.strip())
    authorized_key_quoted = shlex.quote(client_authorized_public_key.strip())
    return f"""set -e
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
printf '%s' {private_key_quoted} > /etc/ssh/ssh_host_ed25519_key
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
if [ -f /run/sshd_mngr.pid ] && kill -0 "$(cat /run/sshd_mngr.pid)" 2>/dev/null; then
    :
else
    "$(command -v sshd)" -f /etc/ssh/sshd_config_mngr
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
