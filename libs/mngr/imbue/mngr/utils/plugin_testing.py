"""Shared test fixtures for mngr plugin libraries.

Provides common pytest fixtures that plugin libraries need for their tests.
Call register_plugin_test_fixtures(globals()) from a plugin's conftest.py
to register the standard set of fixtures.
"""

import importlib.resources
import textwrap
from pathlib import Path
from typing import Any
from typing import Generator
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

import imbue.mngr.main
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr import resources as mngr_resources
from imbue.mngr.agents.agent_registry import load_agents_from_plugins
from imbue.mngr.agents.agent_registry import reset_agent_registry
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.api.providers import reset_provider_instances
from imbue.mngr.config.agent_class_registry import is_agent_class_registered
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_config_registry import is_agent_config_registered
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.consts import PROFILES_DIRNAME
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.main import load_plugin_hookspecs
from imbue.mngr.plugins import hookspecs
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.registry import load_local_backend_only
from imbue.mngr.providers.registry import reset_backend_registry
from imbue.mngr.utils.testing import capture_log_warnings
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import isolate_git
from imbue.mngr.utils.testing import isolate_tmux_server
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr.utils.testing import setup_mngr_test_environment

# Canonical placeholder agent type used across the test suite as an
# "any agent type" stand-in. ``resolve_agent_type`` requires every name to
# be known, so this gets pre-registered by the ``plugin_manager`` fixture
# below. Tests that need a placeholder type should use this name -- helpers
# like ``create_test_agent`` also default to it.
PLACEHOLDER_AGENT_TYPE: str = "generic"


def register_placeholder_agent_type(name: str) -> None:
    """Register a single agent-type name as BaseAgent + base AgentTypeConfig if unregistered.

    Idempotent: existing registrations under ``name`` are left alone. Use this
    in test helpers that take an arbitrary agent-type name and want it to
    pass through ``resolve_agent_type``'s known-type gate without scattering
    register_agent_class / register_agent_config calls across the suite.
    """
    if not is_agent_class_registered(name):
        register_agent_class(name, BaseAgent)
    if not is_agent_config_registered(name):
        register_agent_config(name, AgentTypeConfig)


def register_test_placeholder_agent_type() -> None:
    """Register the canonical placeholder agent type as a SendKeysAgent fixture.

    Registered as ``SendKeysAgent`` (an interactive ``BaseAgent`` that accepts
    messages via tmux send-keys) rather than the bare ``BaseAgent`` that
    ``register_placeholder_agent_type`` uses, so the canonical "any agent" stand-in
    is messageable -- the message-API tests send to it, and most real agent types
    are interactive. ``SendKeysAgent`` IS-A ``BaseAgent``, so tests that use the
    placeholder as a generic agent are unaffected.
    """
    if not is_agent_class_registered(PLACEHOLDER_AGENT_TYPE):
        register_agent_class(PLACEHOLDER_AGENT_TYPE, SendKeysAgent)
    if not is_agent_config_registered(PLACEHOLDER_AGENT_TYPE):
        register_agent_config(PLACEHOLDER_AGENT_TYPE, AgentTypeConfig)


@pytest.fixture
def cli_runner() -> CliRunner:
    """Create a Click CLI runner for testing CLI commands."""
    return CliRunner()


@pytest.fixture()
def log_warnings() -> Generator[list[str], None, None]:
    """Capture loguru warning messages for assertion in tests.

    Delegates to capture_log_warnings() in testing.py (the single source of
    truth shared with mngr/conftest.py's identically-named fixture).
    """
    with capture_log_warnings() as messages:
        yield messages


