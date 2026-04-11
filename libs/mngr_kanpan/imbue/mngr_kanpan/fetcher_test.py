from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from imbue.mngr.api.list import ErrorInfo
from imbue.mngr.api.list import ListResult
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import CiField
from imbue.mngr_kanpan.data_source import CiStatus
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_MUTED
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanDataSource
from imbue.mngr_kanpan.data_source import KanpanFieldTypeError
from imbue.mngr_kanpan.data_source import PrField
from imbue.mngr_kanpan.data_source import PrState
from imbue.mngr_kanpan.data_source import StringField
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import DataSourceConfig
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.data_types import ShellCommandSourceConfig
from imbue.mngr_kanpan.fetcher import _get_local_work_dir
from imbue.mngr_kanpan.fetcher import _is_agent_muted
from imbue.mngr_kanpan.fetcher import _load_muted_agents
from imbue.mngr_kanpan.fetcher import _parse_github_repo_path
from imbue.mngr_kanpan.fetcher import _run_data_sources_parallel
from imbue.mngr_kanpan.fetcher import collect_data_sources
from imbue.mngr_kanpan.fetcher import compute_section
from imbue.mngr_kanpan.fetcher import fetch_board_snapshot
from imbue.mngr_kanpan.fetcher import fetch_local_snapshot
from imbue.mngr_kanpan.fetcher import load_field_cache
from imbue.mngr_kanpan.fetcher import repo_path_from_labels
from imbue.mngr_kanpan.fetcher import save_field_cache
from imbue.mngr_kanpan.plugin import kanpan_data_sources
from imbue.mngr_kanpan.testing import make_agent_details

# === repo path parsing ===


def test_parse_ssh_url() -> None:
    assert _parse_github_repo_path("git@github.com:imbue-ai/mngr.git") == "imbue-ai/mngr"


def test_parse_ssh_url_without_git_suffix() -> None:
    assert _parse_github_repo_path("git@github.com:imbue-ai/mngr") == "imbue-ai/mngr"


def test_parse_https_url() -> None:
    assert _parse_github_repo_path("https://github.com/imbue-ai/mngr.git") == "imbue-ai/mngr"


def test_parse_https_url_without_git_suffix() -> None:
    assert _parse_github_repo_path("https://github.com/imbue-ai/mngr") == "imbue-ai/mngr"


def test_parse_non_github_url() -> None:
    assert _parse_github_repo_path("https://gitlab.com/org/repo.git") is None


def test_repo_path_from_labels_with_remote() -> None:
    assert repo_path_from_labels({"remote": "git@github.com:org/repo.git"}) == "org/repo"


def test_repo_path_from_labels_without_remote() -> None:
    assert repo_path_from_labels({}) is None


# === compute_section ===


def _make_pr(state: PrState = PrState.OPEN, is_draft: bool = False) -> PrField:
    return PrField(
        number=1,
        title="Test PR",
        state=state,
        url="https://github.com/org/repo/pull/1",
        head_branch="test-branch",
        is_draft=is_draft,
    )


def test_compute_section_muted() -> None:
    fields: dict[str, FieldValue] = {FIELD_MUTED: BoolField(value=True)}
    assert compute_section(fields) == BoardSection.MUTED


def test_compute_section_muted_false() -> None:
    fields: dict[str, FieldValue] = {FIELD_MUTED: BoolField(value=False)}
    assert compute_section(fields) == BoardSection.STILL_COOKING


def test_compute_section_no_pr() -> None:
    fields: dict[str, FieldValue] = {}
    assert compute_section(fields) == BoardSection.STILL_COOKING


def test_compute_section_draft_pr() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: _make_pr(is_draft=True)}
    assert compute_section(fields) == BoardSection.STILL_COOKING


def test_compute_section_merged_pr() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: _make_pr(state=PrState.MERGED)}
    assert compute_section(fields) == BoardSection.PR_MERGED


def test_compute_section_closed_pr() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: _make_pr(state=PrState.CLOSED)}
    assert compute_section(fields) == BoardSection.PR_CLOSED


def test_compute_section_open_pr_no_ci() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: _make_pr()}
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_open_pr_ci_failing() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: CiField(status=CiStatus.FAILING),
    }
    assert compute_section(fields) == BoardSection.PRS_FAILED


