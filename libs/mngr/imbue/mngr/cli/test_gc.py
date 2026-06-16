"""Integration tests for the gc CLI command."""

import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.exit_codes import EXIT_CODE_LOCAL_STATE_REMAINS
from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
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


@pytest.mark.allow_warnings(match=r"^Skipping provider fake-unavailable-backend \(unavailable\)")
def test_get_selected_providers_skips_empty_silently_and_records_unavailable(
    temp_mngr_ctx: MngrContext,
) -> None:
    """An explicit --provider that is empty is silently skipped; unavailable is reported.

    Empty providers are known to have nothing to gc, so skipping is safe and
    not a user-visible failure. Unavailable providers, by contrast, have
    unknown state -- the user asked us to gc them specifically and we could
    not reach them, so they come back as an error string and gc exits non-zero.
    """
    backends = (_FakeEmptyBackend, _FakeUnavailableBackend)
    for backend in backends:
        _backend_registry[backend.get_name()] = backend
        register_provider_config(str(backend.get_name()), ProviderInstanceConfig)
    try:
        selected, skipped_errors = _get_selected_providers(
            mngr_ctx=temp_mngr_ctx,
            opts=_make_gc_opts(("fake-empty-backend", "fake-unavailable-backend")),
        )
    finally:
        for backend in backends:
            del _backend_registry[backend.get_name()]
            del _provider_config_registry[backend.get_name()]

    assert selected == []
    assert len(skipped_errors) == 1
    assert "fake-unavailable-backend" in skipped_errors[0]
    assert "unavailable" in skipped_errors[0]


@pytest.mark.allow_warnings(match=r"^Skipping provider fake-unavailable-backend \(unavailable\)")
def test_gc_exits_non_zero_when_explicit_provider_is_unavailable(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """`mngr gc --provider X` with X unavailable exits with the PROVIDER_INACCESSIBLE code.

    The skipped provider's error message is surfaced in the summary so the user
    can see what was not gc'd. gc still runs against any other providers, but
    the overall command fails with the cause-specific exit code (6,
    PROVIDER_INACCESSIBLE) so the explicit request is not silently dropped.
    Empty providers (whose state is known to be empty) take the symmetric
    silent-success path and are exercised by
    test_get_selected_providers_skips_empty_silently_and_records_unavailable.
    """
    _backend_registry[_FakeUnavailableBackend.get_name()] = _FakeUnavailableBackend
    register_provider_config(str(_FakeUnavailableBackend.get_name()), ProviderInstanceConfig)
    try:
        result = cli_runner.invoke(
            gc,
            ["--provider", "fake-unavailable-backend"],
            obj=plugin_manager,
            catch_exceptions=False,
        )
    finally:
        del _backend_registry[_FakeUnavailableBackend.get_name()]
        del _provider_config_registry[_FakeUnavailableBackend.get_name()]

    assert result.exit_code == EXIT_CODE_PROVIDER_INACCESSIBLE, result.output
    assert "fake-unavailable-backend" in result.output
    assert "unavailable" in result.output


@pytest.mark.skipif(
    os.geteuid() == 0, reason="Root bypasses the read-only-dir permission check used to force the failure"
)
@pytest.mark.allow_warnings(match=r"Failed to clean")
def test_gc_exits_with_cause_specific_code_when_work_dir_cleanup_fails(
    cli_runner: CliRunner,
    temp_host_dir: Path,
    per_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """A failed work-dir deletion yields the LOCAL_STATE_REMAINS exit code (4).

    Previously gc exited 0 even when a resource could not be deleted; now the
    failure is recorded as a structured, categorized failure and surfaced both in
    the ``--format json`` ``failures`` key and via a cause-specific exit code. The deletion is
    forced to fail by making the orphaned work dir's parent read-only so ``rm -rf``
    cannot remove it.
    """
    parent_dir = temp_host_dir / "worktrees" / "locked-parent"
    orphaned_dir = parent_dir / "orphaned-agent-789"
    orphaned_dir.mkdir(parents=True)

    # Register the orphan as a generated work dir for the local host so gc tries to delete it.
    _write_certified_data(per_host_dir, temp_host_dir, (str(orphaned_dir),))

    # Make the parent read-only so `rm -rf` of the orphan fails (the entry cannot be unlinked).
    parent_dir.chmod(0o500)
    try:
        result = cli_runner.invoke(
            gc,
            ["--on-error", "continue", "--format", "json"],
            obj=plugin_manager,
            catch_exceptions=False,
        )
    finally:
        # Restore write permission so pytest can clean up the temp dir.
        parent_dir.chmod(0o700)

    assert result.exit_code == EXIT_CODE_LOCAL_STATE_REMAINS, result.output
    summary = json.loads(result.output.strip().splitlines()[-1])
    assert summary["failures"], summary
    assert summary["failures"][0]["category"] == "LOCAL_STATE_REMAINS"
    assert "Failed to clean" in summary["failures"][0]["message"]
