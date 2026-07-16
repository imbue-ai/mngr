"""Tests for the bundling contract that ``apps/minds/scripts/build.js`` relies on.

The build script calls ``uv build --package <name> --wheel`` for each workspace
package it ships. Two properties are load-bearing:

- Every bundled wheel must be pure Python (``py3-none-any``), because the
  build is single-platform and the wheel is shipped to all platforms as-is.
- Every bundled wheel must exclude test files, because the packaged app has
  no use for them and shipping them bloats the bundle / exposes test-only
  imports to the runtime.

``test_workspace_wheel_is_pure_python`` and
``test_workspace_wheel_excludes_test_files`` run ``uv build`` for each
workspace package listed in ``WORKSPACE_PACKAGES`` and assert both
properties. Because they exercise the real build toolchain and build every
wheel, they are marked ``@pytest.mark.acceptance``.

``test_workspace_package_lists_are_consistent`` is a fast, pure file-parsing
drift guard (no toolchain, no network) and stays an unmarked integration
test so it runs on every branch.
"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Final

import pytest

# The full set of workspace packages bundled into the standalone app. This
# same set is hand-maintained in three other places:
#   - apps/minds/scripts/build.js          (WORKSPACE_PACKAGES object)
#   - apps/minds/electron/env-setup.js     (WORKSPACE_PACKAGES array)
#   - apps/minds/electron/pyproject/pyproject.toml ([project.dependencies]
#                                           and [tool.uv.sources])
# test_workspace_package_lists_are_consistent below is the drift guard that
# keeps all four in sync -- two production regressions (0.2.13, 0.2.25) came
# from exactly this list drifting, so the guard is load-bearing.
WORKSPACE_PACKAGES = [
    "minds",
    "imbue-mngr",
    "imbue-mngr-aws",
    "imbue-mngr-claude",
    "imbue-mngr-forward",
    "imbue-mngr-imbue-cloud",
    "imbue-mngr-latchkey",
    "imbue-mngr-lima",
    "imbue-mngr-modal",
    "imbue-mngr-ovh",
    "imbue-mngr-vps",
    "imbue-common",
    "concurrency-group",
    "resource-guards",
    "modal-proxy",
    "overlay",
]

APP_ROOT = Path(__file__).resolve().parents[1]
MONOREPO_ROOT = Path(__file__).resolve().parents[3]
TEST_PATTERN = re.compile(r"(^|/)(test_[^/]*\.py|[^/]+_test\.py|conftest\.py)$")


@pytest.fixture(scope="module")
def built_wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build every workspace package wheel once, share across tests in the module."""
    out_dir = tmp_path_factory.mktemp("wheels")
    wheels: dict[str, Path] = {}
    for name in WORKSPACE_PACKAGES:
        before = set(out_dir.iterdir())
        subprocess.run(
            ["uv", "build", "--package", name, "--wheel", "--out-dir", str(out_dir)],
            cwd=MONOREPO_ROOT,
            check=True,
            capture_output=True,
        )
        produced = [p for p in out_dir.iterdir() if p not in before and p.suffix == ".whl"]
        assert len(produced) == 1, f"Expected exactly one wheel for {name}, got {produced}"
        wheels[name] = produced[0]
    return wheels


@pytest.mark.acceptance
@pytest.mark.parametrize("package_name", WORKSPACE_PACKAGES)
def test_workspace_wheel_is_pure_python(built_wheels: dict[str, Path], package_name: str) -> None:
    """Every workspace wheel must be tagged py3-none-any.

    If this ever fails, a workspace package has picked up a C extension or a
    Python-version-specific dependency. The wheel-bundling strategy assumes
    pure Python so one build ships to all platforms. Adding native code
    requires per-platform wheel builds (see build.js).
    """
    whl = built_wheels[package_name]
    # Wheel filename convention: {distribution}-{version}-{python tag}-{abi tag}-{platform tag}.whl
    parts = whl.stem.split("-")
    assert parts[-3:] == ["py3", "none", "any"], (
        f"{whl.name} is not pure Python. Expected tags py3-none-any, got {parts[-3:]}. "
        "Workspace packages must be pure Python for the current bundling strategy."
    )


