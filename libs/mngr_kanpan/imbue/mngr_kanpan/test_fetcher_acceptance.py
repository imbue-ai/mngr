"""Acceptance tests for fetch_board_snapshot and fetch_local_snapshot.

These tests exercise the full fetch pipeline with real agents created via the
local provider, rather than mocking list_agents.

To run these tests locally:

    just test libs/mngr_kanpan/imbue/mngr_kanpan/test_fetcher_acceptance.py
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from imbue.imbue_common.model_update import to_update
from imbue.mngr.cli.testing import create_test_agent_state
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import FIELD_COMMITS_AHEAD
from imbue.mngr_kanpan.data_source import FIELD_MUTED
from imbue.mngr_kanpan.data_source import FIELD_REPO_PATH
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_sources.git_info import CommitsAheadField
from imbue.mngr_kanpan.data_sources.git_info import GitInfoDataSource
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathField
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathsDataSource
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.fetcher import FetchResult
from imbue.mngr_kanpan.fetcher import fetch_board_snapshot
from imbue.mngr_kanpan.fetcher import fetch_local_snapshot
from imbue.mngr_kanpan.fetcher import toggle_agent_mute


class _FakeRemoteDataSource:
    """A fake remote data source used in fetch_local_snapshot tests."""

    @property
    def name(self) -> str:
        return "fake_remote"

    @property
    def is_remote(self) -> bool:
        return True

    @property
    def columns(self) -> dict[str, str]:
        return {FIELD_REPO_PATH: "FAKE"}

    @property
    def field_types(self) -> dict[str, TypeAdapter[FieldValue]]:
        return {FIELD_REPO_PATH: TypeAdapter(RepoPathField)}

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], list[str]]:
        return (
            {
                AgentName("git-local-agent"): {
                    FIELD_REPO_PATH: RepoPathField(
                        path="should/not/appear",
                        created=datetime.now(tz=timezone.utc),
                    )
                }
            },
            [],
        )


@pytest.fixture
def local_host(local_provider: LocalProviderInstance) -> Host:
    """Create a local Host via the local provider."""
    return local_provider.create_host(HostName(LOCAL_HOST_NAME))


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """Create a temporary work directory for agents."""
    d = tmp_path / "work_dir"
    d.mkdir()
    return d


def _ctx_with_failing_provider(mngr_ctx: MngrContext) -> MngrContext:
    """Return a copy of ``mngr_ctx`` with an extra provider that fails to load.

    The provider references a backend that does not exist, so any attempt to
    instantiate or discover it raises. This stands in for a real provider that
    becomes unreachable during a refresh -- e.g. a remote provider behind a
    flaky network connection -- which is the condition that used to make muted
    agents leak out of the Muted section.
    """
    failing_config = ProviderInstanceConfig(backend=ProviderBackendName("nonexistent-backend-xyz"))
    updated_config = mngr_ctx.config.model_copy_update(
        to_update(
            mngr_ctx.config.field_ref().providers,
            {**mngr_ctx.config.providers, ProviderInstanceName("failing-provider"): failing_config},
        ),
    )
    return mngr_ctx.model_copy_update(to_update(mngr_ctx.field_ref().config, updated_config))


# =============================================================================
# fetch_board_snapshot
# =============================================================================


@pytest.mark.acceptance
def test_fetch_board_snapshot_with_no_agents(temp_mngr_ctx: MngrContext) -> None:
    """Board snapshot with no real agents returns an empty snapshot."""
    result = fetch_board_snapshot(temp_mngr_ctx, [], {})
    assert isinstance(result, FetchResult)
    assert isinstance(result.snapshot, BoardSnapshot)
    assert result.snapshot.entries == ()
    assert result.cached_fields == {}


@pytest.mark.acceptance
@pytest.mark.tmux
def test_fetch_board_snapshot_real_agent_gets_entry(
    local_host: Host,
    work_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A real agent created via local provider shows up in the board snapshot."""
    create_test_agent_state(local_host, work_dir, "snapshot-agent")
    result = fetch_board_snapshot(temp_mngr_ctx, [], {})
    assert isinstance(result.snapshot, BoardSnapshot)
    names = [e.name for e in result.snapshot.entries]
    assert AgentName("snapshot-agent") in names