def test_compute_section_open_pr_ci_passing() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: CiField(status=CiStatus.PASSING),
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_open_pr_ci_pending() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: CiField(status=CiStatus.PENDING),
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_open_pr_ci_unknown() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: CiField(status=CiStatus.UNKNOWN),
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_wrong_muted_type() -> None:
    fields: dict[str, FieldValue] = {FIELD_MUTED: StringField(value="yes")}
    with pytest.raises(KanpanFieldTypeError, match="Expected BoolField"):
        compute_section(fields)


def test_compute_section_wrong_pr_type() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: StringField(value="oops")}
    with pytest.raises(KanpanFieldTypeError, match="Expected PrField"):
        compute_section(fields)


def test_compute_section_wrong_ci_type() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: StringField(value="oops"),
    }
    with pytest.raises(KanpanFieldTypeError, match="Expected CiField"):
        compute_section(fields)


# === _is_agent_muted ===


def test_is_agent_muted_true() -> None:
    certified_data = {"plugin": {"kanpan": {"muted": True}}}
    assert _is_agent_muted(certified_data) is True


def test_is_agent_muted_false() -> None:
    certified_data = {"plugin": {"kanpan": {"muted": False}}}
    assert _is_agent_muted(certified_data) is False


def test_is_agent_muted_missing_key() -> None:
    assert _is_agent_muted({}) is False


def test_is_agent_muted_no_kanpan_key() -> None:
    certified_data = {"plugin": {}}
    assert _is_agent_muted(certified_data) is False


def test_is_agent_muted_no_muted_key() -> None:
    certified_data = {"plugin": {"kanpan": {}}}
    assert _is_agent_muted(certified_data) is False


# === _run_data_sources_parallel ===


class _MockDataSource:
    def __init__(
        self, name: str, result: dict[AgentName, dict[str, FieldValue]], errors: list[str] | None = None
    ) -> None:
        self._name = name
        self._result = result
        self._errors: list[str] = errors or []

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_remote(self) -> bool:
        return False

    @property
    def columns(self) -> dict[str, str]:
        return {}

    @property
    def field_types(self) -> dict[str, type[FieldValue]]:
        return {}

    def compute(
        self,
        agents: object,
        cached_fields: object,
        mngr_ctx: object,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], list[str]]:
        return self._result, self._errors


class _FailingDataSource:
    @property
    def name(self) -> str:
        return "failing"

    @property
    def is_remote(self) -> bool:
        return False

    @property
    def columns(self) -> dict[str, str]:
        return {}

    @property
    def field_types(self) -> dict[str, type[FieldValue]]:
        return {}

    def compute(
        self,
        agents: object,
        cached_fields: object,
        mngr_ctx: object,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], list[str]]:
        raise RuntimeError("data source crashed")


def test_run_data_sources_parallel_empty() -> None:
    results, errors = _run_data_sources_parallel([], (), {}, cast(MngrContext, SimpleNamespace()))
    assert results == {}
    assert errors == []


def test_run_data_sources_parallel_single_source() -> None:
    agent = AgentName("agent-1")
    pr = _make_pr()
    source = _MockDataSource("github", {agent: {"pr": pr}})
    results, errors = _run_data_sources_parallel([source], (), {}, cast(MngrContext, SimpleNamespace()))
    assert "github" in results
    assert agent in results["github"]
    assert errors == []


def test_run_data_sources_parallel_source_with_errors() -> None:
    source = _MockDataSource("github", {}, errors=["some error"])
    results, errors = _run_data_sources_parallel([source], (), {}, cast(MngrContext, SimpleNamespace()))
    assert "some error" in errors


def test_run_data_sources_parallel_source_raises_exception() -> None:
    source = _FailingDataSource()
    results, errors = _run_data_sources_parallel([source], (), {}, cast(MngrContext, SimpleNamespace()))
    assert any("failing" in e and "failed" in e for e in errors)


def test_run_data_sources_parallel_multiple_sources() -> None:
    a1 = AgentName("a1")
    pr = _make_pr()
    ci = CiField(status=CiStatus.PASSING)
    s1 = _MockDataSource("github", {a1: {"pr": pr}})
    s2 = _MockDataSource("git_info", {a1: {"ci": ci}})
    results, errors = _run_data_sources_parallel([s1, s2], (), {}, cast(MngrContext, SimpleNamespace()))
    assert "github" in results
    assert "git_info" in results
    assert errors == []