@pytest.fixture(autouse=True)
def plugin_manager() -> Generator[pluggy.PluginManager, None, None]:
    """Create a plugin manager with mngr hookspecs and local backend only.

    Also loads external plugins via setuptools entry points to match the behavior
    of load_config(). This ensures that external plugins are discovered and registered.

    This fixture also resets the module-level plugin manager singleton to ensure
    test isolation.
    """
    imbue.mngr.main.reset_plugin_manager()
    reset_backend_registry()
    reset_agent_registry()

    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    pm.load_setuptools_entrypoints("mngr")
    load_plugin_hookspecs(pm)
    load_local_backend_only(pm)
    load_agents_from_plugins(pm)
    register_test_placeholder_agent_type()

    yield pm

    imbue.mngr.main.reset_plugin_manager()
    reset_backend_registry()
    reset_agent_registry()


@pytest.fixture
def temp_host_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for host/mngr data."""
    host_dir = tmp_path / ".mngr"
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
def setup_test_mngr_env(
    tmp_home_dir: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    mngr_test_root_name: str,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_tmux_server: None,
) -> Generator[None, None, None]:
    """Set up environment variables for all tests."""
    setup_mngr_test_environment(tmp_home_dir, temp_host_dir, mngr_test_prefix, mngr_test_root_name, monkeypatch)

    yield


@pytest.fixture
def cg() -> Generator[ConcurrencyGroup, None, None]:
    """Provide a ConcurrencyGroup for tests that need to run processes."""
    with ConcurrencyGroup(name="test") as group:
        yield group


@pytest.fixture
def setup_git_config(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Isolate git and provide user config for tests that run git commands.

    Sets GIT_CONFIG_NOSYSTEM and GIT_TERMINAL_PROMPT, and writes a
    .gitconfig to the fake HOME via the shared isolate_git() helper.
    Tests that need git should request this fixture (or temp_git_repo,
    which depends on it).
    """
    with isolate_git(monkeypatch):
        yield


@pytest.fixture
def temp_git_repo(tmp_path: Path, setup_git_config: None) -> Path:
    """Create a temporary git repository with an initial commit."""
    repo_dir = tmp_path / "git_repo"
    init_git_repo(repo_dir)
    return repo_dir


@pytest.fixture
def mngr_test_id() -> str:
    """Generate a unique test ID for isolation."""
    return uuid4().hex


@pytest.fixture
def mngr_test_prefix(mngr_test_id: str) -> str:
    """Get the test prefix for tmux session names."""
    return f"mngr_{mngr_test_id}-"


@pytest.fixture
def mngr_test_root_name(mngr_test_id: str) -> str:
    """Get the test root name for config isolation."""
    return f"mngr-test-{mngr_test_id}"


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
def temp_config(temp_host_dir: Path, mngr_test_prefix: str) -> MngrConfig:
    """Create a MngrConfig with a temporary host directory."""
    return MngrConfig(default_host_dir=temp_host_dir, prefix=mngr_test_prefix, is_error_reporting_enabled=False)


@pytest.fixture
def temp_mngr_ctx(
    temp_config: MngrConfig, temp_profile_dir: Path, plugin_manager: pluggy.PluginManager
) -> Generator[MngrContext, None, None]:
    """Create a MngrContext with a temporary host directory."""
    with ConcurrencyGroup(name="test") as test_cg:
        yield make_mngr_ctx(temp_config, plugin_manager, temp_profile_dir, concurrency_group=test_cg)
    # Clear the provider instance cache so cached instances don't outlive the
    # ConcurrencyGroup that was just torn down.
    reset_provider_instances()


@pytest.fixture
def local_provider(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> LocalProviderInstance:
    """Create a LocalProviderInstance with a temporary host directory."""
    return LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )


@pytest.fixture
def per_host_dir(temp_host_dir: Path) -> Path:
    """Get the host directory for the local provider.

    This is the directory where host-scoped data lives: agents/, data.json,
    activity/, etc. This is the same as temp_host_dir (e.g. ~/.mngr/).
    """
    return temp_host_dir


