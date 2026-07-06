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