@pytest.mark.acceptance
@pytest.mark.parametrize("package_name", WORKSPACE_PACKAGES)
def test_workspace_wheel_excludes_test_files(built_wheels: dict[str, Path], package_name: str) -> None:
    """Wheels must not contain test files.

    If this fails, the package's ``pyproject.toml`` is missing or has wrong
    `exclude` rules under `[tool.hatch.build.targets.wheel]`. See e.g.
    ``libs/imbue_common/pyproject.toml`` for the expected pattern.
    """
    with zipfile.ZipFile(built_wheels[package_name]) as zf:
        leaks = [n for n in zf.namelist() if TEST_PATTERN.search(n)]
    assert leaks == [], (
        f"{built_wheels[package_name].name} contains test files that should be excluded: {leaks}. "
        'Add `exclude = ["*_test.py", "test_*.py", "**/conftest.py"]` to '
        "[tool.hatch.build.targets.wheel] in the package's pyproject.toml."
    )


def _parse_build_js_packages() -> set[str]:
    """Extract the WORKSPACE_PACKAGES object keys from scripts/build.js.

    The object literal looks like ``const WORKSPACE_PACKAGES = { 'name': 'path', ... };``
    -- we slice out that block and pull every quoted key.
    """
    text = (APP_ROOT / "scripts" / "build.js").read_text()
    match = re.search(r"const WORKSPACE_PACKAGES\s*=\s*\{(.*?)\};", text, re.DOTALL)
    assert match is not None, "Could not locate WORKSPACE_PACKAGES object in build.js"
    return set(re.findall(r"""['"]([^'"]+)['"]\s*:""", match.group(1)))


def _parse_env_setup_js_packages() -> set[str]:
    """Extract the WORKSPACE_PACKAGES array entries from electron/env-setup.js.

    The array literal looks like ``const WORKSPACE_PACKAGES = [ 'name', ... ];``.
    """
    text = (APP_ROOT / "electron" / "env-setup.js").read_text()
    match = re.search(r"const WORKSPACE_PACKAGES\s*=\s*\[(.*?)\];", text, re.DOTALL)
    assert match is not None, "Could not locate WORKSPACE_PACKAGES array in env-setup.js"
    return set(re.findall(r"""['"]([^'"]+)['"]""", match.group(1)))


def _parse_pyproject_packages() -> tuple[set[str], set[str]]:
    """Extract package names from electron/pyproject/pyproject.toml.

    Returns ``(dependency_names, source_names)`` -- the names listed under
    ``[project] dependencies`` (with version specifiers stripped) and the
    keys under ``[tool.uv.sources]``. Both must mirror WORKSPACE_PACKAGES.
    """
    text = (APP_ROOT / "electron" / "pyproject" / "pyproject.toml").read_text()

    deps_match = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL | re.MULTILINE)
    assert deps_match is not None, "Could not locate [project] dependencies in pyproject.toml"
    # Strip PEP 508 version specifiers (>=, ==, etc.) to get the bare name.
    dependency_names = {re.split(r"[><=!~ ]", entry)[0] for entry in re.findall(r'"([^"]+)"', deps_match.group(1))}

    sources_match = re.search(r"^\[tool\.uv\.sources\]\n(.*?)(?=^\[|\Z)", text, re.DOTALL | re.MULTILINE)
    assert sources_match is not None, "Could not locate [tool.uv.sources] in pyproject.toml"
    source_names = set(re.findall(r"^([A-Za-z0-9_.-]+)\s*=", sources_match.group(1), re.MULTILINE))

    return dependency_names, source_names


def test_workspace_package_lists_are_consistent() -> None:
    """Bidirectional drift guard across every place the bundled-package list lives.

    The set of workspace packages shipped in the standalone app is hand-maintained
    in four files: this test, scripts/build.js, electron/env-setup.js, and
    electron/pyproject/pyproject.toml (both [project] dependencies and
    [tool.uv.sources]). They MUST all agree -- two production regressions
    (0.2.13, 0.2.25) were caused by this list drifting. Any package added to or
    removed from one file but not the others fails here, naming the offender.
    """
    expected = set(WORKSPACE_PACKAGES)
    dependency_names, source_names = _parse_pyproject_packages()
    actual_by_source = {
        "scripts/build.js": _parse_build_js_packages(),
        "electron/env-setup.js": _parse_env_setup_js_packages(),
        "electron/pyproject/pyproject.toml [project.dependencies]": dependency_names,
        "electron/pyproject/pyproject.toml [tool.uv.sources]": source_names,
    }
    mismatches = {
        source: sorted(found.symmetric_difference(expected))
        for source, found in actual_by_source.items()
        if found != expected
    }
    assert not mismatches, (
        "Bundled workspace-package lists have drifted out of sync. "
        f"build_test.py WORKSPACE_PACKAGES = {sorted(expected)}. "
        f"Differences (symmetric difference vs the test's list) by file: {mismatches}. "
        "Update every file so the package sets match exactly."
    )


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