# === _get_local_work_dir ===


def test_get_local_work_dir_local_agent_with_existing_dir(tmp_path: Path) -> None:
    agent = make_agent_details(name="agent-1", provider_name="local", work_dir=tmp_path)
    result = _get_local_work_dir(agent)
    assert result == tmp_path


def test_get_local_work_dir_local_agent_nonexistent_dir() -> None:
    agent = make_agent_details(
        name="agent-1",
        provider_name="local",
        work_dir=Path("/nonexistent/path/that/does/not/exist"),
    )
    result = _get_local_work_dir(agent)
    assert result is None


def test_get_local_work_dir_remote_agent() -> None:
    agent = make_agent_details(name="agent-1", provider_name="modal")
    result = _get_local_work_dir(agent)
    assert result is None


# === collect_data_sources ===


def _make_mock_mngr_ctx(config: KanpanPluginConfig, sources: list[object]) -> MngrContext:
    """Build a minimal mock MngrContext for collect_data_sources tests."""
    hook = SimpleNamespace(kanpan_data_sources=lambda **kw: [sources])
    pm = SimpleNamespace(hook=hook)
    return cast(
        MngrContext,
        SimpleNamespace(
            get_plugin_config=lambda name, cls: config,
            pm=pm,
        ),
    )


def test_collect_data_sources_returns_all_enabled() -> None:
    source = _MockDataSource("github", {})
    ctx = _make_mock_mngr_ctx(KanpanPluginConfig(), [source])
    sources = collect_data_sources(ctx)
    assert any(s.name == "github" for s in sources)


def test_collect_data_sources_excludes_disabled() -> None:
    source = _MockDataSource("github", {})
    config = KanpanPluginConfig(data_sources={"github": DataSourceConfig(enabled=False)})
    ctx = _make_mock_mngr_ctx(config, [source])
    sources = collect_data_sources(ctx)
    assert not any(s.name == "github" for s in sources)


def test_collect_data_sources_includes_enabled_source() -> None:
    source = _MockDataSource("git_info", {})
    config = KanpanPluginConfig(data_sources={"git_info": DataSourceConfig(enabled=True)})
    ctx = _make_mock_mngr_ctx(config, [source])
    sources = collect_data_sources(ctx)
    assert any(s.name == "git_info" for s in sources)


def test_collect_data_sources_skips_none_results() -> None:
    hook = SimpleNamespace(kanpan_data_sources=lambda **kw: [None])
    pm = SimpleNamespace(hook=hook)
    ctx = cast(
        MngrContext,
        SimpleNamespace(
            get_plugin_config=lambda name, cls: KanpanPluginConfig(),
            pm=pm,
        ),
    )
    sources = collect_data_sources(ctx)
    assert sources == []


def test_collect_data_sources_dict_config_disabled() -> None:
    """When source_config is a raw dict with enabled=False, source should be excluded."""
    source = _MockDataSource("github", {})
    hook = SimpleNamespace(kanpan_data_sources=lambda **kw: [[source]])
    pm = SimpleNamespace(hook=hook)
    ctx = cast(
        MngrContext,
        SimpleNamespace(
            get_plugin_config=lambda name, cls: SimpleNamespace(
                data_sources={"github": {"enabled": False}},
            ),
            pm=pm,
        ),
    )
    sources = collect_data_sources(ctx)
    assert not any(s.name == "github" for s in sources)


def test_collect_data_sources_dict_config_enabled() -> None:
    """When source_config is a raw dict with enabled=True, source should be included."""
    source = _MockDataSource("github", {})
    hook = SimpleNamespace(kanpan_data_sources=lambda **kw: [[source]])
    pm = SimpleNamespace(hook=hook)
    ctx = cast(
        MngrContext,
        SimpleNamespace(
            get_plugin_config=lambda name, cls: SimpleNamespace(
                data_sources={"github": {"enabled": True}},
            ),
            pm=pm,
        ),
    )
    sources = collect_data_sources(ctx)
    assert any(s.name == "github" for s in sources)


