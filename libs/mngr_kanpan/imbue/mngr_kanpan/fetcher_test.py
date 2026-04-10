from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import CiField
from imbue.mngr_kanpan.data_source import CiStatus
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_MUTED
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanFieldTypeError
from imbue.mngr_kanpan.data_source import PrField
from imbue.mngr_kanpan.data_source import PrState
from imbue.mngr_kanpan.data_source import StringField
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import DataSourceConfig
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.data_types import ShellCommandSourceConfig
from imbue.mngr_kanpan.fetcher import _is_agent_muted
from imbue.mngr_kanpan.fetcher import _parse_github_repo_path
from imbue.mngr_kanpan.fetcher import _run_data_sources_parallel
from imbue.mngr_kanpan.fetcher import collect_data_sources
from imbue.mngr_kanpan.fetcher import compute_section
from imbue.mngr_kanpan.fetcher import repo_path_from_labels
from imbue.mngr_kanpan.plugin import kanpan_data_sources

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
    results, errors = _run_data_sources_parallel([], (), {}, cast(MngrContext, MagicMock()))
    assert results == {}
    assert errors == []


def test_run_data_sources_parallel_single_source() -> None:
    agent = AgentName("agent-1")
    pr = _make_pr()
    source = _MockDataSource("github", {agent: {"pr": pr}})
    results, errors = _run_data_sources_parallel([source], (), {}, cast(MngrContext, MagicMock()))
    assert "github" in results
    assert agent in results["github"]
    assert errors == []


def test_run_data_sources_parallel_source_with_errors() -> None:
    source = _MockDataSource("github", {}, errors=["some error"])
    results, errors = _run_data_sources_parallel([source], (), {}, cast(MngrContext, MagicMock()))
    assert "some error" in errors


def test_run_data_sources_parallel_source_raises_exception() -> None:
    source = _FailingDataSource()
    results, errors = _run_data_sources_parallel([source], (), {}, cast(MngrContext, MagicMock()))
    assert any("failing" in e and "failed" in e for e in errors)


def test_run_data_sources_parallel_multiple_sources() -> None:
    a1 = AgentName("a1")
    pr = _make_pr()
    ci = CiField(status=CiStatus.PASSING)
    s1 = _MockDataSource("github", {a1: {"pr": pr}})
    s2 = _MockDataSource("git_info", {a1: {"ci": ci}})
    results, errors = _run_data_sources_parallel([s1, s2], (), {}, cast(MngrContext, MagicMock()))
    assert "github" in results
    assert "git_info" in results
    assert errors == []


# === collect_data_sources ===


def _make_mock_mngr_ctx(config: KanpanPluginConfig, sources: list[object]) -> MngrContext:
    """Build a minimal mock MngrContext for collect_data_sources tests."""
    hook = MagicMock()
    hook.kanpan_data_sources.return_value = [sources]
    pm = MagicMock()
    pm.hook = hook
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
    hook = MagicMock()
    hook.kanpan_data_sources.return_value = [None]
    pm = MagicMock()
    pm.hook = hook
    ctx = cast(
        MngrContext,
        SimpleNamespace(
            get_plugin_config=lambda name, cls: KanpanPluginConfig(),
            pm=pm,
        ),
    )
    sources = collect_data_sources(ctx)
    assert sources == []


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
            )
        ),
    )
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    names = [s.name for s in result]
    assert "shell_my_cmd" in names
