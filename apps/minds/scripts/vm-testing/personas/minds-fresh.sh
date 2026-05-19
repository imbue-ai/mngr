#!/usr/bin/env bash
# Persona: minds-fresh
#
# A pristine macOS install -- no developer tools, no Homebrew, no system
# Python beyond what Apple ships, no pre-existing minds state. The cirruslabs
# vanilla base already gives us most of this; we only do tiny tweaks that
# every test run would otherwise repeat.

set -euo pipefail

echo "[minds-fresh] provisioning..."

# Disable the "App downloaded from the internet" first-launch prompt so the
# harness can launch minds.app non-interactively. Re-enable per-test by
# setting APPLY_QUARANTINE=1 when invoking run-test.sh.
sudo defaults write com.apple.LaunchServices LSQuarantine -bool false || true

# Keep Spotlight from chewing CPU during tests on a freshly populated home.
sudo mdutil -i off / 2>/dev/null || true

# Install Xcode Command Line Tools. The bundled `git` inside minds.app is
# /usr/bin/git, which on macOS is an xcselect stub that delegates to the
# Xcode-CLT-shipped real git. Without CLT installed, invoking it sends
# SIGKILL to the caller (-9 exit status; the same path that triggers the
# "No developer tools were found" GUI prompt for interactive shells).
# Trigger the headless install path via the on-demand sentinel that
# softwareupdate recognises.
if ! xcode-select -p >/dev/null 2>&1; then
    echo "[minds-fresh] installing Xcode Command Line Tools..."
    sentinel=/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
    sudo touch "$sentinel"
    # Guarantee the sentinel is cleared on any exit path; otherwise a
    # softwareupdate failure (set -e) would leave the host stuck in
    # installondemand mode.
    trap 'sudo rm -f "$sentinel"' EXIT
    product="$(softwareupdate -l 2>/dev/null \
        | awk -F': ' '/\*.*Command Line Tools/ {print $2; exit}')"
    if [[ -n "$product" ]]; then
        sudo softwareupdate -i "$product" --verbose
    else
        echo "[minds-fresh] WARNING: no Command Line Tools update offered; git inside minds.app may fail"
    fi
    # softwareupdate can exit 0 even when the install didn't actually land
    # (network blips, partial installs). Confirm the developer dir exists so
    # we never snapshot a persona image that still hits the SIGKILL path.
    if ! xcode-select -p >/dev/null 2>&1; then
        echo "[minds-fresh] ERROR: Command Line Tools install did not complete; aborting persona build" >&2
        exit 1
    fi
fi

echo "[minds-fresh] done."