@pytest.mark.acceptance
@pytest.mark.tmux
def test_fetch_board_snapshot_entry_has_correct_fields(
    local_host: Host,
    work_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Board entry for a real agent has expected field structure."""
    create_test_agent_state(local_host, work_dir, "fields-agent")
    result = fetch_board_snapshot(temp_mngr_ctx, [], {})
    entries = {e.name: e for e in result.snapshot.entries}
    entry = entries[AgentName("fields-agent")]
    assert isinstance(entry, AgentBoardEntry)
    assert FIELD_MUTED in entry.fields
    muted_field = entry.fields[FIELD_MUTED]
    assert isinstance(muted_field, BoolField)
    assert muted_field.value is False
    assert entry.is_muted is False
    assert entry.section == BoardSection.STILL_COOKING


@pytest.mark.acceptance
@pytest.mark.tmux
def test_fetch_board_snapshot_with_repo_paths_source(
    local_host: Host,
    work_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """RepoPathsDataSource populates repo_path field from agent label."""
    agent = create_test_agent_state(local_host, work_dir, "repo-paths-agent")
    agent.set_labels({"remote": "git@github.com:org/myrepo.git"})
    result = fetch_board_snapshot(temp_mngr_ctx, [RepoPathsDataSource()], {})
    entries = {e.name: e for e in result.snapshot.entries}
    entry = entries[AgentName("repo-paths-agent")]
    assert FIELD_REPO_PATH in entry.fields
    repo_field = entry.fields[FIELD_REPO_PATH]
    assert isinstance(repo_field, RepoPathField)
    assert repo_field.path == "org/myrepo"


@pytest.mark.acceptance
@pytest.mark.tmux
def test_fetch_board_snapshot_with_git_info_source(
    local_host: Host,
    temp_git_repo: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """GitInfoDataSource populates commits_ahead field from agent work dir."""
    create_test_agent_state(local_host, temp_git_repo, "git-info-agent")
    result = fetch_board_snapshot(temp_mngr_ctx, [GitInfoDataSource()], {})
    entries = {e.name: e for e in result.snapshot.entries}
    entry = entries[AgentName("git-info-agent")]
    assert FIELD_COMMITS_AHEAD in entry.fields
    commits_field = entry.fields[FIELD_COMMITS_AHEAD]
    assert isinstance(commits_field, CommitsAheadField)


@pytest.mark.acceptance
@pytest.mark.tmux
def test_fetch_board_snapshot_work_dir_set_for_local_agent(
    local_host: Host,
    work_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Local agent with an existing work_dir has it populated on the board entry."""
    create_test_agent_state(local_host, work_dir, "work-dir-agent")
    result = fetch_board_snapshot(temp_mngr_ctx, [], {})
    entries = {e.name: e for e in result.snapshot.entries}
    entry = entries[AgentName("work-dir-agent")]
    assert entry.work_dir == work_dir


@pytest.mark.acceptance
@pytest.mark.tmux
def test_fetch_board_snapshot_cached_fields_updated(
    local_host: Host,
    work_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """cached_fields in the result includes fields for the real agent."""
    agent = create_test_agent_state(local_host, work_dir, "cache-agent")
    agent.set_labels({"remote": "git@github.com:org/repo.git"})
    result = fetch_board_snapshot(temp_mngr_ctx, [RepoPathsDataSource()], {})
    assert AgentName("cache-agent") in result.cached_fields


# =============================================================================
# fetch_local_snapshot
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.tmux
def test_fetch_local_snapshot_skips_remote_sources(
    local_host: Host,
    temp_git_repo: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """fetch_local_snapshot only runs non-remote data sources.

    GitInfoDataSource (is_remote=False) should run; a fabricated remote source
    should be skipped.
    """
    create_test_agent_state(local_host, temp_git_repo, "git-local-agent")
    result = fetch_local_snapshot(
        temp_mngr_ctx,
        [GitInfoDataSource(), _FakeRemoteDataSource()],
        {},
    )
    entries = {e.name: e for e in result.snapshot.entries}
    entry = entries[AgentName("git-local-agent")]
    # commits_ahead is from GitInfoDataSource (local), so it should be present
    assert FIELD_COMMITS_AHEAD in entry.fields
    # repo_path would only come from the remote source, so it should be absent
    assert FIELD_REPO_PATH not in entry.fields


# =============================================================================
# toggle_agent_mute
# =============================================================================


@pytest.mark.acceptance
def test_toggle_agent_mute_twice_returns_to_unmuted(
    local_host: Host,
    work_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Toggling mute twice returns the agent to its unmuted state.

    ``toggle_agent_mute`` reads and writes the persisted certified data, so its
    return values reflect the round trip: first -> True (now muted), second ->
    False (back to unmuted).
    """
    create_test_agent_state(local_host, work_dir, "double-toggle-agent")
    first = toggle_agent_mute(temp_mngr_ctx, AgentName("double-toggle-agent"))
    assert first is True
    second = toggle_agent_mute(temp_mngr_ctx, AgentName("double-toggle-agent"))
    assert second is False


@pytest.mark.acceptance
@pytest.mark.tmux
def test_fetch_board_snapshot_muted_agent_in_muted_section(
    local_host: Host,
    work_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A muted agent appears in the MUTED section of the board snapshot."""
    create_test_agent_state(local_host, work_dir, "muted-section-agent")
    toggle_agent_mute(temp_mngr_ctx, AgentName("muted-section-agent"))
    result = fetch_board_snapshot(temp_mngr_ctx, [], {})
    entries = {e.name: e for e in result.snapshot.entries}
    entry = entries[AgentName("muted-section-agent")]
    assert entry.is_muted is True
    assert entry.section == BoardSection.MUTED
    muted_field = entry.fields[FIELD_MUTED]
    assert isinstance(muted_field, BoolField)
    assert muted_field.value is True


@pytest.mark.acceptance
@pytest.mark.tmux
def test_fetch_board_snapshot_muted_agent_stays_muted_when_a_provider_fails(
    local_host: Host,
    work_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A muted agent stays in the MUTED section even when a provider fails to load.

    Reproduces the symptom of a transient provider-discovery failure: when a
    provider's discovery fails during a refresh, the muted agent used to lose
    its muted bit and get reclassified by PR state -- landing in PRS_FAILED once
    the GitHub fetch also failed, so it appeared mixed in with the non-muted
    rows. The muted bit now rides on each agent's AgentDetails (via kanpan's
    field generators) and is sourced through ``list_agents``, which tolerates a
    failing provider, so the muted agent must remain in MUTED.
    """
    create_test_agent_state(local_host, work_dir, "muted-despite-failure-agent")
    toggle_agent_mute(temp_mngr_ctx, AgentName("muted-despite-failure-agent"))

    failing_ctx = _ctx_with_failing_provider(temp_mngr_ctx)
    result = fetch_board_snapshot(failing_ctx, [], {})

    # Sanity-check that the failing provider was actually exercised, so this test
    # genuinely covers the provider-failure path rather than passing vacuously.
    assert any("nonexistent-backend-xyz" in error for error in result.snapshot.errors)

    entries = {e.name: e for e in result.snapshot.entries}
    entry = entries[AgentName("muted-despite-failure-agent")]
    assert entry.is_muted is True
    assert entry.section == BoardSection.MUTED
