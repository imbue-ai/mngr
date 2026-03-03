"""Shared test fixtures for mng plugin libraries.

Provides common pytest fixtures that plugin libraries need for their tests.
Call register_plugin_test_fixtures(globals()) from a plugin's conftest.py
to register the standard set of fixtures.
"""

from pathlib import Path
from typing import Any
from typing import Generator
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

import imbue.mng.main
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.agents.agent_registry import load_agents_from_plugins
from imbue.mng.agents.agent_registry import reset_agent_registry
from imbue.mng.config.consts import PROFILES_DIRNAME
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.plugins import hookspecs
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.providers.registry import load_local_backend_only
from imbue.mng.providers.registry import reset_backend_registry
from imbue.mng.utils.testing import assert_home_is_temp_directory
from imbue.mng.utils.testing import init_git_repo
from imbue.mng.utils.testing import isolate_home
from imbue.mng.utils.testing import isolate_tmux_server
from imbue.mng.utils.testing import make_mng_ctx


@pytest.fixture
def cli_runner() -> CliRunner:
    """Create a Click CLI runner for testing CLI commands."""
    return CliRunner()


@pytest.fixture(autouse=True)
def plugin_manager() -> Generator[pluggy.PluginManager, None, None]:
    """Create a plugin manager with mng hookspecs and local backend only.

    Also loads external plugins via setuptools entry points to match the behavior
    of load_config(). This ensures that external plugins are discovered and registered.

    This fixture also resets the module-level plugin manager singleton to ensure
    test isolation.
    """
    imbue.mng.main.reset_plugin_manager()
    reset_backend_registry()
    reset_agent_registry()

    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    pm.load_setuptools_entrypoints("mng")
    load_local_backend_only(pm)
    load_agents_from_plugins(pm)

    yield pm

    imbue.mng.main.reset_plugin_manager()
    reset_backend_registry()
    reset_agent_registry()


@pytest.fixture
def temp_host_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for host/mng data."""
    host_dir = tmp_path / ".mng"
    host_dir.mkdir()
    return host_dir


@pytest.fixture
def _isolate_tmux_server(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Give each test its own isolated tmux server.

    Delegates to the shared isolate_tmux_server() context manager in testing.py.
    See its docstring for details on the isolation strategy and why /tmp is used.
    """
    with isolate_tmux_server(monkeypatch):
        yield


@pytest.fixture(autouse=True)
def setup_test_mng_env(
    tmp_path: Path,
    temp_host_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_tmux_server: None,
) -> Generator[None, None, None]:
    """Set up environment variables for all tests."""
    mng_test_id = uuid4().hex
    mng_test_prefix = f"mng_{mng_test_id}-"
    mng_test_root_name = f"mng-test-{mng_test_id}"

    isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("MNG_HOST_DIR", str(temp_host_dir))
    monkeypatch.setenv("MNG_PREFIX", mng_test_prefix)
    monkeypatch.setenv("MNG_ROOT_NAME", mng_test_root_name)

    unison_dir = tmp_path / ".unison"
    unison_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("UNISON", str(unison_dir))

    assert_home_is_temp_directory()

    yield


@pytest.fixture
def cg() -> Generator[ConcurrencyGroup, None, None]:
    """Provide a ConcurrencyGroup for tests that need to run processes."""
    with ConcurrencyGroup(name="test") as group:
        yield group


@pytest.fixture
def setup_git_config(tmp_path: Path) -> None:
    """Create a .gitconfig in the fake HOME so git commands work.

    Use this fixture for any test that runs git commands.
    The temp_git_repo fixture depends on this, so you don't need both.
    """
    gitconfig = tmp_path / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("[user]\n\tname = Test User\n\temail = test@test.com\n")


@pytest.fixture
def temp_git_repo(tmp_path: Path, setup_git_config: None) -> Path:
    """Create a temporary git repository with an initial commit."""
    repo_dir = tmp_path / "git_repo"
    repo_dir.mkdir()
    init_git_repo(repo_dir)
    return repo_dir


@pytest.fixture
def mng_test_id() -> str:
    """Generate a unique test ID for isolation."""
    return uuid4().hex


@pytest.fixture
def mng_test_prefix(mng_test_id: str) -> str:
    """Get the test prefix for tmux session names."""
    return f"mng_{mng_test_id}-"


@pytest.fixture
def mng_test_root_name(mng_test_id: str) -> str:
    """Get the test root name for config isolation."""
    return f"mng-test-{mng_test_id}"


@pytest.fixture
def tmp_home_dir(tmp_path: Path) -> Generator[Path, None, None]:
    yield tmp_path


@pytest.fixture
def temp_profile_dir(temp_host_dir: Path) -> Path:
    """Create a temporary profile directory."""
    profile_dir = temp_host_dir / PROFILES_DIRNAME / uuid4().hex
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


@pytest.fixture
def temp_config(temp_host_dir: Path, mng_test_prefix: str) -> MngConfig:
    """Create a MngConfig with a temporary host directory."""
    return MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix, is_error_reporting_enabled=False)


@pytest.fixture
def temp_mng_ctx(
    temp_config: MngConfig, temp_profile_dir: Path, plugin_manager: pluggy.PluginManager
) -> Generator[MngContext, None, None]:
    """Create a MngContext with a temporary host directory."""
    with ConcurrencyGroup(name="test") as test_cg:
        yield make_mng_ctx(temp_config, plugin_manager, temp_profile_dir, concurrency_group=test_cg)


@pytest.fixture
def local_provider(temp_host_dir: Path, temp_mng_ctx: MngContext) -> LocalProviderInstance:
    """Create a LocalProviderInstance with a temporary host directory."""
    return LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=temp_host_dir,
        mng_ctx=temp_mng_ctx,
    )


def register_plugin_test_fixtures(namespace: dict[str, Any]) -> None:
    """Register common plugin test fixtures into the given namespace.

    Call this from a plugin's conftest.py to get the standard set of fixtures
    needed for testing mng plugins.
    """
    namespace["cg"] = cg
    namespace["cli_runner"] = cli_runner
    namespace["local_provider"] = local_provider
    namespace["mng_test_id"] = mng_test_id
    namespace["mng_test_prefix"] = mng_test_prefix
    namespace["mng_test_root_name"] = mng_test_root_name
    namespace["plugin_manager"] = plugin_manager
    namespace["setup_git_config"] = setup_git_config
    namespace["setup_test_mng_env"] = setup_test_mng_env
    namespace["temp_config"] = temp_config
    namespace["temp_git_repo"] = temp_git_repo
    namespace["temp_host_dir"] = temp_host_dir
    namespace["temp_mng_ctx"] = temp_mng_ctx
    namespace["temp_profile_dir"] = temp_profile_dir
    namespace["tmp_home_dir"] = tmp_home_dir
    namespace["_isolate_tmux_server"] = _isolate_tmux_server