@pytest.fixture
def local_host(local_provider: LocalProviderInstance) -> Host:
    """Create a local Host via the local provider.

    This fixture eliminates the repeated pattern of:
        host = local_provider.create_host(HostName(LOCAL_HOST_NAME))

    Use this when tests need a Host instance for creating agents,
    executing commands, etc. The local provider always returns a Host
    (the concrete OnlineHostInterface implementation).
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    return host


@pytest.fixture
def temp_work_dir(tmp_path: Path) -> Path:
    """Create a temporary work_dir directory for agents."""
    work_dir = tmp_path / "work_dir"
    work_dir.mkdir()
    return work_dir


@pytest.fixture
def project_config_dir(temp_git_repo: Path, mngr_test_root_name: str) -> Path:
    """Return the project config directory inside the test git repo, creating it.

    The directory is named `.{mngr_test_root_name}` (e.g., `.mngr-test-abc123`).
    Tests can write `settings.toml` or `settings.local.toml` into this directory
    to configure project-level settings for a test.
    """
    config_dir = temp_git_repo / f".{mngr_test_root_name}"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


@pytest.fixture
def stub_mngr_log_sh() -> str:
    """A no-op mngr_log.sh stub for testing shell scripts that source it.

    Background scripts in mngr_claude/resources and mngr_antigravity/resources
    source $MNGR_AGENT_STATE_DIR/commands/mngr_log.sh for logging helpers.
    In production the file is provisioned by Host.provision_agent(); tests
    write this stub to the same path so the script under test can source it
    without doing real I/O.
    """
    return textwrap.dedent("""\
        #!/bin/bash
        mngr_timestamp() { date -u +"%Y-%m-%dT%H:%M:%S.000000000Z"; }
        log_info() { :; }
        log_debug() { :; }
        log_warn() { :; }
        log_error() { :; }
    """)


@pytest.fixture
def mngr_transcript_lib_sh() -> str:
    """Real mngr_transcript_lib.sh contents for shell tests that source it.

    Returns the actual library body (not a stub) because the streamer
    scripts exercise the shared primitives end-to-end. Tests write this to
    ``$MNGR_AGENT_STATE_DIR/commands/mngr_transcript_lib.sh`` to mirror the
    production layout established by ``Host._ensure_shared_shell_libs``.
    """
    return importlib.resources.files(mngr_resources).joinpath("mngr_transcript_lib.sh").read_text()


def register_plugin_test_fixtures(namespace: dict[str, Any]) -> None:
    """Register common plugin test fixtures into the given namespace.

    Call this from a plugin's conftest.py to get the standard set of fixtures
    needed for testing mngr plugins. This is the single sanctioned way for an
    mngr plugin to inherit the shared test infrastructure -- in particular the
    autouse ``setup_test_mngr_env`` fixture, which redirects HOME to a temp dir
    so plugin tests cannot read or write the real ~/.mngr or ~/.claude.json.
    """
    namespace["cg"] = cg
    namespace["cli_runner"] = cli_runner
    namespace["local_host"] = local_host
    namespace["local_provider"] = local_provider
    namespace["log_warnings"] = log_warnings
    namespace["mngr_test_id"] = mngr_test_id
    namespace["mngr_test_prefix"] = mngr_test_prefix
    namespace["mngr_test_root_name"] = mngr_test_root_name
    namespace["mngr_transcript_lib_sh"] = mngr_transcript_lib_sh
    namespace["per_host_dir"] = per_host_dir
    namespace["plugin_manager"] = plugin_manager
    namespace["project_config_dir"] = project_config_dir
    namespace["setup_git_config"] = setup_git_config
    namespace["setup_test_mngr_env"] = setup_test_mngr_env
    namespace["stub_mngr_log_sh"] = stub_mngr_log_sh
    namespace["temp_config"] = temp_config
    namespace["temp_git_repo"] = temp_git_repo
    namespace["temp_host_dir"] = temp_host_dir
    namespace["temp_mngr_ctx"] = temp_mngr_ctx
    namespace["temp_profile_dir"] = temp_profile_dir
    namespace["temp_work_dir"] = temp_work_dir
    namespace["tmp_home_dir"] = tmp_home_dir
    namespace["_isolate_tmux_server"] = _isolate_tmux_server