# Drift guards for scripts/git-manifest.json, the single source of truth for
# the pinned dugite-native git payload (design: specs/minds-managed-git/concise.md).
# Pure file-parsing tests: no network, no toolchain.

# The manifest targets and the per-target asset "label" segment
# (dugite-native-v<gitVersion>-<shortsha>-<label>.tar.gz) that each target's
# asset filename must carry. Mirrors the naming scheme dugite-native publishes.
_GIT_MANIFEST_ASSET_LABEL_BY_TARGET: Final[dict[str, str]] = {
    "darwin-arm64": "macOS-arm64",
    "darwin-x64": "macOS-x64",
    "linux-x64": "ubuntu-x64",
    "linux-arm64": "ubuntu-arm64",
    "win32-x64": "windows-x64",
}

# The subset of manifest targets that release verification and CI acceptance
# tests gate on. Non-shipped entries exist so dev machines and future platform
# bring-up get the managed download path for free (spec "Scope decisions").
_GIT_MANIFEST_SHIPPED_TARGETS: Final[frozenset[str]] = frozenset({"darwin-arm64", "linux-x64"})

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _load_git_manifest() -> dict:
    """Load scripts/git-manifest.json (must be valid JSON)."""
    text = (APP_ROOT / "scripts" / "git-manifest.json").read_text()
    return json.loads(text)


def test_git_manifest_version_fields_are_consistent() -> None:
    """The manifest's version fields must be well-formed and mutually consistent.

    ``dugiteNativeTag`` is dugite-native's release tag (``v<git>-<build>``) and
    ``gitVersion`` is the bundled git version. The runtime and docs promise
    ``git --version`` reports exactly ``gitVersion`` on every platform, and the
    download URL is derived from ``dugiteNativeTag``; a tag that does not embed
    ``gitVersion`` would ship a git whose version silently disagrees with what
    the manifest claims. See specs/minds-managed-git/concise.md.
    """
    manifest = _load_git_manifest()

    dugite_native_tag = manifest["dugiteNativeTag"]
    git_version = manifest["gitVersion"]
    assert re.match(r"^v\d+\.\d+\.\d+-\d+$", dugite_native_tag), (
        f"git-manifest.json dugiteNativeTag {dugite_native_tag!r} must match "
        "the dugite-native release tag shape v<major>.<minor>.<patch>-<build>."
    )
    assert re.match(r"^\d+\.\d+\.\d+$", git_version), (
        f"git-manifest.json gitVersion {git_version!r} must be a bare semantic git version <major>.<minor>.<patch>."
    )
    assert dugite_native_tag.startswith(f"v{git_version}"), (
        f"git-manifest.json dugiteNativeTag {dugite_native_tag!r} must start with "
        f"'v{git_version}' so the pinned tag and the reported gitVersion agree."
    )


def test_git_manifest_target_set_is_exact() -> None:
    """The manifest must carry exactly the five expected target keys.

    download-binaries.js and the acceptance test key off these platform-arch
    strings. An unexpected extra key, or a missing one, means a target was added
    or renamed without updating the consumers. See specs/minds-managed-git/concise.md.
    """
    manifest = _load_git_manifest()
    assert set(manifest["targets"]) == set(_GIT_MANIFEST_ASSET_LABEL_BY_TARGET), (
        "git-manifest.json targets must be exactly "
        f"{sorted(_GIT_MANIFEST_ASSET_LABEL_BY_TARGET)}; got {sorted(manifest['targets'])}."
    )


