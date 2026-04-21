#!/usr/bin/env bash
# Collect diagnostic info from a user's Mac and bundle it into a single
# zip they can send back. Run with:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/apps/minds/scripts/collect-diagnostics.sh)
#
# or, locally:
#
#   bash apps/minds/scripts/collect-diagnostics.sh
#
# Produces ~/minds-diagnostics-<timestamp>.zip. Nothing is uploaded
# automatically -- the user decides what to share.

set -u

OUT=$(mktemp -d -t minds-diag)
TS=$(date +%Y%m%d-%H%M%S)
ZIP_OUT="${HOME}/minds-diagnostics-${TS}.zip"

say() { printf '[diag] %s\n' "$*"; }
snap() {
  local name="$1"; shift
  say "collect: $name"
  "$@" > "$OUT/$name" 2>&1 || true
}

# --- Versions and environment ---
snap env.txt env
snap uname.txt uname -a
snap sw_vers.txt sw_vers
snap shell.txt sh -c 'echo "SHELL=$SHELL"; echo "PATH=$PATH"; which git; which limactl; which brew; which node'

# --- Installed app state ---
if [ -d /Applications/minds.app ]; then
  snap minds-app-ls.txt ls -la /Applications/minds.app/Contents
  snap minds-app-plist.txt /usr/libexec/PlistBuddy -c Print /Applications/minds.app/Contents/Info.plist
  snap minds-app-codesign.txt codesign -dv --verbose=4 /Applications/minds.app
  snap minds-app-spctl.txt spctl -a -vv /Applications/minds.app
  snap minds-app-xattr.txt xattr /Applications/minds.app
  snap minds-wheel-head.txt sh -c 'unzip -p /Applications/minds.app/Contents/Resources/wheels/minds-*.whl imbue/minds/config/data_types.py | head -40'
else
  snap minds-app-ls.txt echo "/Applications/minds.app not present"
fi

# --- Running processes related to minds ---
snap processes.txt sh -c 'ps -eo pid,ppid,user,rss,command | grep -iE "minds|electron|limactl" | grep -v grep'

# --- Lima VM state ---
if command -v limactl >/dev/null 2>&1; then
  snap limactl-list.txt limactl list
  snap limactl-version.txt limactl --version
else
  snap limactl-list.txt echo "limactl not installed on system PATH"
fi
if [ -d "${HOME}/.minds/lima" ]; then
  snap bundled-lima-version.txt cat "${HOME}/.minds/lima/VERSION"
  snap bundled-lima-ls.txt ls -la "${HOME}/.minds/lima/bin"
fi
snap lima-dirs.txt sh -c "ls -la ${HOME}/.lima/ 2>&1 | head -30"
snap lima-disk-usage.txt sh -c "du -sh ${HOME}/.lima/* 2>/dev/null | sort -h | head -30"

# --- mngr state ---
if [ -d "${HOME}/.minds/mngr" ]; then
  snap mngr-dirs.txt sh -c "ls -la ${HOME}/.minds/mngr/profiles/*/providers/lima/lima/state/host_state/ 2>/dev/null"
  snap mngr-list.txt sh -c 'MNGR_HOST_DIR="${HOME}/.minds/mngr" MNGR_PREFIX=minds- /Applications/minds.app/Contents/Resources/pyproject/.venv/bin/mngr list --format json'
fi

# --- Recent minds logs (capped to ~2 MB each) ---
mkdir -p "$OUT/logs"
if [ -d "${HOME}/.minds/logs" ]; then
  for f in "${HOME}/.minds/logs"/minds.log "${HOME}/.minds/logs"/minds-events.jsonl; do
    [ -f "$f" ] || continue
    tail -c 2097152 "$f" > "$OUT/logs/$(basename "$f")"
  done
fi

# --- System log last hour, minds-relevant processes only ---
snap syslog-minds.txt /usr/bin/log show --predicate 'process == "minds" OR sender == "amfid"' --last 1h --info

# --- macOS crash reports ---
mkdir -p "$OUT/crash-reports"
ls -lt "${HOME}/Library/Logs/DiagnosticReports/" 2>/dev/null | head -30 > "$OUT/crash-reports/index.txt" || true
# Copy minds-related crash reports only (usually small)
find "${HOME}/Library/Logs/DiagnosticReports/" -maxdepth 1 \( -iname '*minds*' -o -iname '*electron*' \) 2>/dev/null | while IFS= read -r f; do
  cp "$f" "$OUT/crash-reports/" 2>/dev/null || true
done

# --- forever-claude-template settings check (common cause of lima create failures) ---
snap fct-remote-lima-template.txt sh -c 'curl -sL --max-time 10 "https://api.github.com/repos/imbue-ai/forever-claude-template/contents/.mngr/settings.toml" | head -20'

# --- Wrap it up ---
(cd "$OUT" && zip -qr "$ZIP_OUT" .)
rm -rf "$OUT"

cat <<EOM

Done. Diagnostics archive created at:
  $ZIP_OUT

Size: $(du -h "$ZIP_OUT" | cut -f1)

Please share that zip with the minds team. It contains:
  * versions + environment (no secrets)
  * installed app signing + plist
  * running process list
  * lima VM list + disk usage
  * mngr state snapshot
  * last 2 MB of minds logs
  * last hour of system log filtered to minds-relevant events
  * minds / Electron crash reports (if any)

Before sending, you can unzip the archive and review/redact anything
sensitive -- no API keys or credentials are intentionally collected, but
the \`env\` output and \`minds-events.jsonl\` tail could include URLs
or paths from your work.
EOM
