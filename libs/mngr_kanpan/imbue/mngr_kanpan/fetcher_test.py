from pathlib import Path
from types import SimpleNamespace

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_MUTED
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanDataSource
from imbue.mngr_kanpan.data_source import KanpanFieldTypeError
from imbue.mngr_kanpan.data_source import StringField
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_sources.repo_paths import _parse_github_repo_path
from imbue.mngr_kanpan.data_sources.repo_paths import repo_path_from_labels
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.fetcher import _get_local_work_dir
from imbue.mngr_kanpan.fetcher import _is_agent_muted
from imbue.mngr_kanpan.fetcher import _run_data_sources_parallel
from imbue.mngr_kanpan.fetcher import collect_data_sources
from imbue.mngr_kanpan.fetcher import compute_section
from imbue.mngr_kanpan.fetcher import load_field_cache
from imbue.mngr_kanpan.fetcher import save_field_cache
from imbue.mngr_kanpan.plugin import _is_source_enabled
from imbue.mngr_kanpan.plugin import kanpan_data_sources
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_mngr_ctx
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_config
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_profile_dir
from imbue.mngr_kanpan.testing import make_pr_field

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
    fields: dict[str, FieldValue] = {FIELD_PR: make_pr_field(is_draft=True)}
    assert compute_section(fields) == BoardSection.PR_DRAFT


def test_compute_section_merged_pr() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: make_pr_field(state=PrState.MERGED)}
    assert compute_section(fields) == BoardSection.PR_MERGED


def test_compute_section_closed_pr() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: make_pr_field(state=PrState.CLOSED)}
    assert compute_section(fields) == BoardSection.PR_CLOSED


def test_compute_section_open_pr_no_ci() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: make_pr_field()}
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_open_pr_ci_failing() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: make_pr_field(),
        FIELD_CI: CiField(status=CiStatus.FAILING),
    }
    assert compute_section(fields) == BoardSection.PRS_FAILED


def test_compute_section_open_pr_ci_passing() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: make_pr_field(),
        FIELD_CI: CiField(status=CiStatus.PASSING),
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_open_pr_ci_pending() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: make_pr_field(),
        FIELD_CI: CiField(status=CiStatus.PENDING),
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_open_pr_ci_unknown() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: make_pr_field(),
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
        FIELD_PR: make_pr_field(),
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
    results, errors = _run_data_sources_parallel([], (), {}, make_mngr_ctx())
    assert results == {}
    assert errors == []


def test_run_data_sources_parallel_single_source() -> None:
    agent = AgentName("agent-1")
    pr = make_pr_field()
    source = _MockDataSource("github", {agent: {"pr": pr}})
    results, errors = _run_data_sources_parallel([source], (), {}, make_mngr_ctx())
    assert "github" in results
    assert agent in results["github"]
    assert errors == []


def test_run_data_sources_parallel_source_with_errors() -> None:
    source = _MockDataSource("github", {}, errors=["some error"])
    results, errors = _run_data_sources_parallel([source], (), {}, make_mngr_ctx())
    assert "some error" in errors


def test_run_data_sources_parallel_source_raises_exception() -> None:
    source = _FailingDataSource()
    results, errors = _run_data_sources_parallel([source], (), {}, make_mngr_ctx())
    assert any("failing" in e and "failed" in e for e in errors)


def test_run_data_sources_parallel_multiple_sources() -> None:
    a1 = AgentName("a1")
    pr = make_pr_field()
    ci = CiField(status=CiStatus.PASSING)
    s1 = _MockDataSource("github", {a1: {"pr": pr}})
    s2 = _MockDataSource("git_info", {a1: {"ci": ci}})
    results, errors = _run_data_sources_parallel([s1, s2], (), {}, make_mngr_ctx())
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
    return SimpleNamespace(  # ty: ignore[invalid-return-type]
        get_plugin_config=lambda name, cls: config,
        pm=pm,
    )


def test_collect_data_sources_returns_all_enabled() -> None:
    source = _MockDataSource("github", {})
    ctx = _make_mock_mngr_ctx(KanpanPluginConfig(), [source])
    sources = collect_data_sources(ctx)
    assert any(s.name == "github" for s in sources)


def test_collect_data_sources_skips_none_results() -> None:
    hook = SimpleNamespace(kanpan_data_sources=lambda **kw: [None])
    pm = SimpleNamespace(hook=hook)
    ctx: MngrContext = SimpleNamespace(  # ty: ignore[invalid-assignment]
        get_plugin_config=lambda name, cls: KanpanPluginConfig(),
        pm=pm,
    )
    sources = collect_data_sources(ctx)
    assert sources == []


# === plugin._is_source_enabled / kanpan_data_sources ===


