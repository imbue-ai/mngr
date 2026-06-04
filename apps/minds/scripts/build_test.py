"""Tests for the bundling contract that ``apps/minds/scripts/build.js`` relies on."""

import json
import plistlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]

_NODE_BINARY: Final[str | None] = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE_BINARY is None,
    reason="evaluating apps/minds/todesktop.js requires a node binary on PATH",
)


def _load_todesktop_config() -> dict:
    """Evaluate ``apps/minds/todesktop.js`` and return its exported config."""
    assert _NODE_BINARY is not None
    result = subprocess.run(
        [_NODE_BINARY, "-e", "console.log(JSON.stringify(require('./todesktop.js')))"],
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


def test_bundle_latchkey_uses_pnpm_deploy_against_lockfile() -> None:
    """Guard: bundleLatchkey() must use ``pnpm deploy --prod`` so the shipped
    latchkey tree is pinned by ``pnpm-lock.yaml``, not a fresh registry resolve.

    The previous implementation did ``npm install --no-package-lock`` into a
    scratch dir, which re-resolved every latchkey transitive from version
    ranges at build time. That floated the shipped ``playwright`` /
    ``playwright-core`` independently of dev/CI and already caused user-facing
    breakage (playwright-core 1.60 internals shipped against latchkey code
    expecting the pre-1.60 layout). If a future change reintroduces an
    install path that bypasses the lockfile, this guard fails.
    """
    text = (APP_ROOT / "scripts" / "build.js").read_text()
    match = re.search(r"function bundleLatchkey\(\) \{(.*?)\n\}\n", text, re.DOTALL)
    assert match is not None, "Could not locate bundleLatchkey() in build.js"
    body = match.group(1)

    assert "'pnpm'" in body and "'deploy'" in body and "'--prod'" in body, (
        "bundleLatchkey() must invoke `pnpm deploy --prod` so the shipped "
        "latchkey tree is lockfile-pinned. See "
        "/tmp/minds-build-js-pnpm-deploy-handoff.md for context."
    )
    forbidden = [
        ("npm install", "'install'"),
        ("--no-package-lock flag", "--no-package-lock"),
    ]
    for label, needle in forbidden:
        assert needle not in body, (
            f"bundleLatchkey() contains {label} ({needle!r}). That bypasses "
            "pnpm-lock.yaml and floats the shipped playwright independently "
            "of what dev/CI tested. Use `pnpm deploy --prod` instead."
        )


def test_pnpm_workspace_pins_cross_platform_architectures() -> None:
    """Guard: pnpm-workspace.yaml must list every target platform under
    ``supportedArchitectures`` so cross-platform native prebuilds
    (@napi-rs/keyring-*, playwright fsevents, ...) materialize in the
    ``pnpm deploy`` output regardless of the build host's OS/arch/libc.

    ToDesktop runs ``pnpm build`` once per release on a single host, so the
    bundle must contain prebuilds for every target. Without this block,
    pnpm (like npm) only installs prebuilds matching the build host and the
    shipped resources/latchkey/ would crash on user platforms different
    from the builder's. Every variant is already resolved in
    ``pnpm-lock.yaml``, so this only changes which resolved entries get
    materialized.
    """
    text = (APP_ROOT / "pnpm-workspace.yaml").read_text()
    assert "supportedArchitectures:" in text, (
        "pnpm-workspace.yaml must declare supportedArchitectures so "
        "scripts/build.js's `pnpm deploy` materializes cross-platform "
        "native prebuilds."
    )
    for platform_name in ("darwin", "linux", "win32"):
        assert platform_name in text, (
            f"pnpm-workspace.yaml supportedArchitectures.os must include "
            f"'{platform_name}' (the ToDesktop build targets it)."
        )
    for cpu in ("x64", "arm64"):
        assert cpu in text, f"pnpm-workspace.yaml supportedArchitectures.cpu must include '{cpu}'."