# === plugin.kanpan_data_sources ===


def _make_plugin_mngr_ctx(config: KanpanPluginConfig) -> MngrContext:
    return cast(MngrContext, SimpleNamespace(get_plugin_config=lambda name, cls: config))


def test_plugin_kanpan_data_sources_default() -> None:
    ctx = _make_plugin_mngr_ctx(KanpanPluginConfig())
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    names = [s.name for s in result]
    assert "repo_paths" in names
    assert "git_info" in names
    assert "github" in names


def test_plugin_kanpan_data_sources_with_shell_commands() -> None:
    config = KanpanPluginConfig(
        shell_commands={"my_cmd": ShellCommandSourceConfig(name="My Command", header="CMD", command="echo hi")}
    )
    ctx = _make_plugin_mngr_ctx(config)
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    names = [s.name for s in result]
    assert "shell_my_cmd" in names


def test_plugin_kanpan_data_sources_github_config_as_dict() -> None:
    # GitHub config as a raw dict (tests the isinstance dict branch in plugin.py)
    ctx = cast(
        MngrContext,
        SimpleNamespace(
            get_plugin_config=lambda name, cls: SimpleNamespace(
                data_sources={"github": {"enabled": True, "pr": True}},
                shell_commands={},
                columns={},
            )
        ),
    )
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None


def test_plugin_kanpan_data_sources_shell_config_as_dict() -> None:
    # Shell command config as a raw dict (tests the isinstance dict branch for shell)
    ctx = cast(
        MngrContext,
        SimpleNamespace(
            get_plugin_config=lambda name, cls: SimpleNamespace(
                data_sources={},
                shell_commands={"my_cmd": {"name": "My Command", "header": "CMD", "command": "echo hi"}},
                columns={},
            )
        ),
    )
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    names = [s.name for s in result]
    assert "shell_my_cmd" in names


# === fetch_board_snapshot ===


def _make_list_result(agents: list[AgentDetails]) -> ListResult:
    """Build a ListResult with the given agents and no errors."""
    result = ListResult()
    result.agents = agents
    return result


def _make_fetch_ctx() -> MngrContext:
    """Build a minimal MngrContext for fetch_board_snapshot tests."""
    return cast(MngrContext, SimpleNamespace())


def test_fetch_board_snapshot_empty_agents() -> None:
    """Board snapshot with no agents returns empty entries."""
    ctx = _make_fetch_ctx()
    list_result = _make_list_result([])
    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=list_result):
        with patch("imbue.mngr_kanpan.fetcher._load_muted_agents", return_value=set()):
            result = fetch_board_snapshot(ctx, [], {})
    assert isinstance(result.snapshot, BoardSnapshot)
    assert result.snapshot.entries == ()
    assert result.snapshot.errors == ()
    assert result.cached_fields == {}


def test_fetch_board_snapshot_single_agent_no_data_sources() -> None:
    """Single agent with no data sources gets a STILL_COOKING section."""
    agent = make_agent_details(name="agent-1")
    ctx = _make_fetch_ctx()
    list_result = _make_list_result([agent])
    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=list_result):
        with patch("imbue.mngr_kanpan.fetcher._load_muted_agents", return_value=set()):
            result = fetch_board_snapshot(ctx, [], {})
    assert len(result.snapshot.entries) == 1
    entry = result.snapshot.entries[0]
    assert isinstance(entry, AgentBoardEntry)
    assert entry.name == agent.name
    assert entry.section == BoardSection.STILL_COOKING
    assert entry.is_muted is False
    assert FIELD_MUTED in entry.fields
    muted_field = entry.fields[FIELD_MUTED]
    assert isinstance(muted_field, BoolField)
    assert muted_field.value is False


def test_fetch_board_snapshot_muted_agent() -> None:
    """Muted agents end up in the MUTED section."""
    agent = make_agent_details(name="muted-agent")
    ctx = _make_fetch_ctx()
    list_result = _make_list_result([agent])
    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=list_result):
        with patch("imbue.mngr_kanpan.fetcher._load_muted_agents", return_value={agent.name}):
            result = fetch_board_snapshot(ctx, [], {})
    assert len(result.snapshot.entries) == 1
    entry = result.snapshot.entries[0]
    assert entry.is_muted is True
    assert entry.section == BoardSection.MUTED
    muted_field = entry.fields[FIELD_MUTED]
    assert isinstance(muted_field, BoolField)
    assert muted_field.value is True