def test_plugin_kanpan_data_sources_default() -> None:
    ctx = make_mngr_ctx_with_config(KanpanPluginConfig())
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    names = [s.name for s in result]
    assert "repo_paths" in names
    assert "git_info" in names
    assert "github" in names


def test_is_source_enabled_default() -> None:
    config = KanpanPluginConfig()
    assert _is_source_enabled(config, "github") is True


def test_is_source_enabled_dict_disabled() -> None:
    config = KanpanPluginConfig(data_sources={"github": {"enabled": False}})
    assert _is_source_enabled(config, "github") is False


def test_is_source_enabled_dict_enabled() -> None:
    config = KanpanPluginConfig(data_sources={"github": {"enabled": True}})
    assert _is_source_enabled(config, "github") is True


def test_is_source_enabled_dict_missing_enabled_defaults_true() -> None:
    """A raw dict without an 'enabled' key defaults to True (source-specific fields only)."""
    config = KanpanPluginConfig(data_sources={"github": {"pr": True}})
    assert _is_source_enabled(config, "github") is True


def test_plugin_excludes_disabled_github() -> None:
    config = KanpanPluginConfig(data_sources={"github": {"enabled": False}})
    ctx = make_mngr_ctx_with_config(config)
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    assert not any(s.name == "github" for s in result)


def test_plugin_excludes_disabled_repo_paths() -> None:
    config = KanpanPluginConfig(data_sources={"repo_paths": {"enabled": False}})
    ctx = make_mngr_ctx_with_config(config)
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    assert not any(s.name == "repo_paths" for s in result)


def test_plugin_kanpan_data_sources_with_shell_commands() -> None:
    config = KanpanPluginConfig(
        shell_commands={"my_cmd": {"name": "My Command", "header": "CMD", "command": "echo hi"}}
    )
    ctx = make_mngr_ctx_with_config(config)
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    names = [s.name for s in result]
    assert "shell_my_cmd" in names


def test_plugin_kanpan_data_sources_with_github_config() -> None:
    config = KanpanPluginConfig(data_sources={"github": {"pr": True}})
    ctx = make_mngr_ctx_with_config(config)
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    assert any(s.name == "github" for s in result)


def test_plugin_kanpan_data_sources_from_loader_path() -> None:
    """Regression: loader uses model_construct, so configs may reach the plugin via that path."""
    config = KanpanPluginConfig.model_construct(
        data_sources={"github": {"enabled": False}},
        shell_commands={},
        columns={},
    )
    ctx = make_mngr_ctx_with_config(config)
    result = kanpan_data_sources(mngr_ctx=ctx)
    assert result is not None
    assert not any(s.name == "github" for s in result)


# === save_field_cache / load_field_cache ===


def _make_mock_data_source(field_key: str, field_type: type[FieldValue]) -> KanpanDataSource:
    return SimpleNamespace(  # ty: ignore[invalid-return-type]
        field_types={field_key: field_type},
    )


def test_save_field_cache_writes_json(tmp_path: Path) -> None:
    """save_field_cache creates a JSON file under profile_dir/kanpan/."""
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    agent_name = AgentName("agent-1")
    cached: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {"pr_count": StringField(value="3")},
    }
    save_field_cache(ctx, cached)
    cache_file = tmp_path / "kanpan" / "field_cache.json"
    assert cache_file.exists()


def test_load_field_cache_returns_empty_when_no_file(tmp_path: Path) -> None:
    """load_field_cache returns empty dict when the cache file does not exist."""
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    result = load_field_cache(ctx, [])
    assert result == {}


def test_save_load_field_cache_roundtrip(tmp_path: Path) -> None:
    """Fields saved with save_field_cache are correctly restored by load_field_cache."""
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    agent_name = AgentName("agent-1")
    original: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {"status": StringField(value="hello")},
    }
    data_sources = [_make_mock_data_source("status", StringField)]
    save_field_cache(ctx, original)
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
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    result = load_field_cache(ctx, [])
    assert result == {}


def test_load_field_cache_skips_unknown_types(tmp_path: Path) -> None:
    """load_field_cache skips field entries whose type is not in the type registry."""
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    agent_name = AgentName("agent-1")
    original: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {"status": StringField(value="hello")},
    }
    save_field_cache(ctx, original)
    # Load with no data sources (empty type registry) -- field should be skipped
    loaded = load_field_cache(ctx, [])
    assert loaded == {}


def test_save_field_cache_swallows_errors(tmp_path: Path) -> None:
    """save_field_cache does not raise even when the write fails."""
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o444)
    ctx = make_mngr_ctx_with_profile_dir(readonly_dir / "subdir_that_cannot_exist")
    try:
        save_field_cache(ctx, {})
    finally:
        readonly_dir.chmod(0o755)