def test_git_manifest_entries_are_well_formed() -> None:
    """Every manifest target entry must be well-formed and consistently named.

    Each entry needs a 64-hex ``sha256`` (SHA256-verified download is the whole
    provenance story -- no artifact mirroring), a boolean ``shipped`` flag, and
    an ``asset`` following dugite-native's naming scheme
    ``dugite-native-v<gitVersion>-<shortsha>-<label>.tar.gz`` where the
    ``<shortsha>`` (a dugite-native commit) is identical across all five assets
    of a single release and ``<label>`` is the expected per-target platform
    label. Asset names embed the short-SHA verbatim, so they cannot be templated
    from the version alone. See specs/minds-managed-git/concise.md.
    """
    manifest = _load_git_manifest()
    git_version = manifest["gitVersion"]
    asset_pattern = re.compile(rf"^dugite-native-v{re.escape(git_version)}-([0-9a-f]+)-([A-Za-z0-9-]+)\.tar\.gz$")

    short_shas: set[str] = set()
    for target_key, label in _GIT_MANIFEST_ASSET_LABEL_BY_TARGET.items():
        entry = manifest["targets"][target_key]

        sha256 = entry["sha256"]
        assert _SHA256_HEX.match(sha256), (
            f"git-manifest.json target {target_key} sha256 {sha256!r} must be 64 lowercase hex chars."
        )
        assert isinstance(entry["shipped"], bool), (
            f"git-manifest.json target {target_key} 'shipped' must be a JSON boolean, got {entry['shipped']!r}."
        )

        asset = entry["asset"]
        asset_match = asset_pattern.match(asset)
        assert asset_match is not None, (
            f"git-manifest.json target {target_key} asset {asset!r} must match "
            f"dugite-native-v{git_version}-<shortsha>-{label}.tar.gz."
        )
        short_shas.add(asset_match.group(1))
        assert asset_match.group(2) == label, (
            f"git-manifest.json target {target_key} asset {asset!r} must carry the "
            f"platform label {label!r}, got {asset_match.group(2)!r}."
        )

    assert len(short_shas) == 1, (
        "git-manifest.json asset names must all share one dugite-native commit "
        f"short-SHA (a single release), but found {sorted(short_shas)}."
    )


def test_git_manifest_sha256_values_are_distinct() -> None:
    """All five pinned SHA256 hashes must be distinct.

    The five assets are distinct per-platform tarballs, so identical hashes would
    mean a copy-paste error left one target pinned to another's bytes -- pinning
    defends against future substitution, not against pasting a wrong value in.
    See specs/minds-managed-git/concise.md.
    """
    manifest = _load_git_manifest()
    hashes = [entry["sha256"] for entry in manifest["targets"].values()]
    assert len(set(hashes)) == len(hashes), (
        "git-manifest.json has duplicate sha256 values across targets: "
        f"{sorted(hashes)}. Each per-platform asset must pin its own bytes."
    )


def test_git_manifest_shipped_targets_are_exact() -> None:
    """The set of ``shipped: true`` targets must be exactly the shipped platforms.

    ``shipped`` marks the targets that release verification and CI acceptance
    tests gate on (darwin-arm64 and linux-x64). Flipping a flag here without the
    corresponding CI/release work -- or forgetting to flip one when a platform is
    brought up -- is exactly the drift this guards. See specs/minds-managed-git/concise.md.
    """
    manifest = _load_git_manifest()
    shipped = {key for key, entry in manifest["targets"].items() if entry["shipped"]}
    assert shipped == set(_GIT_MANIFEST_SHIPPED_TARGETS), (
        f"git-manifest.json shipped:true targets must be exactly {sorted(_GIT_MANIFEST_SHIPPED_TARGETS)}; "
        f"got {sorted(shipped)}."
    )


def _parse_git_manifest_target_by_platform_arch() -> set[str]:
    """Extract the GIT_MANIFEST_TARGET_BY_PLATFORM_ARCH values from download-binaries.js.

    The object literal looks like
    ``const GIT_MANIFEST_TARGET_BY_PLATFORM_ARCH = { 'darwin/aarch64': 'darwin-arm64', ... };``
    -- we slice out that block and pull every quoted map value (the manifest
    target key on the right-hand side of each ``:``).
    """
    text = (APP_ROOT / "scripts" / "download-binaries.js").read_text()
    match = re.search(r"const GIT_MANIFEST_TARGET_BY_PLATFORM_ARCH\s*=\s*\{(.*?)\};", text, re.DOTALL)
    assert match is not None, "Could not locate GIT_MANIFEST_TARGET_BY_PLATFORM_ARCH object in download-binaries.js"
    return set(re.findall(r""":\s*['"]([^'"]+)['"]""", match.group(1)))


