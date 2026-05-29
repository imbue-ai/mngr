"""Tests for the bundling contract that ``apps/minds/scripts/build.js`` relies on."""

import json
import plistlib
import subprocess
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def _load_todesktop_config() -> dict:
    """Evaluate ``apps/minds/todesktop.js`` and return its exported config."""
    result = subprocess.run(
        ["node", "-e", "console.log(JSON.stringify(require('./todesktop.js')))"],
        cwd=APP_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_bundled_limactl_is_signed_with_virtualization_entitlement() -> None:
    """Guard: the ToDesktop signing config must give bundled limactl the
    virtualization entitlement.

    limactl needs ``com.apple.security.virtualization`` to use Apple's
    Virtualization.framework. ToDesktop deep-signs every nested binary with
    the app's ``mac.entitlements`` plist; if that plist omits the
    entitlement, the re-signed limactl cannot start Lima VMs (VZ exits
    instantly with empty errors) and agent creation fails. limactl must
    also be in ``mac.additionalBinariesToSign`` so ToDesktop signs it
    explicitly with that plist.
    """
    todesktop = _load_todesktop_config()
    mac = todesktop.get("mac", {})

    entitlements_rel = mac.get("entitlements")
    assert entitlements_rel, "todesktop.js must set mac.entitlements"
    entitlements = plistlib.loads((APP_ROOT / entitlements_rel).read_bytes())
    assert entitlements.get("com.apple.security.virtualization") is True, (
        f"{entitlements_rel} must grant com.apple.security.virtualization -- "
        "without it the bundled limactl cannot start Lima VMs."
    )

    additional = mac.get("additionalBinariesToSign", [])
    assert any("limactl" in path for path in additional), (
        "todesktop.js mac.additionalBinariesToSign must include the bundled "
        f"limactl so it is signed with mac.entitlements; got {additional}."
    )