def test_fetch_board_snapshot_merges_fields_from_data_sources() -> None:
    """Fields from data sources are merged and available on entries."""
    agent = make_agent_details(name="agent-1")
    ctx = _make_fetch_ctx()
    pr_field = PrField(
        number=5,
        title="Test PR",
        state=PrState.OPEN,
        url="https://github.com/org/repo/pull/5",
        head_branch="branch",
        is_draft=False,
    )
    source = _MockDataSource("github", {agent.name: {FIELD_PR: pr_field}})
    list_result = _make_list_result([agent])
    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=list_result):
        with patch("imbue.mngr_kanpan.fetcher._load_muted_agents", return_value=set()):
            result = fetch_board_snapshot(ctx, [source], {})
    assert len(result.snapshot.entries) == 1
    entry = result.snapshot.entries[0]
    assert FIELD_PR in entry.fields
    pr_field_result = entry.fields[FIELD_PR]
    assert isinstance(pr_field_result, PrField)
    assert pr_field_result.number == 5
    assert entry.section == BoardSection.PR_BEING_REVIEWED


def test_fetch_board_snapshot_errors_from_list_agents() -> None:
    """Errors from list_agents are captured in the snapshot."""
    ctx = _make_fetch_ctx()
    list_result = _make_list_result([])
    error = ErrorInfo(exception_type="SomeError", message="provider failed")
    list_result.errors = [error]
    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=list_result):
        with patch("imbue.mngr_kanpan.fetcher._load_muted_agents", return_value=set()):
            result = fetch_board_snapshot(ctx, [], {})
    assert any("SomeError" in e for e in result.snapshot.errors)


def test_fetch_board_snapshot_cached_fields_updated() -> None:
    """The cached_fields in the result reflect newly computed fields."""
    agent = make_agent_details(name="agent-1")
    ctx = _make_fetch_ctx()
    pr_field = PrField(
        number=1,
        title="PR",
        state=PrState.MERGED,
        url="https://github.com/org/repo/pull/1",
        head_branch="branch",
        is_draft=False,
    )
    source = _MockDataSource("github", {agent.name: {FIELD_PR: pr_field}})
    list_result = _make_list_result([agent])
    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=list_result):
        with patch("imbue.mngr_kanpan.fetcher._load_muted_agents", return_value=set()):
            result = fetch_board_snapshot(ctx, [source], {})
    assert agent.name in result.cached_fields
    assert FIELD_PR in result.cached_fields[agent.name]


# === fetch_local_snapshot ===


class _RemoteDataSource(_MockDataSource):
    @property
    def is_remote(self) -> bool:
        return True


def test_fetch_local_snapshot_skips_remote_sources() -> None:
    """fetch_local_snapshot only runs non-remote data sources."""
    agent = make_agent_details(name="agent-1")
    pr_field = PrField(
        number=1,
        title="PR",
        state=PrState.OPEN,
        url="https://github.com/org/repo/pull/1",
        head_branch="b",
        is_draft=False,
    )
    local_source = _MockDataSource("local_src", {agent.name: {FIELD_PR: pr_field}})
    remote_source = _RemoteDataSource("remote_src", {agent.name: {FIELD_CI: CiField(status=CiStatus.PASSING)}})

    ctx = _make_fetch_ctx()
    list_result = _make_list_result([agent])
    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=list_result):
        with patch("imbue.mngr_kanpan.fetcher._load_muted_agents", return_value=set()):
            result = fetch_local_snapshot(ctx, [local_source, remote_source], {})
    entry = result.snapshot.entries[0]
    assert FIELD_PR in entry.fields
    assert FIELD_CI not in entry.fields


# === _load_muted_agents ===


