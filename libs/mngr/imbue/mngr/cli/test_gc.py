"""Integration tests for the gc CLI command."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.gc import GcCliOptions
from imbue.mngr.cli.gc import _get_selected_providers
from imbue.mngr.cli.gc import gc
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import _provider_config_registry
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import get_or_create_local_host_id
from imbue.mngr.providers.registry import _backend_registry


def _write_certified_data(per_host_dir: Path, temp_host_dir: Path, generated_work_dirs: tuple[str, ...]) -> Path:
    """Write CertifiedHostData to data.json in the per-host directory. Returns data_path."""
    host_id = get_or_create_local_host_id(temp_host_dir)
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="test-host",
        generated_work_dirs=generated_work_dirs,
        created_at=now,
        updated_at=now,
    )
    data_path = per_host_dir / "data.json"
    data_path.write_text(json.dumps(certified_data.model_dump(by_alias=True, mode="json"), indent=2))
    return data_path


def test_gc_work_dirs_dry_run(
    cli_runner: CliRunner,
    temp_host_dir: Path,
    per_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test gc --dry-run shows orphaned directories without removing them."""
    orphaned_dir = temp_host_dir / "worktrees" / "orphaned-agent-123"
    orphaned_dir.mkdir(parents=True)

    _write_certified_data(per_host_dir, temp_host_dir, (str(orphaned_dir),))

    result = cli_runner.invoke(
        gc,
        ["--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Would destroy" in result.output
    assert str(orphaned_dir) in result.output
    assert orphaned_dir.exists(), "Directory should still exist after dry-run"


def test_gc_removes_orphaned_directory(
    cli_runner: CliRunner,
    temp_host_dir: Path,
    per_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test gc removes orphaned directories and updates certified data."""
    orphaned_dir = temp_host_dir / "worktrees" / "orphaned-agent-456"
    orphaned_dir.mkdir(parents=True)

    test_file = orphaned_dir / "test.txt"
    test_file.write_text("test content")

    data_path = _write_certified_data(per_host_dir, temp_host_dir, (str(orphaned_dir),))

    result = cli_runner.invoke(
        gc,
        [],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Work directories: 1" in result.output
    assert "Destroyed 1 resource(s)" in result.output
    assert not orphaned_dir.exists(), "Orphaned directory should be removed"

    updated_data = CertifiedHostData.model_validate_json(data_path.read_text())
    assert str(orphaned_dir) not in updated_data.generated_work_dirs, "generated_work_dirs should be updated"


class _FakeEmptyBackend(ProviderBackendInterface):
    """Backend whose provider construction reports the provider is empty.

    Mirrors how the Modal backend signals "no per-user environment yet" so we
    can exercise gc's provider-selection skip path without real Modal access.
    """

    @staticmethod
    def get_name() -> ProviderBackendName:
        return ProviderBackendName("fake-empty-backend")

    @staticmethod
    def get_description() -> str:
        return "Fake backend that is always empty."

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ProviderInstanceConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def get_start_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
        is_for_host_creation: bool = False,
    ) -> ProviderInstanceInterface:
        raise ProviderEmptyError(provider_name=name, reason="no state yet (test backend)")


class _FakeUnavailableBackend(ProviderBackendInterface):
    """Backend whose provider construction reports the backend is unreachable."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return ProviderBackendName("fake-unavailable-backend")

    @staticmethod
    def get_description() -> str:
        return "Fake backend that is always unavailable."

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ProviderInstanceConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def get_start_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
        is_for_host_creation: bool = False,
    ) -> ProviderInstanceInterface:
        raise ProviderUnavailableError(provider_name=name, reason="backend offline (test backend)")


def _make_gc_opts(provider: tuple[str, ...]) -> GcCliOptions:
    """Build GcCliOptions for an explicit --provider selection (other flags defaulted)."""
    return GcCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
        dry_run=True,
        on_error="abort",
        all_providers=False,
        provider=provider,
    )


def test_get_selected_providers_skips_empty_and_unavailable_providers(temp_mngr_ctx: MngrContext) -> None:
    """An explicit --provider that is empty or unavailable is skipped, not an error.

    This mirrors how `mngr list --provider modal` already skips a not-yet-created
    Modal environment: gc must stay consistent so `mngr gc --provider modal` does
    not fail just because the provider has no state yet.
    """
    backends = (_FakeEmptyBackend, _FakeUnavailableBackend)
    for backend in backends:
        _backend_registry[backend.get_name()] = backend
        register_provider_config(str(backend.get_name()), ProviderInstanceConfig)
    try:
        selected = _get_selected_providers(
            mngr_ctx=temp_mngr_ctx,
            opts=_make_gc_opts(("fake-empty-backend", "fake-unavailable-backend")),
        )
    finally:
        for backend in backends:
            del _backend_registry[backend.get_name()]
            del _provider_config_registry[backend.get_name()]

    # Both providers declared themselves empty/unavailable at construction, so
    # neither is selected -- gc treats them as "nothing to collect" rather than
    # raising.
    assert selected == []
