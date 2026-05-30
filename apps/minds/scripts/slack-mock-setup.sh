#!/usr/bin/env bash
# Provision a localhost slack mock for a running lima VM agent.
#
# Architecture:
#
#   agent (inside lima VM) --[curl https://slack.com/...]--+
#                                                          |
#                              /etc/hosts inside VM --> 192.168.5.2 (host.lima.internal)
#                                                          |
#                              macOS host :443 socat ------+
#                                  (TLS terminate)
#                                          |
#                              127.0.0.1:8443 plain HTTP node mock
#
# Requires sudo on the macOS host (to bind :443 and run socat) and
# passwordless sudo inside the lima VM (default for lima ubuntu user).
#
# Inputs:
#   VM_NAME       lima VM name (required)
#
# Outputs (consumed by slack-mock-teardown.sh and CI artifacts):
#   /tmp/slack-mock/cert.pem  /tmp/slack-mock/key.pem
#   /tmp/slack-mock/mock.pid  /tmp/slack-mock/socat.pid
#   /tmp/slack-mock/mock.log
set -uo pipefail

VM_NAME="${VM_NAME:?VM_NAME is required (lima VM hosting the agent)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOCK_SCRIPT="$SCRIPT_DIR/../test/e2e/mocks/slack-mock-server.js"
STATE_DIR=/tmp/slack-mock
HOST_LIMA_IP=192.168.5.2
# Pin to the lima bundled with the .app so its version matches the VM image.
LIMA_BIN_DIR=/Applications/Minds.app/Contents/Resources/lima/bin
export PATH="$LIMA_BIN_DIR:$PATH"

log() { printf '[slack-mock-setup] %s\n' "$*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

[[ -f "$MOCK_SCRIPT" ]] || fail "mock script not at $MOCK_SCRIPT"
command -v limactl >/dev/null || fail "limactl not on PATH"
command -v socat   >/dev/null || fail "socat not on PATH (brew install socat)"
command -v node    >/dev/null || fail "node not on PATH"

mkdir -p "$STATE_DIR"

# 1. Self-signed cert covering slack.com + files.slack.com.
if [[ ! -f "$STATE_DIR/cert.pem" ]]; then
  log "generating self-signed cert"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$STATE_DIR/key.pem" -out "$STATE_DIR/cert.pem" \
    -days 1 -subj "/CN=slack.com" \
    -addext "subjectAltName=DNS:slack.com,DNS:files.slack.com" \
    >/dev/null 2>&1 || fail "openssl req failed"
fi

# 2. Trust the cert inside the lima VM (system CA bundle).
log "installing cert in $VM_NAME CA store"
limactl copy "$STATE_DIR/cert.pem" "$VM_NAME:/tmp/slack-mock-ca.crt" \
  || fail "limactl copy cert -> $VM_NAME failed"
limactl shell "$VM_NAME" -- sudo bash -c '
  set -e
  cp /tmp/slack-mock-ca.crt /usr/local/share/ca-certificates/slack-mock.crt
  update-ca-certificates 2>&1 | tail -1
' || fail "update-ca-certificates failed in $VM_NAME"

# 3. /etc/hosts inside the VM: slack.com / files.slack.com -> host.lima.internal.
log "patching /etc/hosts in $VM_NAME"
limactl shell "$VM_NAME" -- sudo bash -c "
  set -e
  marker='# slack-mock'
  sed -i.bak \"/\$marker/d\" /etc/hosts
  sed -i \"/^${HOST_LIMA_IP} slack.com /d\" /etc/hosts
  echo '${HOST_LIMA_IP} slack.com files.slack.com  # slack-mock' >> /etc/hosts
" || fail "/etc/hosts patch failed in $VM_NAME"

# 4. Pre-seed latchkey slack creds (so cred-presence check passes).
log "pre-seeding latchkey slack creds in $VM_NAME"
limactl shell "$VM_NAME" -- bash -c '
  set -e
  latchkey auth set slack -H "Authorization: Bearer xoxc-ci-mock-token"
' || fail "latchkey auth set failed in $VM_NAME"

# 5. Start node mock on host:8443 (plain HTTP).
log "starting node mock on 127.0.0.1:8443"
SLACK_MOCK_PORT=8443 node "$MOCK_SCRIPT" \
  > "$STATE_DIR/mock.log" 2>&1 &
echo $! > "$STATE_DIR/mock.pid"
sleep 1
curl -sf http://127.0.0.1:8443/api/auth.test >/dev/null \
  || fail "mock not responding on 127.0.0.1:8443"

# 6. socat: TLS terminate :443 on the lima-visible IP, forward to 127.0.0.1:8443.
log "starting socat TLS terminator on 0.0.0.0:443 -> 127.0.0.1:8443"
sudo socat -d \
  OPENSSL-LISTEN:443,bind=0.0.0.0,reuseaddr,fork,verify=0,cert="$STATE_DIR/cert.pem",key="$STATE_DIR/key.pem" \
  TCP:127.0.0.1:8443 \
  > "$STATE_DIR/socat.log" 2>&1 &
SOCAT_PID=$!
echo $SOCAT_PID | sudo tee "$STATE_DIR/socat.pid" >/dev/null

# 7. End-to-end sanity check: agent's perspective.
log "verifying TLS reach from inside $VM_NAME"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  body=$(limactl shell "$VM_NAME" -- curl -sf --max-time 5 \
    https://slack.com/api/auth.test 2>&1) && break
  sleep 1
done
case "$body" in
  *Imbue\ CI\ Mock*) log "mock reachable from VM (got team=Imbue CI Mock)";;
  *)                 fail "mock NOT reachable from VM. Last response: $body";;
esac

log "OK"
