#!/bin/bash
# Start the Docker daemon inside a Modal sandbox with enable_docker=True.
# Based on Modal's Docker-in-Sandboxes demo:
# https://modal.com/docs/guide/docker-in-sandboxes
#
# This script is idempotent: if dockerd is already running, it exits early.
set -euo pipefail
set -x

DOCKERD_STARTUP_TIMEOUT_SECONDS="${DOCKERD_STARTUP_TIMEOUT_SECONDS:-60}"

# Watching the pid is what tells a crashed daemon apart from a merely slow one.
wait_for_dockerd() {
    local dockerd_pid="$1"
    local timeout_seconds="$2"
    local deadline
    deadline=$(( $(date +%s) + timeout_seconds ))
    until /usr/local/bin/docker info >/dev/null 2>&1; do
        if ! kill -0 "$dockerd_pid" 2>/dev/null; then
            echo "Error: dockerd (PID $dockerd_pid) exited before becoming ready." >&2
            return 1
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "Error: dockerd (PID $dockerd_pid) did not accept API calls within ${timeout_seconds}s." >&2
            return 1
        fi
        sleep 1
    done
}

# Guard: skip if dockerd is already running
if /usr/local/bin/docker info >/dev/null 2>&1; then
    echo "Docker daemon is already running."
    exit 0
fi

# Guard: skip if another start-dockerd.sh is in progress (PID file exists
# but daemon hasn't finished starting yet)
if [ -f /var/run/docker.pid ] && kill -0 "$(cat /var/run/docker.pid)" 2>/dev/null; then
    existing_pid="$(cat /var/run/docker.pid)"
    echo "Docker daemon process exists (PID $existing_pid), waiting for it..."
    wait_for_dockerd "$existing_pid" "$DOCKERD_STARTUP_TIMEOUT_SECONDS"
    echo "Docker daemon is ready."
    exit 0
fi

dev=$(ip route show default | awk '/default/ {print $5}')
if [ -z "$dev" ]; then
    echo "Error: No default device found."
    ip route show
    exit 1
else
    echo "Default device: $dev"
fi
addr=$(ip addr show dev "$dev" | grep -w inet | awk '{print $2}' | cut -d/ -f1)
if [ -z "$addr" ]; then
    echo "Error: No IP address found for device $dev."
    ip addr show dev "$dev"
    exit 1
else
    echo "IP address for $dev: $addr"
fi

echo 1 > /proc/sys/net/ipv4/ip_forward

# Prefer IPv4 over IPv6 for the daemon's own resolver. Modal sandbox
# IPv6 routing to Docker Hub (registry-1.docker.io, auth.docker.io) is
# unreliable -- getaddrinfo can return IPv6 addresses that then fail to
# connect. Without this, image pulls intermittently fail.
cat > /etc/gai.conf <<'EOF'
precedence ::ffff:0:0/96 100
EOF

# Also try to disable IPv6 at the kernel level as a belt-and-suspenders
# fallback (may be read-only in gVisor; tolerate failure).
echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || true
echo 1 > /proc/sys/net/ipv6/conf/default/disable_ipv6 2>/dev/null || true

# SNAT rules for outbound NAT from Docker containers.
# Required for container internet access with --iptables=false.
# Try SNAT first; fall back to MASQUERADE if SNAT is unsupported.
nat_ok=false
if iptables-legacy -t nat -A POSTROUTING -o "$dev" -j SNAT --to-source "$addr" -p tcp 2>/dev/null && \
   iptables-legacy -t nat -A POSTROUTING -o "$dev" -j SNAT --to-source "$addr" -p udp 2>/dev/null; then
    echo "SNAT rules installed successfully."
    nat_ok=true
elif iptables-legacy -t nat -A POSTROUTING -o "$dev" -j MASQUERADE 2>/dev/null; then
    echo "SNAT failed; MASQUERADE rule installed as fallback."
    nat_ok=true
fi

if [ "$nat_ok" = false ]; then
    echo "WARNING: Could not install NAT rules. Docker containers will have no internet access."
fi

# gVisor doesn't support nftables yet (https://github.com/google/gvisor/issues/10510).
# The iptables/ip6tables alternatives are pinned to the legacy backend at image
# build time in Dockerfile.release, so no runtime update-alternatives call is needed here.

# Override /etc/resolv.conf to use public DNS servers directly. The
# daemon itself uses the host's resolver for image pulls (the --dns flag
# only affects containers), and the sandbox's default resolver in gVisor
# sometimes returns unreachable addresses or fails lookups entirely.
#
# On some Modal sandboxes /etc/resolv.conf is on a read-only overlay;
# tolerate that (dockerd --dns=... still works for container resolution).
cat > /etc/resolv.conf <<'EOF' || echo "Warning: could not write /etc/resolv.conf (read-only); relying on --dns flags."
nameserver 1.1.1.1
nameserver 8.8.8.8
options single-request-reopen
EOF

# Disable IPv6 in dockerd -- Modal sandbox IPv6 routing to Docker Hub
# (registry-1.docker.io, auth.docker.io) is unreliable, causing
# "network is unreachable" errors on image pulls. Force IPv4-only.
# Use explicit public DNS (1.1.1.1, 8.8.8.8) for containers too -- the
# Docker bridge's default DNS forwarder cannot resolve upstream.
dockerd --iptables=false --ip6tables=false --ipv6=false --dns=1.1.1.1 --dns=8.8.8.8 &
wait_for_dockerd "$!" "$DOCKERD_STARTUP_TIMEOUT_SECONDS"
echo "Docker daemon is ready."
