"""Unit tests for create_plugin_manager."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from imbue.mngr.main import create_plugin_manager
from imbue.mngr.utils.env_utils import parse_bool_env

# Heavy third-party provider SDKs that must NOT load during `mngr` startup. Each is
# large (google-cloud-compute alone is ~900 modules) and, imported eagerly, dominates
# CLI cold-start latency -- the root cause of MIND-179, where a cold `mngr config`
# subprocess exceeded its timeout under CI I/O contention. These three are imported
# only by their own provider backend, which is now registered lazily, so they must not
# appear at startup.
#
# NOTE: boto3/botocore (AWS) and the `modal` SDK are deliberately NOT guarded here.
# They are also imported at startup by non-provider plugins (e.g. mngr_imbue_cloud and
# the sentry uploader import boto3; mngr_schedule imports the modal provider instance),
# so lazy provider registration alone cannot keep them out of startup -- removing them
# is separate follow-up work in those plugins.
_HEAVY_PROVIDER_SDK_PREFIXES: tuple[str, ...] = (
    "google.cloud.compute_v1",
    "azure.mgmt.compute",
    "anthropic",
)


def test_create_plugin_manager_blocks_disabled_plugins(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """create_plugin_manager should block plugins disabled in config files."""
    # MNGR_LOAD_ALL_PLUGINS disables config-based blocking, so if it is set it would
    # silently mask this test. It must never be set during a normal test run, so treat
    # its presence as a leak and fail loudly -- some other test or imported module set
    # it process-wide (e.g. importing scripts/make_cli_docs, which sets it at import
    # time and is expected to pop it again). Surface the leak so it gets fixed at the
    # source rather than papered over here.
    assert not parse_bool_env(os.environ.get("MNGR_LOAD_ALL_PLUGINS", "")), (
        "MNGR_LOAD_ALL_PLUGINS is set in the test environment, which disables plugin "
        "blocking and would mask this test. It leaked into the process from another "
        "test or an imported module (e.g. an importer of scripts/make_cli_docs that "
        "failed to pop it). Find and contain the leak at its source."
    )
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[plugins.modal]\nenabled = false\n"
    )

    pm = create_plugin_manager()

    assert pm.is_blocked("modal")


def test_create_plugin_manager_skips_blocking_when_load_all_plugins_set(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_plugin_manager should skip blocking when MNGR_LOAD_ALL_PLUGINS is truthy."""
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[plugins.modal]\nenabled = false\n"
    )
    monkeypatch.setenv("MNGR_LOAD_ALL_PLUGINS", "1")

    pm = create_plugin_manager()

    assert not pm.is_blocked("modal")


# A cold subprocess import of the whole CLI can take several seconds under CI load,
# so give it headroom above the default 10s (this test exists to prevent the very
# slowness that headroom guards against).
@pytest.mark.timeout(60)
def test_mngr_startup_does_not_eagerly_import_heavy_provider_sdks(tmp_path: Path) -> None:
    """Importing ``imbue.mngr.main`` (what the console script does) must not pull in any
    heavy cloud-provider SDK. Those SDKs are only needed to *operate* a provider, never
    to load config / list / show help / print the version, yet eager loading makes every
    ``mngr`` invocation pay for all of them -- the MIND-179 cold-start regression. The
    check runs in a fresh subprocess (so ``sys.modules`` is clean, unlike the
    collection-polluted test process) and sets ``MNGR_LOAD_ALL_PLUGINS`` so every
    installed plugin is loaded -- the strongest form of the guarantee.
    """
    child_program = (
        "import sys\n"
        "import imbue.mngr.main\n"
        f"prefixes = {_HEAVY_PROVIDER_SDK_PREFIXES!r}\n"
        "leaked = sorted(\n"
        "    name\n"
        "    for name in sys.modules\n"
        "    if any(name == p or name.startswith(p + '.') for p in prefixes)\n"
        ")\n"
        "sys.stdout.write('\\n'.join(leaked))\n"
    )
    # MNGR_LOAD_ALL_PLUGINS forces every plugin to load regardless of local config, so a
    # disabled provider cannot make this pass vacuously. cwd is an empty dir so no project
    # settings.toml interferes.
    env = {**os.environ, "MNGR_LOAD_ALL_PLUGINS": "1"}
    result = subprocess.run(
        [sys.executable, "-c", child_program],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == 0, f"importing imbue.mngr.main failed:\n{result.stderr}"
    leaked = [line for line in result.stdout.splitlines() if line.strip()]
    assert not leaked, (
        "mngr startup eagerly imported heavy provider-SDK modules, regressing CLI cold-start "
        "(MIND-179). Import the offending provider's SDK lazily -- inside the code path that "
        f"actually operates the provider -- not at module top level. Leaked modules: {leaked}"
    )
