#!/usr/bin/env bash
set -euo pipefail

# Cleanup function
cleanup() {
    echo "Cleaning up..."
    docker rm -f frp-test-container 2>/dev/null || true
    pkill -f "frps -c frps.toml" 2>/dev/null || true
}
trap cleanup EXIT

# Start frps on host
echo "=== Starting frps on host ==="
frps -c frps.toml &
sleep 2

# Start container and run everything inside
echo "=== Starting container and services ==="
docker run -d --name frp-test-container \
    --add-host=host.docker.internal:host-gateway \
    ubuntu:22.04 \
    sleep infinity

# Copy configs into container
docker cp frpc-foo.toml frp-test-container:/frpc-foo.toml
docker cp frpc-bar.toml frp-test-container:/frpc-bar.toml

docker cp foo.py frp-test-container:/foo.py
docker cp bar.py frp-test-container:/bar.py

# Install dependencies and start services inside container
docker exec frp-test-container bash -c '
    apt-get update && apt-get install -y python3 wget

    # Download frp
    wget -q https://github.com/fatedier/frp/releases/download/v0.61.1/frp_0.61.1_linux_amd64.tar.gz
    tar xzf frp_0.61.1_linux_amd64.tar.gz
    cp frp_0.61.1_linux_amd64/frpc /usr/local/bin/

    # Start two simple HTTP servers
    python3 /foo.py >& /tmp/foo.py.out &
    python3 /bar.py >& /tmp/bar.py.out &
    sleep 1

    # Start frpc instances
    frpc -c /frpc-foo.toml >& /tmp/foo.frpc.out &
    frpc -c /frpc-bar.toml >& /tmp/bar.frpc.out &
    sleep 2
'

echo "=== Testing ==="
sleep 2

echo "Testing foo.container1.localhost:"
curl -s -H "Host: foo.container1.localhost" http://localhost:8080
echo

echo "Testing bar.container1.localhost:"
curl -s -H "Host: bar.container1.localhost" http://localhost:8080
echo

echo "=== Test complete ==="
