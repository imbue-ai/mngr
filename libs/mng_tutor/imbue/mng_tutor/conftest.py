import os
import shutil
import subprocess
import tempfile
from pathlib import Path
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


@pytest.fixture
def cg() -> Generator[ConcurrencyGroup, None, None]:
    """Provide a ConcurrencyGroup for tests that need to run processes."""
    with ConcurrencyGroup(name="test") as group:
        yield group


@pytest.fixture
def cli_runner() -> CliRunner:
    """Create a Click CLI runner for testing CLI commands."""
    return CliRunner()


@pytest.fixture(autouse=True)
def plugin_manager() -> Generator[pluggy.PluginManager, None, None]:
    """Create a plugin manager with mng hookspecs and local backend only."""
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
def _isolate_tmux_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Give each test its own isolated tmux server."""
    tmux_tmpdir = Path(tempfile.mkdtemp(prefix="mng-tmux-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_tmpdir))
    monkeypatch.delenv("TMUX", raising=False)

    yield

    tmux_tmpdir_str = str(tmux_tmpdir)
    assert tmux_tmpdir_str.startswith("/tmp/mng-tmux-"), (
        f"TMUX_TMPDIR safety check failed! Expected /tmp/mng-tmux-* path but got: {tmux_tmpdir_str}. "
        "Refusing to run 'tmux kill-server' to avoid killing the real tmux server."
    )
    socket_path = Path(tmux_tmpdir_str) / f"tmux-{os.getuid()}" / "default"
    kill_env = os.environ.copy()
    kill_env.pop("TMUX", None)
    kill_env["TMUX_TMPDIR"] = tmux_tmpdir_str
    subprocess.run(
        ["tmux", "-S", str(socket_path), "kill-server"],
        capture_output=True,
        env=kill_env,
    )
    shutil.rmtree(tmux_tmpdir, ignore_errors=True)


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
def setup_git_config(tmp_path: Path) -> None:
    """Create a .gitconfig in the fake HOME so git commands work."""
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
def temp_config(temp_host_dir: Path) -> MngConfig:
    """Create a MngConfig with a temporary host directory."""
    mng_test_prefix = os.environ.get("MNG_PREFIX", "mng-test-")
    return MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix, is_error_reporting_enabled=False)


@pytest.fixture
def temp_profile_dir(temp_host_dir: Path) -> Path:
    """Create a temporary profile directory."""
    profile_dir = temp_host_dir / PROFILES_DIRNAME / uuid4().hex
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


@pytest.fixture
def temp_mng_ctx(
    temp_config: MngConfig, temp_profile_dir: Path, plugin_manager: pluggy.PluginManager
) -> Generator[MngContext, None, None]:
    """Create a MngContext with a temporary host directory."""
    with ConcurrencyGroup(name="test") as cg:
        yield MngContext(
            config=temp_config,
            pm=plugin_manager,
            profile_dir=temp_profile_dir,
            is_interactive=False,
            is_auto_approve=False,
            concurrency_group=cg,
        )


@pytest.fixture
def local_provider(temp_host_dir: Path, temp_mng_ctx: MngContext) -> LocalProviderInstance:
    """Create a LocalProviderInstance with a temporary host directory."""
    return LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=temp_host_dir,
        mng_ctx=temp_mng_ctx,
    )
