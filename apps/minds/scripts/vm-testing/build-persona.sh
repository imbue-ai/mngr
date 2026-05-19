#!/usr/bin/env bash
# Build a reusable Tart persona image from the cirruslabs vanilla base.
#
# Usage: build-persona.sh <persona-name>
#
# Clones the base image to a build VM, runs personas/<persona-name>.sh inside
# it over SSH, shuts the VM down, and leaves it registered as a local Tart
# image named <persona-name>. Subsequent `tart clone <persona-name> ...` calls
# produce a fresh, provisioned VM in seconds.

set -euo pipefail

HERE="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$HERE/lib.sh"

require_cmd tart
require_cmd sshpass

persona="${1:-}"
if [[ -z "$persona" ]]; then
    die "usage: $0 <persona-name>"
fi

persona_script="$HERE/personas/$persona.sh"
[[ -f "$persona_script" ]] || die "no persona script at $persona_script"

if tart list 2>/dev/null | awk 'NR>1 {print $2}' | grep -Fxq "$persona"; then
    log "persona image '$persona' already exists; deleting before rebuild"
    tart delete "$persona" 2>&1 | sed 's/^/[tart-delete] /' >&2 || true
fi

build_vm="$persona-build-$$"
trap 'stop_and_delete_vm "$build_vm"' EXIT

log "pulling base image $BASE_IMAGE (no-op if cached)"
tart pull "$BASE_IMAGE" 2>&1 | sed 's/^/[tart-pull] /' >&2

log "cloning base into build VM $build_vm"
tart clone "$BASE_IMAGE" "$build_vm"

# Use --vnc-experimental even for build runs: starting tart from a non-
# interactive shell otherwise stalls waiting for an attached GUI session.
log "booting build VM $build_vm"
tart run "$build_vm" --vnc-experimental &
boot_pid=$!

# If `tart run` exits early (e.g. image error) we want a clear signal.
sleep 2
if ! kill -0 "$boot_pid" 2>/dev/null; then
    die "tart run exited prematurely"
fi

ip=$(wait_for_vm_ssh "$build_vm" 180 180)

log "uploading persona provisioning script"
vm_scp "$persona_script" "$VM_USER@$ip:/tmp/persona.sh"

log "running persona provisioning script"
vm_ssh "$ip" "chmod +x /tmp/persona.sh && /tmp/persona.sh" 2>&1 \
    | sed "s/^/[$persona] /" >&2

log "shutting down build VM cleanly"
vm_ssh "$ip" "sudo shutdown -h now" || true

# Wait for the boot process to exit. `tart stop` is a fallback in case the
# guest never finishes its shutdown sequence.
for _ in $(seq 1 60); do
    if ! kill -0 "$boot_pid" 2>/dev/null; then
        break
    fi
    sleep 1
done
if kill -0 "$boot_pid" 2>/dev/null; then
    log "guest did not shut down; forcing stop"
    tart stop "$build_vm" 2>&1 | sed 's/^/[tart-stop] /' >&2 || true
    wait "$boot_pid" 2>/dev/null || true
fi

log "renaming build VM to persona image '$persona'"
tart rename "$build_vm" "$persona"
trap - EXIT

log "persona '$persona' is ready (tart list will show it)"
