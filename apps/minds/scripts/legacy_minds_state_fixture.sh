#!/usr/bin/env bash
# Seed a realistic pre-migration ~/.<root_name> tree, and afterwards assert the
# app moved it onto the canonical roots.
#
# Why this exists: mac-runner-reset.sh deletes the legacy dotfolder along with
# the three canonical roots, so an app launched after a reset finds nothing to
# migrate and takes its no-legacy-dir path. Without seeding, a green
# launch-to-msg run says nothing at all about the migration -- which is the one
# part of the move that touches a real user's SSH keys and auth sessions.
#
# Usage:
#   legacy_minds_state_fixture.sh seed     # after reset + install, before first launch
#   legacy_minds_state_fixture.sh verify   # after the app has launched at least once
set -euo pipefail

# CLEANUP: delete this script and the two minds-launch-to-msg.yml steps that call
# it once the migration it exercises is gone (specs/minds-platform-canonical-dirs/spec.md, "Retiring the migration").

MODE="${1:?usage: legacy_minds_state_fixture.sh <seed|verify>}"

# The app name the canonical roots are keyed on: MINDS_APP_NAME in
# electron/platform-roots.js, which tracks `productName` in package.json.
APP_NAME="Imbue Studio"
APP_BUNDLE="/Applications/$APP_NAME.app"
APP_ROOT_NAME_FILE="$APP_BUNDLE/Contents/Resources/pyproject/imbue/minds/config/envs/_bundled/root_name"

# Which tier the installed build writes as. Mirrors `_bundled_root_name` in
# launch_to_msg_e2e.py: a staging build owns ~/.minds-staging, not ~/.minds.
if [[ -n "${MINDS_ROOT_NAME:-}" ]]; then
    ROOT_NAME="$MINDS_ROOT_NAME"
elif [[ -f "$APP_ROOT_NAME_FILE" ]]; then
    ROOT_NAME="$(tr -d '[:space:]' < "$APP_ROOT_NAME_FILE")"
else
    ROOT_NAME="minds"
fi
if [[ "$ROOT_NAME" == "minds" ]]; then
    TIER="production"
else
    TIER="${ROOT_NAME#minds-}"
fi

LEGACY_DIR="$HOME/.$ROOT_NAME"
STATE_ROOT="$HOME/Library/Application Support/$APP_NAME/$TIER"
CACHE_ROOT="$HOME/Library/Caches/$APP_NAME/$TIER"
LOGS_ROOT="$HOME/Library/Logs/$APP_NAME/$TIER"

PROFILE_ID="ci0legacy0profile"
HOST_STATE_JSON="$LEGACY_DIR/mngr/profiles/$PROFILE_ID/providers/lima/lima/state/host_state/host-ci0legacy.json"
SSH_KEY_REL="mngr/profiles/$PROFILE_ID/keys/root_ssh_key"
COMPLETION_REL="mngr/completions/mngr.zsh"
CANARY_REL="legacy-canary.txt"
TRANSCRIPT_REL="mngr/preserved/agent-legacy/events.jsonl"
LATCHKEY_HOST_FILE_REL="latchkey/mngr_latchkey/hosts/host-ci0legacy/latchkey_permissions.json"
LATCHKEY_HANDLE_REL="latchkey/mngr_latchkey/permissions/ci0legacy.json"

log() { echo "[legacy-fixture] $*"; }

seed() {
    if [[ -e "$LEGACY_DIR" ]]; then
        echo "ERROR: $LEGACY_DIR already exists; reset should have removed it before seeding." >&2
        exit 1
    fi
    log "seeding $LEGACY_DIR (tier=$TIER)"

    # A secret whose loss would be visible, under the subtree that must land on state.
    mkdir -p "$LEGACY_DIR/auth/sessions"
    printf '{"refreshToken":"ci-legacy-refresh"}' > "$LEGACY_DIR/auth/sessions/ci-legacy-account.json"

    # An SSH key plus the host-state record that points at it by absolute path.
    # If the rewrite misses this, every pre-existing agent loses SSH access.
    mkdir -p "$(dirname "$LEGACY_DIR/$SSH_KEY_REL")" "$(dirname "$HOST_STATE_JSON")"
    printf 'CI-LEGACY-PRIVATE-KEY\n' > "$LEGACY_DIR/$SSH_KEY_REL"
    printf '{"ssh_key_path": "%s"}' "$LEGACY_DIR/$SSH_KEY_REL" > "$HOST_STATE_JSON"

    # The completion shim calls the interpreter by absolute path.
    mkdir -p "$(dirname "$LEGACY_DIR/$COMPLETION_REL")"
    printf '#compdef mngr\n%s/.venv/bin/python3 -m imbue.mngr.cli.complete "$@"\n' "$LEGACY_DIR" \
        > "$LEGACY_DIR/$COMPLETION_REL"

    # Recorded agent output that merely mentions the old path. Must survive byte
    # for byte -- rewriting it would corrupt a transcript.
    mkdir -p "$(dirname "$LEGACY_DIR/$TRANSCRIPT_REL")"
    printf '{"text":"I looked in %s/mngr"}' "$LEGACY_DIR" > "$LEGACY_DIR/$TRANSCRIPT_REL"

    # A workspace's latchkey handle as mngr_latchkey leaves it: a symlink to its
    # host's permissions file by absolute path. The workspace's token names the
    # handle's legacy path, which the gateway must still resolve after the move.
    mkdir -p "$(dirname "$LEGACY_DIR/$LATCHKEY_HOST_FILE_REL")" "$(dirname "$LEGACY_DIR/$LATCHKEY_HANDLE_REL")"
    printf '{"rules": []}' > "$LEGACY_DIR/$LATCHKEY_HOST_FILE_REL"
    ln -s "$LEGACY_DIR/$LATCHKEY_HOST_FILE_REL" "$LEGACY_DIR/$LATCHKEY_HANDLE_REL"

    # One entry per destination root, so a role routed to the wrong place shows up.
    mkdir -p "$LEGACY_DIR/logs" "$LEGACY_DIR/.uv-cache" "$LEGACY_DIR/template-cache"
    printf 'ci-legacy-log\n' > "$LEGACY_DIR/logs/ci-legacy.log"
    printf 'ci-legacy-uv-cache\n' > "$LEGACY_DIR/.uv-cache/ci-legacy-marker"
    printf 'ci-legacy-template-cache\n' > "$LEGACY_DIR/template-cache/ci-legacy-marker"
    printf 'ci-legacy-canary\n' > "$LEGACY_DIR/$CANARY_REL"

    log "seeded $(find "$LEGACY_DIR" -type f | wc -l | tr -d ' ') files"
}