def test_load_muted_agents_returns_muted_names() -> None:
    """_load_muted_agents returns names of agents whose certified_data marks them muted."""
    agent_ref_muted = SimpleNamespace(
        agent_name=AgentName("muted-agent"),
        certified_data={"plugin": {"kanpan": {"muted": True}}},
    )
    agent_ref_not_muted = SimpleNamespace(
        agent_name=AgentName("active-agent"),
        certified_data={"plugin": {"kanpan": {"muted": False}}},
    )
    agents_by_host = {
        "host-1": [agent_ref_muted, agent_ref_not_muted],
    }
    ctx = cast(MngrContext, SimpleNamespace())
    with patch(
        "imbue.mngr_kanpan.fetcher.discover_hosts_and_agents",
        return_value=(agents_by_host, {}),
    ):
        muted = _load_muted_agents(ctx)
    assert AgentName("muted-agent") in muted
    assert AgentName("active-agent") not in muted


def test_load_muted_agents_returns_empty_on_exception() -> None:
    """_load_muted_agents returns empty set when discover_hosts_and_agents raises."""
    ctx = cast(MngrContext, SimpleNamespace())
    with patch(
        "imbue.mngr_kanpan.fetcher.discover_hosts_and_agents",
        side_effect=RuntimeError("network down"),
    ):
        muted = _load_muted_agents(ctx)
    assert muted == set()


# === save_field_cache / load_field_cache ===


def _make_cache_ctx(profile_dir: Path) -> MngrContext:
    return cast(MngrContext, SimpleNamespace(profile_dir=profile_dir))


def _make_mock_data_source(field_key: str, field_type: type[FieldValue]) -> KanpanDataSource:
    return cast(
        KanpanDataSource,
        SimpleNamespace(
            field_types={field_key: field_type},
        ),
    )


def test_save_field_cache_writes_json(tmp_path: Path) -> None:
    """save_field_cache creates a JSON file under profile_dir/kanpan/."""
    ctx = _make_cache_ctx(tmp_path)
    agent_name = AgentName("agent-1")
    cached: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {"pr_count": StringField(value="3")},
    }
    data_sources = [_make_mock_data_source("pr_count", StringField)]
    save_field_cache(ctx, cached, data_sources)
    cache_file = tmp_path / "kanpan" / "field_cache.json"
    assert cache_file.exists()


def test_load_field_cache_returns_empty_when_no_file(tmp_path: Path) -> None:
    """load_field_cache returns empty dict when the cache file does not exist."""
    ctx = _make_cache_ctx(tmp_path)
    result = load_field_cache(ctx, [])
    assert result == {}


def test_save_load_field_cache_roundtrip(tmp_path: Path) -> None:
    """Fields saved with save_field_cache are correctly restored by load_field_cache."""
    ctx = _make_cache_ctx(tmp_path)
    agent_name = AgentName("agent-1")
    original: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {"status": StringField(value="hello")},
    }
    data_sources = [_make_mock_data_source("status", StringField)]
    save_field_cache(ctx, original, data_sources)
    loaded = load_field_cache(ctx, data_sources)
    assert agent_name in loaded
    field = loaded[agent_name]["status"]
    assert isinstance(field, StringField)
    assert field.value == "hello"


def test_load_field_cache_returns_empty_on_corrupt_json(tmp_path: Path) -> None:
    """load_field_cache returns empty dict when the cache file contains invalid JSON."""
    cache_dir = tmp_path / "kanpan"
    cache_dir.mkdir(parents=True)
    (cache_dir / "field_cache.json").write_text("not valid json {{{")
    ctx = _make_cache_ctx(tmp_path)
    result = load_field_cache(ctx, [])
    assert result == {}


def test_load_field_cache_skips_unknown_types(tmp_path: Path) -> None:
    """load_field_cache skips field entries whose type is not in the type registry."""
    ctx = _make_cache_ctx(tmp_path)
    agent_name = AgentName("agent-1")
    original: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {"status": StringField(value="hello")},
    }
    data_sources = [_make_mock_data_source("status", StringField)]
    save_field_cache(ctx, original, data_sources)
    # Load with no data sources (empty type registry) -- field should be skipped
    loaded = load_field_cache(ctx, [])
    assert loaded == {}


def test_save_field_cache_swallows_errors(tmp_path: Path) -> None:
    """save_field_cache does not raise even when the write fails."""
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o444)
    ctx = _make_cache_ctx(readonly_dir / "subdir_that_cannot_exist")
    try:
        save_field_cache(ctx, {}, [])
    finally:
        readonly_dir.chmod(0o755)
