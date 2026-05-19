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

echo "[minds-fresh] done."