fail() { echo "ERROR: $*" >&2; FAILED=1; }

verify() {
    FAILED=0
    log "verifying the migration moved $LEGACY_DIR onto the canonical roots"

    [[ -f "$STATE_ROOT/auth/sessions/ci-legacy-account.json" ]] \
        || fail "auth session did not reach $STATE_ROOT/auth/sessions/"
    [[ -f "$STATE_ROOT/$CANARY_REL" ]] || fail "canary did not reach the state root"
    [[ -f "$STATE_ROOT/$SSH_KEY_REL" ]] || fail "ssh key did not reach the state root"
    [[ -f "$CACHE_ROOT/.uv-cache/ci-legacy-marker" ]] || fail "uv cache did not reach $CACHE_ROOT"
    [[ -f "$CACHE_ROOT/template-cache/ci-legacy-marker" ]] || fail "template cache did not reach $CACHE_ROOT"
    # logs/ is unwrapped: its children land at the logs root itself.
    [[ -f "$LOGS_ROOT/ci-legacy.log" ]] || fail "log did not reach $LOGS_ROOT (unwrapped)"

    # Secrets must not have followed the caches onto a root the OS may reclaim.
    [[ ! -e "$CACHE_ROOT/auth" ]] || fail "auth/ was filed under the cache root"

    local moved_host_state="$STATE_ROOT/mngr/profiles/$PROFILE_ID/providers/lima/lima/state/host_state/host-ci0legacy.json"
    if [[ -f "$moved_host_state" ]]; then
        grep -q "$STATE_ROOT/$SSH_KEY_REL" "$moved_host_state" \
            || fail "host state does not point at the moved ssh key: $(cat "$moved_host_state")"
        ! grep -q "$LEGACY_DIR" "$moved_host_state" \
            || fail "host state still points into $LEGACY_DIR"
    else
        fail "host state did not reach $moved_host_state"
    fi

    # The migration drops the generated completion shims rather than rewriting
    # their baked interpreter path. mngr rebuilds one on demand, and the app has
    # run by the time this check does, so a shim here is only wrong when it still
    # names the legacy root. That the migration deletes rather than rewrites is
    # pinned in test/unit/migrate-data-dir.test.js, where no app can rebuild it.
    local moved_completion="$STATE_ROOT/$COMPLETION_REL"
    if [[ -e "$moved_completion" ]] && grep -q "$LEGACY_DIR/" "$moved_completion"; then
        fail "completion shim at $moved_completion still names $LEGACY_DIR"
    fi

    local moved_transcript="$STATE_ROOT/$TRANSCRIPT_REL"
    if [[ -f "$moved_transcript" ]]; then
        grep -q "$LEGACY_DIR/mngr" "$moved_transcript" \
            || fail "transcript was rewritten; recorded output must be left as written"
    else
        fail "transcript did not reach $moved_transcript"
    fi

    # -f follows symlinks, as the gateway does when it resolves a token's path.
    [[ -f "$LEGACY_DIR/$LATCHKEY_HANDLE_REL" ]] \
        || fail "a pre-migration workspace token's path $LEGACY_DIR/$LATCHKEY_HANDLE_REL no longer resolves"
    [[ "$(realpath "$LEGACY_DIR/$LATCHKEY_HANDLE_REL")" == "$(realpath "$STATE_ROOT/$LATCHKEY_HOST_FILE_REL")" ]] \
        || fail "$LEGACY_DIR/$LATCHKEY_HANDLE_REL does not resolve to the moved host permissions file"

    [[ -f "$STATE_ROOT/.migrated-from-dotfolder" ]] || fail "no migration marker in $STATE_ROOT"

    if [[ "$FAILED" -ne 0 ]]; then
        echo "--- $LEGACY_DIR ---" >&2
        find "$LEGACY_DIR" 2>&1 | head -40 >&2
        echo "--- $STATE_ROOT ---" >&2
        find "$STATE_ROOT" -maxdepth 3 2>&1 | head -40 >&2
        exit 1
    fi
    log "migration verified: state / cache / logs all populated, recorded paths rewritten, transcript intact, latchkey tokens resolve"
}

case "$MODE" in
    seed) seed ;;
    verify) verify ;;
    *) echo "usage: legacy_minds_state_fixture.sh <seed|verify>" >&2; exit 2 ;;
esac