def test_download_binaries_git_map_agrees_with_manifest() -> None:
    """Drift guard: download-binaries.js's platform map must line up with the manifest.

    GIT_MANIFEST_TARGET_BY_PLATFORM_ARCH maps each ``(platform, arch)`` the
    downloader supports to a manifest target key. Every value it names must exist
    in the manifest (a typo'd key would hard-fail the build with "No dugite-native
    manifest entry"), and every shipped manifest target must be reachable through
    some mapping (otherwise a platform we claim to ship could never actually be
    downloaded). Windows deliberately stays on the MinGit path, so win32-x64 must
    NOT appear among the map's values. See specs/minds-managed-git/concise.md.
    """
    manifest = _load_git_manifest()
    manifest_targets = set(manifest["targets"])
    shipped = {key for key, entry in manifest["targets"].items() if entry["shipped"]}
    mapped_targets = _parse_git_manifest_target_by_platform_arch()

    unknown = mapped_targets - manifest_targets
    assert not unknown, (
        "download-binaries.js GIT_MANIFEST_TARGET_BY_PLATFORM_ARCH maps to target "
        f"keys absent from git-manifest.json: {sorted(unknown)}."
    )
    unreachable = shipped - mapped_targets
    assert not unreachable, (
        "download-binaries.js GIT_MANIFEST_TARGET_BY_PLATFORM_ARCH does not cover "
        f"these shipped manifest targets: {sorted(unreachable)}. Shipped targets "
        "must be reachable by the downloader."
    )
    assert "win32-x64" not in mapped_targets, (
        "download-binaries.js must NOT route win32-x64 through the dugite-native "
        "map -- Windows deliberately stays on the MinGit path (spec 'Windows "
        "bring-up' out of scope)."
    )


def test_ensure_binaries_guards_stale_git_payload() -> None:
    """Guard: ensure-binaries.js must keep its dugite-native staleness check.

    A dev machine carrying an old (pre-manifest or wrong-tag) git payload passes
    the plain existence check, so ensure-binaries.js reads the pinned tag from
    git-manifest.json and treats a missing/mismatched ``.dugite-tag`` marker as a
    missing binary, forcing a re-download. This cheap textual check trips if
    someone deletes that staleness logic. See specs/minds-managed-git/concise.md.
    """
    text = (APP_ROOT / "scripts" / "ensure-binaries.js").read_text()
    assert ".dugite-tag" in text, (
        "ensure-binaries.js must reference the '.dugite-tag' marker so a stale "
        "bundled git payload is re-downloaded (spec: minds-managed-git)."
    )
    assert "git-manifest.json" in text, (
        "ensure-binaries.js must read 'git-manifest.json' to compare the on-disk "
        ".dugite-tag against the pinned dugiteNativeTag (spec: minds-managed-git)."
    )


_DOWNLOAD_BINARIES_PATH: Final[Path] = APP_ROOT / "scripts" / "download-binaries.js"


