#!/usr/bin/env bash
#
# slack_pr.sh — Slack helpers for the post-pr-to-slack skill.
#
# Every request goes through `latchkey curl`, which injects the Slack
# credential automatically (see the latchkey skill). JSON bodies are built with
# jq so message text containing backticks, quotes, or & is escaped correctly.
#
# Commands:
#   check                                verify latchkey + jq + a working Slack credential
#   channel-id <name>                    print a channel's ID (paginates conversations.list)
#   history <#name|ID> [limit]           print the text of recent messages (default 10)
#   post <#name|ID> <text>               post a message; print its ts
#   reply <#name|ID> <thread_ts> <text>  post a threaded reply; print its ts
#   find-ts <#name|ID> <substring>       print ts of the latest message containing <substring>
#   done <#name|ID> <ts>                 add the :merged: reaction (idempotent)
#
set -euo pipefail

API="https://slack.com/api"

die() { echo "slack_pr.sh: $*" >&2; exit 1; }

# latchkey is usually an nvm-managed node binary. Non-login shells often don't
# source nvm, so latchkey (and its node runtime) may be missing from PATH even
# when installed. Add the install dir back if needed; a no-op when already found.
if ! command -v latchkey >/dev/null 2>&1; then
  for d in "$HOME"/.nvm/versions/node/*/bin "$HOME/.local/bin" /usr/local/bin /opt/homebrew/bin; do
    [ -x "$d/latchkey" ] && { PATH="$d:$PATH"; break; }
  done
fi
command -v latchkey >/dev/null 2>&1 || die "missing dependency: latchkey (npm install -g latchkey; see the latchkey skill)"
command -v jq       >/dev/null 2>&1 || die "missing dependency: jq"

# Fail loudly unless the JSON on stdin has ok:true; pass it through otherwise.
check_ok() {
  local json; json="$(cat)"
  [ -n "$json" ] || die "empty response from Slack"
  if [ "$(printf '%s' "$json" | jq -r '.ok')" != "true" ]; then
    die "Slack API error: $(printf '%s' "$json" | jq -r '.error // "unknown"')"
  fi
  printf '%s' "$json"
}

slack_get()  { latchkey curl -s "$API/$1?${2:-}"; }
slack_post() {
  latchkey curl -s -X POST "$API/$1" \
    -H 'Content-Type: application/json; charset=utf-8' \
    -d "$2"
}

# Confirm the Slack credential actually works (read-only identity check).
cmd_check() {
  local resp; resp="$(latchkey curl -s -X POST "$API/auth.test")"
  [ -n "$resp" ] || die "no response from Slack — is latchkey configured? (latchkey services info slack)"
  if [ "$(printf '%s' "$resp" | jq -r '.ok')" = "true" ]; then
    echo "slack ok: team '$(printf '%s' "$resp" | jq -r '.team')' as '$(printf '%s' "$resp" | jq -r '.user')'"
  else
    die "slack auth failed: $(printf '%s' "$resp" | jq -r '.error // "unknown"') — run: latchkey services info slack"
  fi
}

# Resolve a #name (or bare name) to a channel ID, paginating the full list so
# this tolerates renames and large workspaces (>1000 channels).
channel_id() {
  local want="${1#\#}" cursor="" page id
  [ -n "$want" ] || die "usage: channel-id <name>"
  while :; do
    page="$(slack_get conversations.list \
      "types=public_channel,private_channel&limit=1000&cursor=$cursor" | check_ok)"
    id="$(printf '%s' "$page" | jq -r --arg n "$want" \
      '[.channels[] | select(.name == $n) | .id][0] // ""')"
    [ -n "$id" ] && { printf '%s\n' "$id"; return 0; }
    cursor="$(printf '%s' "$page" | jq -r '.response_metadata.next_cursor // ""')"
    [ -n "$cursor" ] || break
  done
  die "channel '#$want' not found (wrong name, or you are not a member)"
}

# A C…/G… argument is already a channel ID; anything else is a name to resolve.
resolve() { case "$1" in C* | G*) printf '%s\n' "$1" ;; *) channel_id "$1" ;; esac; }

cmd_history() {
  [ $# -ge 1 ] || die "usage: history <#name|ID> [limit]"
  local ch; ch="$(resolve "$1")"
  slack_get conversations.history "channel=$ch&limit=${2:-10}" | check_ok | jq -r '.messages[].text'
}

cmd_post() {
  [ $# -eq 2 ] || die "usage: post <#name|ID> <text>"
  local ch; ch="$(resolve "$1")"
  slack_post chat.postMessage \
    "$(jq -n --arg c "$ch" --arg t "$2" '{channel: $c, text: $t, unfurl_links: true}')" \
    | check_ok | jq -r '.ts'
}

cmd_reply() {
  [ $# -eq 3 ] || die "usage: reply <#name|ID> <thread_ts> <text>"
  local ch; ch="$(resolve "$1")"
  slack_post chat.postMessage \
    "$(jq -n --arg c "$ch" --arg th "$2" --arg t "$3" '{channel: $c, thread_ts: $th, text: $t}')" \
    | check_ok | jq -r '.ts'
}

# Slack auto-links URLs as <url> in stored text, so match on the bare substring.
cmd_find_ts() {
  [ $# -eq 2 ] || die "usage: find-ts <#name|ID> <substring>"
  local ch; ch="$(resolve "$1")"
  slack_get conversations.history "channel=$ch&limit=200" | check_ok \
    | jq -r --arg u "$2" '[.messages[] | select(.text | contains($u)) | .ts][0] // ""'
}

# Idempotent: a pre-existing :merged: (already_reacted) counts as success.
cmd_done() {
  [ $# -eq 2 ] || die "usage: done <#name|ID> <ts>"
  local ch resp ok err
  ch="$(resolve "$1")"
  resp="$(slack_post reactions.add \
    "$(jq -n --arg c "$ch" --arg ts "$2" '{channel: $c, timestamp: $ts, name: "merged"}')")"
  [ -n "$resp" ] || die "empty response from reactions.add"
  ok="$(printf '%s' "$resp" | jq -r '.ok')"
  err="$(printf '%s' "$resp" | jq -r '.error // ""')"
  [ "$ok" = "true" ] || [ "$err" = "already_reacted" ] || die "reactions.add failed: $err"
  echo ok
}

[ $# -ge 1 ] || die "usage: slack_pr.sh <check|channel-id|history|post|reply|find-ts|done> ..."
cmd="$1"; shift
case "$cmd" in
  check)      cmd_check "$@" ;;
  channel-id) channel_id "$@" ;;
  history)    cmd_history "$@" ;;
  post)       cmd_post "$@" ;;
  reply)      cmd_reply "$@" ;;
  find-ts)    cmd_find_ts "$@" ;;
  done)       cmd_done "$@" ;;
  *)          die "unknown command: $cmd" ;;
esac
