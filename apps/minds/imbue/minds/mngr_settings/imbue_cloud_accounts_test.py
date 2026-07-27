import tomllib
from pathlib import Path

import pytest

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings.enablement import set_provider_is_enabled
from imbue.minds.mngr_settings.imbue_cloud_accounts import set_imbue_cloud_provider_for_account
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_LOG_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_VOLUME_HOME_PATH
from imbue.minds.testing import stub_mngr_host_dir

_FAKE_CONNECTOR_URL = "https://test--rsc-api.modal.run"


def test_set_imbue_cloud_provider_for_account_writes_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    changed = set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root=MindsRoot("minds-dev-tname"),
    )
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    block = parsed["providers"]["imbue_cloud_alice-example-com"]
    assert block == {
        "backend": "imbue_cloud",
        "account": "alice@example.com",
        "connector_url": _FAKE_CONNECTOR_URL,
        "is_enabled": True,
        # Runsc + hardening args so the slow (rebuild) path runs under gVisor.
        "docker_runtime": "runsc",
        "install_gvisor_runtime": True,
        "default_start_args": ["--workdir=/", "--security-opt=no-new-privileges"],
        # The user-data layout knobs (see provider_blocks).
        "host_dir": WORKSPACE_HOST_DIR,
        "volume_home_path": WORKSPACE_VOLUME_HOME_PATH,
        "host_log_dir": WORKSPACE_HOST_LOG_DIR,
    }


def test_set_force_enable_re_enables_disabled_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root=MindsRoot("minds-dev-tname"),
    )
    set_provider_is_enabled("imbue_cloud_alice-example-com", False, root=MindsRoot("minds-dev-tname"))

    changed = set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root=MindsRoot("minds-dev-tname"),
        force_enable=True,
    )
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is True


def test_set_preserve_does_not_re_enable_disabled_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bootstrap reconcile path must leave a previously disabled
    provider (e.g. from the providers panel toggle) disabled -- only an
    explicit signin event force-enables.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root=MindsRoot("minds-dev-tname"),
    )
    set_provider_is_enabled("imbue_cloud_alice-example-com", False, root=MindsRoot("minds-dev-tname"))

    changed = set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root=MindsRoot("minds-dev-tname"),
        force_enable=False,
    )
    assert changed is False
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is False


def test_set_imbue_cloud_provider_for_account_also_writes_default_disabled_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: a first signin on a fresh ``MINDS_ROOT_NAME`` must land
    both the per-account block AND the default-disabled
    ``[providers.imbue_cloud]`` suppression block.

    Without the suppression block, ``mngr observe`` auto-creates a phantom
    default ``imbue_cloud`` instance with no ``connector_url``, which
    raises ``MissingConnectorUrlError`` on every discovery cycle and
    breaks ``mngr create`` against the env. ``apply_bootstrap``'s call
    to ``ensure_mngr_settings`` no-ops on a fresh env (the mngr profile
    dir doesn't exist yet at startup), so ``set_imbue_cloud_provider_for_account``
    has to ensure it as part of writing the per-account block.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-staging")
    set_imbue_cloud_provider_for_account(
        "josh@imbue.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root=MindsRoot("minds-staging"),
    )
    parsed = tomllib.loads(settings_path.read_text())
    # The per-account block lands as before.
    assert parsed["providers"]["imbue_cloud_josh-imbue-com"]["connector_url"] == _FAKE_CONNECTOR_URL
    # AND the suppression block + recursive-disable land in the same pass.
    assert parsed["providers"]["imbue_cloud"] == {"backend": "imbue_cloud", "is_enabled": False}
    assert parsed["plugins"]["recursive"]["enabled"] is False


def test_set_imbue_cloud_provider_for_account_repairs_missing_default_block_on_resignin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An already-signed-in user whose settings.toml is missing the
    suppression block (because the original signin happened on a build
    that didn't write it) gets the block back on the next signin event,
    even when the per-account block itself is unchanged and the function
    short-circuits its per-account write path.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-staging")
    # Pre-seed: a per-account block exists but the suppression block is missing
    # (mirrors the on-disk state of a staging env that signed in before the
    # fix landed).
    settings_path.write_text(
        "[providers.imbue_cloud_josh-imbue-com]\n"
        'backend = "imbue_cloud"\n'
        'account = "josh@imbue.com"\n'
        f'connector_url = "{_FAKE_CONNECTOR_URL}"\n'
        "is_enabled = true\n"
        'docker_runtime = "runsc"\n'
        "install_gvisor_runtime = true\n"
        'default_start_args = ["--workdir=/", "--security-opt=no-new-privileges"]\n'
        f'host_dir = "{WORKSPACE_HOST_DIR}"\n'
        f'volume_home_path = "{WORKSPACE_VOLUME_HOME_PATH}"\n'
        f'host_log_dir = "{WORKSPACE_HOST_LOG_DIR}"\n'
    )

    changed = set_imbue_cloud_provider_for_account(
        "josh@imbue.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root=MindsRoot("minds-staging"),
    )
    # The per-account write itself is a no-op (existing block already matches),
    # but the file is still modified because the suppression block lands -- and
    # that modification is reported so the caller bounces ``mngr observe``.
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud"] == {"backend": "imbue_cloud", "is_enabled": False}
    assert parsed["plugins"]["recursive"]["enabled"] is False