def _run_download_binaries_function(expression: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one exported download-binaries.js function via ``node -e``.

    ``expression`` sees the module as ``db`` and the given arguments as
    ``process.argv[2..]`` (argv[1] is the module path).
    """
    assert _NODE_BINARY is not None
    return subprocess.run(
        [
            _NODE_BINARY,
            "-e",
            f"const db = require(process.argv[1]); {expression}",
            str(_DOWNLOAD_BINARIES_PATH),
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _make_fake_git_payload(payload_root: Path) -> None:
    """Lay out a miniature dugite-native-shaped payload with the real link shapes.

    ``libexec/git-core/git`` is an executable script that echoes its argv, so a
    converted shim can be executed and its dispatch observed directly.
    """
    git_core = payload_root / "libexec" / "git-core"
    git_core.mkdir(parents=True)
    (payload_root / "bin").mkdir()
    fake_git = git_core / "git"
    fake_git.write_text('#!/bin/sh\necho "git-multicall $@"\n')
    fake_git.chmod(0o755)
    fake_remote_http = git_core / "git-remote-http"
    fake_remote_http.write_text('#!/bin/sh\necho "remote-http $@"\n')
    fake_remote_http.chmod(0o755)
    (git_core / "git-fetch").symlink_to("git")
    (git_core / "git-status").symlink_to("git")
    (git_core / "git-remote-https").symlink_to("git-remote-http")


def test_convert_git_payload_symlinks_produces_working_symlink_free_shims(tmp_path: Path) -> None:
    """The shim conversion must leave zero symlinks and preserve dispatch behavior.

    ToDesktop's app-source zip follows symlinks, so the 142 dugite-native
    ``libexec/git-core`` links to the multicall git binary would be zipped as
    ~480MB of copies (the 2026-07 launch-to-msg 701MB upload failure). The
    conversion replaces each link with an sh shim; this test runs the real
    exported function on a miniature payload and then executes the shims:
    a dashed builtin must dispatch as ``git <subcommand>`` and a remote
    helper must exec its sibling helper unchanged.
    """
    payload_root = tmp_path / "git"
    _make_fake_git_payload(payload_root)

    result = _run_download_binaries_function(
        "console.log(db.convertGitPayloadSymlinksToShims(process.argv[2]));", str(payload_root)
    )
    assert result.returncode == 0, f"conversion failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.stdout.strip() == "3", f"expected 3 converted symlinks, got {result.stdout.strip()!r}"
    assert [entry for entry in payload_root.rglob("*") if entry.is_symlink()] == []

    git_core = payload_root / "libexec" / "git-core"
    for shim_name in ("git-fetch", "git-status", "git-remote-https"):
        shim_path = git_core / shim_name
        assert shim_path.read_text().startswith("#!/bin/sh"), f"{shim_name} is not an sh shim"
        assert os.access(shim_path, os.X_OK), f"{shim_name} lost its executable bit"

    fetch_output = subprocess.run(
        [str(git_core / "git-fetch"), "origin", "main"], capture_output=True, text=True, timeout=30
    )
    assert fetch_output.returncode == 0 and fetch_output.stdout.strip() == "git-multicall fetch origin main", (
        f"dashed builtin shim dispatched wrong: stdout={fetch_output.stdout!r} stderr={fetch_output.stderr!r}"
    )
    helper_output = subprocess.run(
        [str(git_core / "git-remote-https"), "origin", "https://example.invalid/repo.git"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (
        helper_output.returncode == 0
        and helper_output.stdout.strip() == "remote-http origin https://example.invalid/repo.git"
    ), f"remote helper shim dispatched wrong: stdout={helper_output.stdout!r} stderr={helper_output.stderr!r}"


def test_convert_git_payload_symlinks_rejects_links_escaping_the_payload(tmp_path: Path) -> None:
    """A payload symlink pointing outside the payload must abort the conversion.

    Such a link means the upstream payload layout changed (or the archive is
    malicious); silently shimming it would bake an absolute path to a
    build-machine file into the shipped app.
    """
    payload_root = tmp_path / "git"
    _make_fake_git_payload(payload_root)
    outside_file = tmp_path / "outside-the-payload"
    outside_file.write_text("not payload content\n")
    (payload_root / "libexec" / "git-core" / "git-escape").symlink_to(outside_file)

    result = _run_download_binaries_function(
        "db.convertGitPayloadSymlinksToShims(process.argv[2]);", str(payload_root)
    )
    assert result.returncode != 0, "conversion must fail on a payload-escaping symlink"
    assert "escapes the payload" in result.stderr


def test_measure_tree_as_archived_prices_symlinks_at_target_size(tmp_path: Path) -> None:
    """measureTreeAsArchived must model a symlink-following archiver.

    A symlink contributes its target's full size to the archived total (that
    is exactly what ToDesktop's zip does), while the real total only carries
    the link entry itself -- the difference is the inflation the build guard
    alarms on.
    """
    tree_root = tmp_path / "tree"
    tree_root.mkdir()
    payload_file = tree_root / "payload.bin"
    payload_file.write_bytes(b"x" * 10_000)
    (tree_root / "link-one").symlink_to("payload.bin")
    (tree_root / "link-two").symlink_to("payload.bin")

    result = _run_download_binaries_function(
        "console.log(JSON.stringify(db.measureTreeAsArchived(process.argv[2])));", str(tree_root)
    )
    assert result.returncode == 0, f"measureTreeAsArchived failed:\nstderr:\n{result.stderr}"
    measurement = json.loads(result.stdout)
    assert measurement["symlinkCount"] == 2
    assert measurement["archivedBytes"] == 30_000, "each symlink must count at its target's 10KB"
    assert measurement["realBytes"] < 11_000, "the real total must not include materialized symlink copies"


def test_assert_tree_fits_upload_budget_fails_on_symlink_inflation(tmp_path: Path) -> None:
    """The payload guard must fail a tree whose symlinks would materialize past the threshold.

    ToDesktop prices symlinks harmlessly, but downstream symlink-dereferencing
    copiers (electron-builder's extraResources copy into the final app) would
    materialize a full copy per link. Uses a sparse 100MB target (instant to
    create) with two symlinks: 200MB of materialization against a 100MB real
    payload must fail even far below the upload limit; a symlink-free tree of
    the same size must pass.
    """
    tree_root = tmp_path / "tree"
    tree_root.mkdir()
    sparse_target = tree_root / "big.bin"
    sparse_target.touch()
    os.truncate(sparse_target, 100 * 1024 * 1024)
    (tree_root / "copy-one").symlink_to("big.bin")
    (tree_root / "copy-two").symlink_to("big.bin")

    guard_expression = (
        "db.assertTreeFitsUploadBudget(process.argv[2], { uploadSizeLimitMb: 600, label: 'resources/' });"
    )
    inflated = _run_download_binaries_function(guard_expression, str(tree_root))
    assert inflated.returncode != 0, "the guard must fail on 200MB of symlink materialization"
    assert "materialize" in inflated.stderr and "shims" in inflated.stderr

    (tree_root / "copy-one").unlink()
    (tree_root / "copy-two").unlink()
    symlink_free = _run_download_binaries_function(guard_expression, str(tree_root))
    assert symlink_free.returncode == 0, f"symlink-free tree must pass the guard:\nstderr:\n{symlink_free.stderr}"


def test_build_pipeline_keeps_payload_shim_and_budget_guards() -> None:
    """Drift guard: the shim conversion and upload-budget guard must stay wired in.

    ``downloadGit`` must convert payload symlinks BEFORE writing the
    ``.dugite-tag`` marker (a payload that failed conversion must never be
    tagged complete, or ensure-binaries.js would skip re-downloading it), and
    ``build.js`` must run the upload-budget assertion so a symlink regression
    fails the local build instead of the ToDesktop upload.
    """
    download_binaries_text = _DOWNLOAD_BINARIES_PATH.read_text()
    conversion_call_index = download_binaries_text.find("convertGitPayloadSymlinksToShims(gitDir)")
    tag_write_index = download_binaries_text.find("'.dugite-tag'")
    assert conversion_call_index != -1, "downloadGit must call convertGitPayloadSymlinksToShims on the payload"
    assert tag_write_index != -1, "downloadGit must write the .dugite-tag marker"
    assert conversion_call_index < tag_write_index, (
        "downloadGit must convert payload symlinks BEFORE writing .dugite-tag, so a "
        "failed conversion is never tagged as a complete payload"
    )
    build_text = (APP_ROOT / "scripts" / "build.js").read_text()
    assert "assertUploadFitsToDesktopLimit(ROOT" in build_text, (
        "build.js must estimate the WHOLE ToDesktop app-source upload (appFiles + "
        "extraResources) against uploadSizeLimit -- a resources/-only check passed at "
        "371MB while the real upload was 701MB (2026-07 launch-to-msg failures)"
    )
    assert "assertTreeFitsUploadBudget(RESOURCES_DIR" in build_text, (
        "build.js must keep the payload symlink-inflation guard so downstream "
        "symlink-dereferencing copiers cannot balloon the final app"
    )


def test_estimate_todesktop_upload_mirrors_cli_composition(tmp_path: Path) -> None:
    """The upload estimator must reproduce @todesktop/cli's selection rules.

    App files drop node_modules/.git at any depth, .gitignore files, symlinks,
    and appFiles-excluded prefixes; extraResources are priced whole (lstat) on
    top, even when the same directory is excluded from app files. These are
    exactly the semantics that made resources/ upload twice (701MB, 2026-07),
    so the estimator asserting them is what keeps the build-time guard honest.
    """
    app_root = tmp_path / "app"
    (app_root / "electron").mkdir(parents=True)
    (app_root / "electron" / "main.js").write_bytes(b"m" * 1_000)
    (app_root / "node_modules" / "dep").mkdir(parents=True)
    (app_root / "node_modules" / "dep" / "big.js").write_bytes(b"n" * 50_000)
    (app_root / "nested").mkdir()
    (app_root / "nested" / "node_modules").mkdir()
    (app_root / "nested" / "node_modules" / "inner.js").write_bytes(b"n" * 40_000)
    (app_root / ".gitignore").write_bytes(b"resources/\n")
    (app_root / "electron" / "main-link.js").symlink_to("main.js")
    resources_dir = app_root / "resources"
    (resources_dir / "latchkey" / "node_modules").mkdir(parents=True)
    (resources_dir / "latchkey" / "node_modules" / "dep.js").write_bytes(b"r" * 30_000)
    (resources_dir / "payload.bin").write_bytes(b"r" * 20_000)
    icon = app_root / "icon.png"
    icon.write_bytes(b"i" * 500)

    config = {
        "appFiles": ["**", "!resources/**"],
        "extraResources": [{"from": "resources/", "to": "."}],
        "icon": "./icon.png",
        "uploadSizeLimit": 600,
    }
    result = _run_download_binaries_function(
        "console.log(JSON.stringify(db.estimateToDesktopUploadBytes(process.argv[2], JSON.parse("
        + json.dumps(json.dumps(config))
        + "))));",
        str(app_root),
    )
    assert result.returncode == 0, f"estimator failed:\nstderr:\n{result.stderr}"
    estimate = json.loads(result.stdout)
    # App files: electron/main.js (1000) plus icon.png (500) -- the icon
    # matches '**' AND uploads again to icons/, mirroring the real CLI.
    # node_modules at both depths, the symlink, the .gitignore file, and
    # resources/** are all out.
    assert estimate["appFilesBytes"] == 1_500
    # Extra: the whole resources tree INCLUDING its nested node_modules
    # (30000 + 20000) plus the 500-byte icon again.
    assert estimate["extraBytes"] == 50_500
    assert estimate["totalBytes"] == 52_000


def test_estimate_todesktop_upload_rejects_unsupported_globs(tmp_path: Path) -> None:
    """Unsupported appFiles shapes must throw rather than silently mis-estimate.

    The estimator only models the glob shapes this repo uses ('**' and
    '!<dir>/**'); anything fancier must fail loudly so the guard is extended
    together with todesktop.js instead of drifting from the real zip.
    """
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "main.js").write_bytes(b"m")
    config = {"appFiles": ["dist/**"], "uploadSizeLimit": 600}
    result = _run_download_binaries_function(
        "db.estimateToDesktopUploadBytes(process.argv[2], JSON.parse(" + json.dumps(json.dumps(config)) + "));",
        str(app_root),
    )
    assert result.returncode != 0
    assert "only understands" in result.stderr


def test_assert_upload_fits_todesktop_limit_enforces_the_limit(tmp_path: Path) -> None:
    """The whole-upload guard must fail past uploadSizeLimit and pass under it.

    Uses a sparse file so a >limit tree is instant to create. The passing case
    re-runs with a limit above the tree size.
    """
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "main.js").write_bytes(b"m" * 1_000)
    sparse = app_root / "huge.bin"
    sparse.touch()
    os.truncate(sparse, 700 * 1_000_000)

    def run_with_limit(limit_mb: int) -> subprocess.CompletedProcess[str]:
        config = {"uploadSizeLimit": limit_mb}
        return _run_download_binaries_function(
            "db.assertUploadFitsToDesktopLimit(process.argv[2], JSON.parse(" + json.dumps(json.dumps(config)) + "));",
            str(app_root),
        )

    over = run_with_limit(600)
    assert over.returncode != 0, "700MB of app files must fail a 600MB limit"
    assert "uploadSizeLimit" in over.stderr
    under = run_with_limit(800)
    assert under.returncode == 0, f"700MB of app files must pass an 800MB limit:\nstderr:\n{under.stderr}"


def test_todesktop_config_balances_app_files_exclusions_and_sign_paths() -> None:
    """Drift guard: the appFiles exclusions must keep both failure modes impossible.

    Two constraints pull in opposite directions and both broke a cloud build
    in 2026-07: (1) the heavy resources subtrees must be EXCLUDED from the
    app-files upload or the tree uploads twice (extraResources uploads it
    whole regardless; 701MB against the 600MB limit); (2) every
    ``mac.additionalBinariesToSign`` path must remain INCLUDED, because the
    builder's signing preflight fails with "The following
    additionalBinariesToSign are missing" when its path is excluded, and
    nothing recreates lima cloud-side.
    """
    config = _load_todesktop_config()
    app_files = config.get("appFiles")
    assert app_files is not None and "**" in app_files, (
        "todesktop.js appFiles must include '**' for the app code"
    )
    excluded_prefixes = []
    for glob in app_files:
        if glob.startswith("!"):
            assert glob.endswith("/**"), f"unexpected appFiles exclusion shape: {glob}"
            excluded_prefixes.append(glob[1:].removesuffix("**"))
    for heavy_subtree in ("resources/git/", "resources/latchkey/"):
        assert any(heavy_subtree.startswith(prefix) for prefix in excluded_prefixes), (
            f"todesktop.js appFiles must exclude {heavy_subtree} -- it already uploads whole "
            "via extraResources, and double-uploading it is what hit 701MB in 2026-07"
        )
    for sign_path in config["mac"]["additionalBinariesToSign"]:
        covering = [prefix for prefix in excluded_prefixes if sign_path.startswith(prefix)]
        assert not covering, (
            f"todesktop.js appFiles exclusions {covering} cover the additionalBinariesToSign "
            f"path {sign_path}; the builder's signing preflight requires that file in the "
            "app-files upload and fails the build without it"
        )
    extra_resource_sources = [entry["from"] for entry in config.get("extraResources", [])]
    assert "resources/" in extra_resource_sources, (
        "todesktop.js must keep uploading resources/ via extraResources; the appFiles "
        "exclusions assume that channel delivers the staged binaries to the builder"
    )
