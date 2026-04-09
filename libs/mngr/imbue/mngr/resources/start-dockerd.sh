#!/bin/bash
# Start the Docker daemon inside a Modal sandbox with enable_docker=True.
# Based on Modal's Docker-in-Sandboxes demo:
# https://modal.com/docs/guide/docker-in-sandboxes
#
# This script is idempotent: if dockerd is already running, it exits early.
set -euxo pipefail

# Guard: skip if dockerd is already running
if /usr/local/bin/docker info >/dev/null 2>&1; then
    echo "Docker daemon is already running."
    exit 0
fi

# Guard: skip if another start-dockerd.sh is in progress (PID file exists
# but daemon hasn't finished starting yet)
if [ -f /var/run/docker.pid ] && kill -0 "$(cat /var/run/docker.pid)" 2>/dev/null; then
    echo "Docker daemon process exists (PID $(cat /var/run/docker.pid)), waiting for it..."
    timeout 30 sh -c 'until /usr/local/bin/docker info >/dev/null 2>&1; do sleep 1; done'
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
update-alternatives --set iptables /usr/sbin/iptables-legacy
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy

dockerd --iptables=false --ip6tables=false &

# Wait for Docker daemon to be ready
timeout 30 sh -c 'until /usr/local/bin/docker info >/dev/null 2>&1; do sleep 1; done'
echo "Docker daemon is ready."
