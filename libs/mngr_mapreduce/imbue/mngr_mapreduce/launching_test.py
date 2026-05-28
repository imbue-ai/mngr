"""Unit tests for framework launching helpers."""

from pathlib import Path

from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import TransferMode
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import LaunchConfig
from imbue.mngr_mapreduce.launching import ROLE_LABEL_KEY
from imbue.mngr_mapreduce.launching import _build_agent_options


def _make_config(provider: str = "local", snapshot: SnapshotName | None = None) -> LaunchConfig:
    """Build a LaunchConfig for unit testing.

    Uses model_construct to skip validation of the source_host field,
    which requires a real OnlineHostInterface that these unit tests don't need.
    """
    return LaunchConfig.model_construct(
        source_dir=Path("/tmp/src"),
        source_host=None,
        base_commit="0" * 40,
        agent_type=AgentTypeName("claude"),
        provider_name=ProviderInstanceName(provider),
        env_options=AgentEnvironmentOptions(),
        label_options=AgentLabelOptions(),
        snapshot=snapshot,
    )


def test_build_agent_options_rsync_disabled() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config(), AgentKind.MAPPER)
    assert opts.data_options.is_rsync_enabled is False


def test_build_agent_options_local_uses_git_mirror() -> None:
    """Local agents use GIT_MIRROR so their branches stay in their own clones,
    keeping the orchestrator's bundle-pull path identical across providers."""
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("local"), AgentKind.MAPPER)
    assert opts.git is not None
    assert opts.transfer_mode == TransferMode.GIT_MIRROR


def test_build_agent_options_remote_uses_git_mirror() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("modal"), AgentKind.MAPPER)
    assert opts.git is not None
    assert opts.transfer_mode == TransferMode.GIT_MIRROR


def test_build_agent_options_local_ready_timeout() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("local"), AgentKind.MAPPER)
    assert opts.ready_timeout_seconds == 10.0


def test_build_agent_options_remote_ready_timeout() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("docker"), AgentKind.MAPPER)
    assert opts.ready_timeout_seconds == 60.0


def test_build_agent_options_passes_env_and_labels() -> None:
    env = AgentEnvironmentOptions(env_vars=(EnvVar(key="FOO", value="bar"),))
    labels = AgentLabelOptions(labels={"batch": "1"})
    config = _make_config()
    config_with_env_and_labels = LaunchConfig.model_construct(
        source_dir=config.source_dir,
        source_host=None,
        base_commit=config.base_commit,
        agent_type=config.agent_type,
        provider_name=config.provider_name,
        env_options=env,
        label_options=labels,
        snapshot=None,
    )
    opts = _build_agent_options(AgentName("test"), "branch", config_with_env_and_labels, AgentKind.MAPPER)
    assert opts.environment.env_vars == (EnvVar(key="FOO", value="bar"),)
    # role label is stamped automatically; everything else is preserved.
    assert opts.label_options.labels == {"batch": "1", ROLE_LABEL_KEY: AgentKind.MAPPER.value}


def test_build_agent_options_sets_agent_name() -> None:
    opts = _build_agent_options(
        AgentName("tmr-my-test-abc123"), "tmr/20260101/my-test", _make_config(), AgentKind.MAPPER
    )
    assert opts.name == AgentName("tmr-my-test-abc123")


def test_build_agent_options_stamps_role_label_for_each_kind() -> None:
    for kind in (AgentKind.MAPPER, AgentKind.SNAPSHOTTER, AgentKind.REDUCER):
        opts = _build_agent_options(AgentName("test"), "branch", _make_config(), kind)
        assert opts.label_options.labels.get(ROLE_LABEL_KEY) == kind.value


def test_build_agent_options_target_path_pins_work_dir() -> None:
    opts = _build_agent_options(
        AgentName("test"), "branch", _make_config("modal"), AgentKind.MAPPER, target_path=Path("/code")
    )
    assert opts.target_path == Path("/code")


def test_build_agent_options_transfer_mode_override_wins() -> None:
    opts = _build_agent_options(
        AgentName("test"),
        "branch",
        _make_config("modal"),
        AgentKind.MAPPER,
        transfer_mode=TransferMode.GIT_WORKTREE,
    )
    assert opts.transfer_mode == TransferMode.GIT_WORKTREE
