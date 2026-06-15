#!/usr/bin/env bash
# Cancel a ToDesktop build by ID.
#
# Usage: bash cancel-todesktop-build.sh <APP_ID> <BUILD_ID>
#
# Requires env: TODESKTOP_EMAIL, TODESKTOP_ACCESS_TOKEN.
#
# ToDesktop's CLI doesn't expose a `cancel` subcommand, but its dashboard
# uses a `cancelBuild` Firebase Function. This script replicates the auth
# flow the CLI does internally:
#
#   1. POST /loginWithAccessToken {email, accessToken} -> custom token
#   2. POST https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken
#      with the custom token + the bundled Firebase web API key -> ID token
#   3. POST /cancelBuild with `Authorization: Bearer <ID token>` and
#      callable body shape `{"data": {"appId": "...", "buildId": "..."}}`
#
# Reverse-engineered from @todesktop/cli@1.24.0's bundled cli.js + .env.
set -euo pipefail

APP_ID="${1:?usage: cancel-todesktop-build.sh APP_ID BUILD_ID}"
BUILD_ID="${2:?usage: cancel-todesktop-build.sh APP_ID BUILD_ID}"

: "${TODESKTOP_EMAIL:?TODESKTOP_EMAIL must be set}"
: "${TODESKTOP_ACCESS_TOKEN:?TODESKTOP_ACCESS_TOKEN must be set}"

FB_FUNCTIONS_BASE="https://us-central1-todesktop-prod1.cloudfunctions.net"
FB_API_KEY="AIzaSyB3bfv74OgIezU160UPDVXDP6c1ApNfc6M"

log() { printf '[cancel-td] %s\n' "$*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

log "exchanging access token for Firebase custom token"
LOGIN_RESP=$(curl -s -X POST "$FB_FUNCTIONS_BASE/loginWithAccessToken" \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"email":"%s","accessToken":"%s"}' "$TODESKTOP_EMAIL" "$TODESKTOP_ACCESS_TOKEN")")
# loginWithAccessToken returns the raw JWT as a JSON string literal.
CUSTOM_TOKEN=$(echo "$LOGIN_RESP" | python3 -c 'import json,sys; v=json.load(sys.stdin); print(v if isinstance(v,str) else "")' 2>/dev/null || echo "")
if [[ -z "$CUSTOM_TOKEN" || "$CUSTOM_TOKEN" != ey* ]]; then
  fail "loginWithAccessToken failed: $LOGIN_RESP"
fi
log "  got custom token (${#CUSTOM_TOKEN} chars)"

log "exchanging custom token for Firebase ID token"
SIGN_IN_RESP=$(curl -s -X POST \
  "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=$FB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"token":"%s","returnSecureToken":true}' "$CUSTOM_TOKEN")")
ID_TOKEN=$(echo "$SIGN_IN_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("idToken",""))' 2>/dev/null || echo "")
if [[ -z "$ID_TOKEN" ]]; then
  fail "signInWithCustomToken failed: $SIGN_IN_RESP"
fi
log "  got ID token (${#ID_TOKEN} chars)"

log "POST $FB_FUNCTIONS_BASE/cancelBuild appId=$APP_ID buildId=$BUILD_ID"
CANCEL_RESP=$(curl -s -X POST "$FB_FUNCTIONS_BASE/cancelBuild" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ID_TOKEN" \
  -d "$(printf '{"data":{"appId":"%s","buildId":"%s"}}' "$APP_ID" "$BUILD_ID")")
log "  response: $CANCEL_RESP"

# Callable response is either {"result":...} on success or {"error":{...}} on failure.
if echo "$CANCEL_RESP" | python3 -c 'import json,sys; sys.exit(0 if "error" not in json.load(sys.stdin) else 1)' 2>/dev/null; then
  log "SUCCESS"
  exit 0
else
  fail "cancelBuild returned an error"
fi
