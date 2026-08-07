"""Test fixtures for mngr-schedule.

Uses shared plugin test fixtures from mngr to avoid duplicating common
fixture code across plugin libraries.
"""

from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_schedule.data_types import ModalScheduleCreationRecord
from imbue.mngr_schedule.data_types import ScheduleCreationRecord
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition
from imbue.mngr_schedule.data_types import ScheduledMngrCommand

register_plugin_test_fixtures(globals())


@pytest.fixture()
def set_test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a dummy ANTHROPIC_API_KEY for tests that exercise deploy hooks.

    The claude plugin's modify_env_vars_for_deploy hook requires an API key.
    Tests that invoke stage_deploy_files, _stage_consolidated_env, or the
    modify_env_vars_for_deploy hook with temp_mngr_ctx should request this
    fixture to avoid UserInputError.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key-for-tests")


@pytest.fixture()
def bare_plugin_manager() -> pluggy.PluginManager:
    """Create a plugin manager with hookspecs only, no plugins registered."""
    from imbue.mngr.plugins import hookspecs

    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    return pm


def _build_mngr_ctx(pm: pluggy.PluginManager, tmp_path: Path) -> MngrContext:
    """Build a MngrContext for testing."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(exist_ok=True)
    config = MngrConfig(default_host_dir=tmp_path / ".mngr")
    return MngrContext(
        config=config,
        pm=pm,
        profile_dir=profile_dir,
        concurrency_group=ConcurrencyGroup(name="test"),
    )


@pytest.fixture()
def temp_mngr_ctx(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
) -> MngrContext:
    """MngrContext with all plugins loaded (via plugin_manager fixture)."""
    return _build_mngr_ctx(plugin_manager, tmp_path)


@pytest.fixture()
def bare_temp_mngr_ctx(
    tmp_path: Path,
    bare_plugin_manager: pluggy.PluginManager,
) -> MngrContext:
    """MngrContext with no plugins loaded (bare hookspecs only)."""
    return _build_mngr_ctx(bare_plugin_manager, tmp_path)


def _build_test_trigger(
    name: str = "test-trigger",
    *,
    command: ScheduledMngrCommand = ScheduledMngrCommand.CREATE,
    args: str = "--message hello",
    schedule_cron: str = "0 2 * * *",
    provider: str = "local",
    is_enabled: bool = True,
    git_image_hash: str = "",
) -> ScheduleTriggerDefinition:
    """Build a ScheduleTriggerDefinition for unit tests.

    Defaults describe a minimal local-provider CREATE trigger; keyword
    arguments override individual fields so a single factory covers the
    local, modal, disabled, and image-hash variants that the tests need.

    Module-level so the ``make_test_trigger`` fixture can expose it directly
    without defining an inline closure (which the inline-function ratchet
    disallows).
    """
    return ScheduleTriggerDefinition(
        name=name,
        command=command,
        args=args,
        schedule_cron=schedule_cron,
        provider=provider,
        is_enabled=is_enabled,
        git_image_hash=git_image_hash,
    )


@pytest.fixture()
def make_test_trigger() -> Callable[..., ScheduleTriggerDefinition]:
    """Factory fixture returning ``_build_test_trigger``.

    Callers invoke ``make_test_trigger()`` for the default name or
    ``make_test_trigger("custom-name", provider="modal", ...)`` to override
    individual fields. Used across schedule unit tests so a change to the
    trigger shape only needs to be made in one place.
    """
    return _build_test_trigger


def _build_schedule_record(
    *,
    trigger: ScheduleTriggerDefinition | None = None,
    is_modal: bool = True,
    full_commandline: str = "uv run mngr schedule add --command create",
    hostname: str = "dev-laptop",
    working_directory: str = "/home/user/project",
    mngr_git_hash: str = "fedcba654321",
    created_at: datetime | None = None,
    app_name: str = "mngr-schedule-nightly-build",
    environment: str = "mngr-user1",
) -> ScheduleCreationRecord:
    """Build a ScheduleCreationRecord for unit tests.

    Returns a ``ModalScheduleCreationRecord`` by default (``is_modal=True``),
    or a base ``ScheduleCreationRecord`` when ``is_modal=False``. The trigger
    defaults to a modal/local CREATE trigger matching ``is_modal``; callers
    pass ``trigger=make_test_trigger(...)`` to control its fields.
    """
    if trigger is None:
        trigger = _build_test_trigger("nightly-build", provider="modal" if is_modal else "local")
    if created_at is None:
        created_at = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
    if not is_modal:
        return ScheduleCreationRecord(
            trigger=trigger,
            full_commandline=full_commandline,
            hostname=hostname,
            working_directory=working_directory,
            mngr_git_hash=mngr_git_hash,
            created_at=created_at,
        )
    return ModalScheduleCreationRecord(
        trigger=trigger,
        full_commandline=full_commandline,
        hostname=hostname,
        working_directory=working_directory,
        mngr_git_hash=mngr_git_hash,
        created_at=created_at,
        app_name=app_name,
        environment=environment,
    )


@pytest.fixture()
def make_schedule_record() -> Callable[..., ScheduleCreationRecord]:
    """Factory fixture returning ``_build_schedule_record``.

    Centralizes construction of ``ScheduleCreationRecord`` /
    ``ModalScheduleCreationRecord`` instances for the CLI output tests so the
    record shape lives in one place.
    """
    return _build_schedule_record


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override HOME for a test that relies on ``Path.home()`` pointing at
    an empty directory.

    The autouse ``setup_test_mngr_env`` fixture (in plugin_testing.py) sets
    HOME to ``tmp_path`` and writes ``~/.mngr/config.toml`` into it via
    ``register_test_sleep_agent_type``. Tests that assert on the contents
    of ``~/.mngr`` therefore need their own clean HOME, not the one the
    autouse fixture shares with ``host_dir``.
    """
    home = tmp_path / "isolated_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def temp_git_repo_with_remote(temp_git_repo: Path, tmp_path: Path) -> Path:
    """A temp git repo with a bare ``origin`` remote and its branch pushed.

    Builds on ``temp_git_repo`` (which pulls in ``setup_git_config`` for
    system-config isolation), adds a bare remote, and pushes the initial
    branch with upstream tracking so push-state assertions are deterministic.
    Returns the working-tree repo path.
    """
    remote_path = tmp_path / "remote.git"
    run_git_command(tmp_path, "init", "--bare", str(remote_path))
    run_git_command(temp_git_repo, "remote", "add", "origin", str(remote_path))
    branch = run_git_command(temp_git_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    run_git_command(temp_git_repo, "push", "-u", "origin", branch)
    return temp_git_repo


@pytest.fixture()
def monorepo_root() -> Path:
    """Get the monorepo root from this file's location.

    mngr schedule add needs to package the repo, so the subprocess must run
    from the monorepo root. We can't use cwd because isolate_home() chdir's
    to a temp directory.

    The path is derived from this file's location
    (libs/mngr_schedule/imbue/mngr_schedule/conftest.py), mirroring the
    pattern used in libs/mngr/imbue/mngr/conftest.py and other sibling
    modules. Avoiding a git subprocess keeps fixtures fast and makes the
    fixture work in non-git checkouts.
    """
    return Path(__file__).resolve().parents[4]
