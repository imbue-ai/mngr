#!/usr/bin/env bash
# Provision a localhost slack mock for a running minds.app agent.
#
# Architecture: the latchkey gateway runs on the macOS HOST (started by
# minds.app), not inside the lima VM. The agent connects to 127.0.0.1:1989
# via a reverse-SSH tunnel into the host's gateway, and the gateway makes
# the outbound `slack.com` call from the host. So the interception layer
# lives entirely on the host:
#
#   agent (in VM) --[SSH tunnel 127.0.0.1:1989]--> macOS host's latchkey gateway
#                                                          |
#                       /etc/hosts on host --> 127.0.0.1
#                                                          |
#                       socat :443 (TLS) ----> 127.0.0.1:8443 plain HTTP node mock
#
# Requires NOPASSWD sudo on the runner (for /etc/hosts, trust-cert install,
# and socat :443 bind).
#
# Outputs (consumed by slack-mock-teardown.sh):
#   /tmp/slack-mock/cert.pem  /tmp/slack-mock/key.pem
#   /tmp/slack-mock/mock.pid  /tmp/slack-mock/socat.pid
#   /tmp/slack-mock/mock.log  /tmp/slack-mock/socat.log
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOCK_SCRIPT="$SCRIPT_DIR/../test/e2e/mocks/slack-mock-server.js"
STATE_DIR=/tmp/slack-mock
LATCHKEY_BIN=/Applications/Minds.app/Contents/Resources/latchkey/bin/latchkey
LATCHKEY_DIRECTORY="$HOME/.minds/latchkey"
# The bundled latchkey shim runs Electron-as-node and needs the .app's
# Electron binary path to resolve `process.execPath`.
export MINDS_ELECTRON_EXEC_PATH=/Applications/Minds.app/Contents/MacOS/Minds

log() { printf '[slack-mock-setup] %s\n' "$*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

[[ -f "$MOCK_SCRIPT" ]]     || fail "mock script not at $MOCK_SCRIPT"
[[ -x "$LATCHKEY_BIN" ]]    || fail "bundled latchkey not at $LATCHKEY_BIN (minds.app not installed?)"
[[ -f "$LATCHKEY_DIRECTORY/encryption_key" ]] \
                            || fail "$LATCHKEY_DIRECTORY/encryption_key missing -- minds.app gateway never started?"
command -v socat   >/dev/null || fail "socat not on PATH (brew install socat)"
command -v node    >/dev/null || fail "node not on PATH"

mkdir -p "$STATE_DIR"

# 1. Self-signed cert covering slack.com + files.slack.com.
if [[ ! -f "$STATE_DIR/cert.pem" ]]; then
  log "generating self-signed cert for slack.com"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$STATE_DIR/key.pem" -out "$STATE_DIR/cert.pem" \
    -days 1 -subj "/CN=slack.com" \
    -addext "subjectAltName=DNS:slack.com,DNS:files.slack.com" \
    >/dev/null 2>&1 || fail "openssl req failed"
fi

# 2. Trust the cert at the system level (curl-darwinssl uses System keychain).
# On non-interactive macOS, `security add-trusted-cert -d` triggers the
# auth-prompt code path even with sudo and fails with
#   "SecTrustSettingsSetTrustSettings: ... no user interaction was possible"
# unless we pre-grant trust-settings.admin in the authorization DB.
log "trusting cert in /Library/Keychains/System.keychain"
sudo security authorizationdb write com.apple.trust-settings.admin allow \
  >/dev/null 2>&1 || true
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain "$STATE_DIR/cert.pem" \
  || fail "security add-trusted-cert failed (authorization DB pre-grant didn't take?)"
# Revert the authorization DB to its default. Best-effort -- losing the
# default is annoying but not a teardown blocker.
sudo security authorizationdb remove com.apple.trust-settings.admin \
  >/dev/null 2>&1 || true

# 3. /etc/hosts on the host: slack.com / files.slack.com -> 127.0.0.1.
log "patching /etc/hosts"
sudo sed -i.bak '/# slack-mock/d' /etc/hosts
echo '127.0.0.1 slack.com files.slack.com  # slack-mock' \
  | sudo tee -a /etc/hosts >/dev/null

# 4. Pre-seed latchkey slack creds (host-side; gateway picks up on next req).
log "pre-seeding latchkey slack creds"
LATCHKEY_DIRECTORY="$LATCHKEY_DIRECTORY" \
  "$LATCHKEY_BIN" auth set slack \
    -H "Authorization: Bearer xoxc-ci-mock-token" \
  || fail "latchkey auth set slack failed"

# 5. Start node mock on 127.0.0.1:8443 (plain HTTP).
log "starting node mock on 127.0.0.1:8443"
SLACK_MOCK_PORT=8443 node "$MOCK_SCRIPT" \
  > "$STATE_DIR/mock.log" 2>&1 &
echo $! > "$STATE_DIR/mock.pid"
sleep 1
curl -sf http://127.0.0.1:8443/api/auth.test >/dev/null \
  || fail "mock not responding on 127.0.0.1:8443"

# 6. socat: TLS terminate :443 on 127.0.0.1, forward to 127.0.0.1:8443.
log "starting socat TLS terminator on 127.0.0.1:443 -> 127.0.0.1:8443"
sudo socat -d \
  OPENSSL-LISTEN:443,bind=127.0.0.1,reuseaddr,fork,verify=0,cert="$STATE_DIR/cert.pem",key="$STATE_DIR/key.pem" \
  TCP:127.0.0.1:8443 \
  > "$STATE_DIR/socat.log" 2>&1 &
SOCAT_PID=$!
echo $SOCAT_PID | sudo tee "$STATE_DIR/socat.pid" >/dev/null

# 7. End-to-end sanity check from the host's perspective.
log "verifying TLS reach as the gateway would see it"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  body=$(curl -sf --max-time 5 https://slack.com/api/auth.test 2>&1) && break
  sleep 1
done
case "$body" in
  *Imbue\ CI\ Mock*) log "mock reachable (got team=Imbue CI Mock)";;
  *)                 fail "mock NOT reachable. Last response: $body";;
esac

log "OK"
