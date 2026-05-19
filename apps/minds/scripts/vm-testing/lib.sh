#!/usr/bin/env bash
# Shared helpers for vm-testing scripts. Source this; do not execute.

set -euo pipefail

VM_USER="${VM_USER:-admin}"
VM_PASSWORD="${VM_PASSWORD:-admin}"
BASE_IMAGE="${BASE_IMAGE:-ghcr.io/cirruslabs/macos-tahoe-vanilla:latest}"

# Disable host key checking and Kerberos prompts; we never reuse identities
# across throwaway VMs and the VM's host key changes every clone.
SSH_OPTS=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o GSSAPIAuthentication=no
    -o ConnectTimeout=5
)

log() {
    printf '[vm-testing %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2
}

die() {
    log "ERROR: $*"
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

# Wait for the named VM's IP to appear, then for sshd to accept logins.
# Echoes the IP to stdout on success.
wait_for_vm_ssh() {
    local vm_name="$1"
    local ip_timeout="${2:-120}"
    local ssh_timeout="${3:-120}"
    local ip
    ip=$(tart ip "$vm_name" --wait "$ip_timeout") || die "VM did not get IP within ${ip_timeout}s"
    log "VM $vm_name has IP $ip; waiting for sshd..."
    local deadline=$(( SECONDS + ssh_timeout ))
    while (( SECONDS < deadline )); do
        if sshpass -p "$VM_PASSWORD" ssh "${SSH_OPTS[@]}" "$VM_USER@$ip" true 2>/dev/null; then
            log "SSH ready on $ip"
            printf '%s' "$ip"
            return 0
        fi
        sleep 2
    done
    die "sshd not ready on $ip within ${ssh_timeout}s"
}

vm_ssh() {
    local ip="$1"; shift
    sshpass -p "$VM_PASSWORD" ssh "${SSH_OPTS[@]}" "$VM_USER@$ip" "$@"
}

vm_scp() {
    sshpass -p "$VM_PASSWORD" scp "${SSH_OPTS[@]}" "$@"
}

vm_running() {
    local vm_name="$1"
    tart list --format json 2>/dev/null \
        | python3 -c "
import json, sys
data = json.load(sys.stdin)
for vm in data:
    if vm.get('Name') == '$vm_name' and vm.get('State') == 'running':
        sys.exit(0)
sys.exit(1)
"
}

stop_and_delete_vm() {
    local vm_name="$1"
    if vm_running "$vm_name"; then
        log "stopping VM $vm_name"
        tart stop "$vm_name" 2>&1 | sed 's/^/[tart-stop] /' >&2 || true
    fi
    if tart list 2>/dev/null | awk 'NR>1 {print $2}' | grep -Fxq "$vm_name"; then
        log "deleting VM $vm_name"
        tart delete "$vm_name" 2>&1 | sed 's/^/[tart-delete] /' >&2 || true
    fi
}
