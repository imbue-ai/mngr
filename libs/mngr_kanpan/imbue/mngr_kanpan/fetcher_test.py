import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

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
from imbue.mngr_kanpan.data_sources.github import CreatePrUrlField
from imbue.mngr_kanpan.data_sources.github import GitHubDataSource
from imbue.mngr_kanpan.data_sources.github import GitHubDataSourceConfig
from imbue.mngr_kanpan.data_sources.github import PrFetchFailedField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_sources.repo_paths import _parse_github_repo_path
from imbue.mngr_kanpan.data_sources.repo_paths import repo_path_from_labels
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.fetcher import _get_local_work_dir
from imbue.mngr_kanpan.fetcher import _run_data_sources_parallel
from imbue.mngr_kanpan.fetcher import collect_data_sources
from imbue.mngr_kanpan.fetcher import compute_section
from imbue.mngr_kanpan.fetcher import load_field_cache
from imbue.mngr_kanpan.fetcher import save_field_cache
from imbue.mngr_kanpan.mock_data_source_test import make_fake_data_source
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
    fields: dict[str, FieldValue] = {
        FIELD_MUTED: BoolField(value=True, created=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
    }
    assert compute_section(fields) == BoardSection.MUTED


def test_compute_section_muted_false() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_MUTED: BoolField(value=False, created=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    }
    assert compute_section(fields) == BoardSection.STILL_COOKING


def test_compute_section_no_pr() -> None:
    fields: dict[str, FieldValue] = {}
    assert compute_section(fields) == BoardSection.STILL_COOKING


def test_compute_section_draft_pr() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: make_pr_field(is_draft=True, created=datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc))
    }
    assert compute_section(fields) == BoardSection.PR_DRAFT


def test_compute_section_merged_pr() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: make_pr_field(state=PrState.MERGED, created=datetime(2026, 1, 1, 0, 0, 4, tzinfo=timezone.utc))
    }
    assert compute_section(fields) == BoardSection.PR_MERGED


def test_compute_section_closed_pr() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: make_pr_field(state=PrState.CLOSED, created=datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc))
    }
    assert compute_section(fields) == BoardSection.PR_CLOSED


def test_compute_section_open_pr_no_ci() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: make_pr_field(created=datetime(2026, 1, 1, 0, 0, 6, tzinfo=timezone.utc))
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


@pytest.mark.parametrize(
    "ci_status",
    [CiStatus.SUCCESS, CiStatus.FAILURE, CiStatus.PENDING, CiStatus.UNKNOWN],
)
def test_compute_section_open_pr_ignores_ci(ci_status: CiStatus) -> None:
    # Regression: compute_section no longer dispatches on FIELD_CI for open PRs.
    # An open, non-draft PR is always PR_BEING_REVIEWED regardless of CI status;
    # PRS_FAILED is reserved for the "could not load PR data" case (see
    # test_compute_section_pr_fetch_failed below).
    fields: dict[str, FieldValue] = {
        FIELD_PR: make_pr_field(created=datetime(2026, 1, 1, 0, 0, 7, tzinfo=timezone.utc)),
        FIELD_CI: CiField(status=ci_status, created=datetime(2026, 1, 1, 0, 0, 8, tzinfo=timezone.utc)),
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_pr_fetch_failed() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: PrFetchFailedField(repo="org/repo", created=datetime(2026, 1, 1, 0, 0, 9, tzinfo=timezone.utc))
    }
    assert compute_section(fields) == BoardSection.PRS_FAILED


def test_compute_section_wrong_muted_type() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_MUTED: StringField(value="yes", created=datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc))
    }
    with pytest.raises(KanpanFieldTypeError, match="Expected BoolField"):
        compute_section(fields)


def test_compute_section_wrong_pr_type() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: StringField(value="oops", created=datetime(2026, 1, 1, 0, 0, 11, tzinfo=timezone.utc))
    }
    with pytest.raises(KanpanFieldTypeError, match="Expected PrField"):
        compute_section(fields)


# === _run_data_sources_parallel ===


def test_run_data_sources_parallel_empty() -> None:
    results, errors = _run_data_sources_parallel([], (), {}, make_mngr_ctx())
    assert results == {}
    assert errors == []


def test_run_data_sources_parallel_single_source() -> None:
    agent = AgentName("agent-1")
    pr = make_pr_field(created=datetime(2026, 1, 1, 0, 0, 12, tzinfo=timezone.utc))
    source = make_fake_data_source("github", {agent: {FIELD_PR: pr}})
    results, errors = _run_data_sources_parallel([source], (), {}, make_mngr_ctx())
    assert errors == []
    # The exact field object must round-trip through the parallel runner; a bug
    # that returned {agent: {}} or dropped/replaced the value would fail here.
    assert results["github"][agent][FIELD_PR] == pr


def test_run_data_sources_parallel_source_with_errors() -> None:
    source = make_fake_data_source("github", {}, errors=["some error"])
    results, errors = _run_data_sources_parallel([source], (), {}, make_mngr_ctx())
    assert "some error" in errors


def test_run_data_sources_parallel_source_raises_exception() -> None:
    source = make_fake_data_source("failing", raises=RuntimeError("data source crashed"))
    results, errors = _run_data_sources_parallel([source], (), {}, make_mngr_ctx())
    assert "failing" not in results
    assert any("failing" in e and "failed" in e for e in errors)


def test_run_data_sources_parallel_multiple_sources() -> None:
    a1 = AgentName("a1")
    pr = make_pr_field(created=datetime(2026, 1, 1, 0, 0, 13, tzinfo=timezone.utc))
    ci = CiField(status=CiStatus.SUCCESS, created=datetime(2026, 1, 1, 0, 0, 14, tzinfo=timezone.utc))
    s1 = make_fake_data_source("github", {a1: {FIELD_PR: pr}})
    s2 = make_fake_data_source("git_info", {a1: {FIELD_CI: ci}})
    results, errors = _run_data_sources_parallel([s1, s2], (), {}, make_mngr_ctx())
    assert errors == []
    # Each source's fields must come back under that source's own name (no
    # crossed wires) with the exact field objects intact.
    assert results["github"][a1][FIELD_PR] == pr
    assert FIELD_CI not in results["github"][a1]
    assert results["git_info"][a1][FIELD_CI] == ci
    assert FIELD_PR not in results["git_info"][a1]


def test_run_data_sources_parallel_partial_failure() -> None:
    """When one source raises and another succeeds, the good fields are still
    returned and the failure is surfaced as an error (not swallowed silently)."""
    a1 = AgentName("a1")
    pr = make_pr_field(created=datetime(2026, 1, 1, 0, 0, 23, tzinfo=timezone.utc))
    good = make_fake_data_source("github", {a1: {FIELD_PR: pr}})
    bad = make_fake_data_source("git_info", raises=RuntimeError("boom"))
    results, errors = _run_data_sources_parallel([good, bad], (), {}, make_mngr_ctx())
    assert results["github"][a1][FIELD_PR] == pr
    assert "git_info" not in results
    assert any("git_info" in e and "failed" in e for e in errors)


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


def test_collect_data_sources_returns_all_enabled(temp_mngr_ctx: MngrContext) -> None:
    """Driving the real, registered plugin manager (via temp_mngr_ctx) returns
    exactly the built-in kanpan data sources with their default config: no
    duplicates and no unexpected extras."""
    sources = collect_data_sources(temp_mngr_ctx)
    names = sorted(s.name for s in sources)
    assert names == ["git_info", "github", "repo_paths"]


def test_collect_data_sources_skips_none_results() -> None:
    """collect_data_sources drops the None entries that hookimpls contribute when
    they opt out, while keeping every real source returned by other hookimpls.

    A real plugin manager never returns None per-source for the kanpan
    built-ins, so the None-skipping branch is driven here with an explicit hook
    fake that interleaves a None result with a populated one.
    """
    kept_a = make_fake_data_source("source_a", {})
    kept_b = make_fake_data_source("source_b", {})
    # The hook aggregates one result list per contributing hookimpl; here one
    # hookimpl opted out (None) and two contributed real sources.
    hook = SimpleNamespace(kanpan_data_sources=lambda **kw: [None, [kept_a], [kept_b]])
    pm = SimpleNamespace(hook=hook)
    ctx: MngrContext = SimpleNamespace(pm=pm)  # ty: ignore[invalid-assignment]
    sources = collect_data_sources(ctx)
    assert [s.name for s in sources] == ["source_a", "source_b"]


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
        field_types={field_key: TypeAdapter(field_type)},
    )


def test_save_field_cache_writes_json(tmp_path: Path) -> None:
    """save_field_cache writes a JSON file whose contents include the serialized
    agent key and field value (not merely that some file exists)."""
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    agent_name = AgentName("agent-1")
    cached: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {"pr_count": StringField(value="3", created=datetime(2026, 1, 1, 0, 0, 15, tzinfo=timezone.utc))},
    }
    save_field_cache(ctx, cached)
    cache_file = tmp_path / "kanpan" / "field_cache.json"
    parsed = json.loads(cache_file.read_text())
    assert str(agent_name) in parsed
    assert parsed[str(agent_name)]["pr_count"]["value"] == "3"
    assert parsed[str(agent_name)]["pr_count"]["kind"] == "string"


def test_load_field_cache_returns_empty_when_no_file(tmp_path: Path) -> None:
    """load_field_cache returns empty dict when the cache file does not exist."""
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    result = load_field_cache(ctx, [])
    assert result == {}


def test_save_load_field_cache_roundtrip(tmp_path: Path) -> None:
    """Fields saved with save_field_cache are correctly restored by load_field_cache."""
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    agent_name = AgentName("agent-1")
    created = datetime(2026, 1, 1, 0, 0, 16, tzinfo=timezone.utc)
    original: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {"status": StringField(value="hello", created=created)},
    }
    data_sources = [_make_mock_data_source("status", StringField)]
    save_field_cache(ctx, original)
    loaded = load_field_cache(ctx, data_sources)
    assert agent_name in loaded
    field = loaded[agent_name]["status"]
    assert isinstance(field, StringField)
    assert field.value == "hello"
    assert field.created == created


def test_save_load_field_cache_polymorphic_slot_roundtrip(tmp_path: Path) -> None:
    """A slot can hold any of several FieldValue subclasses (e.g. FIELD_PR can hold
    PrField, CreatePrUrlField, or PrFetchFailedField). All declared classes for a
    slot must round-trip through the cache, regardless of which one was last persisted.
    """
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    a1 = AgentName("a1")
    a2 = AgentName("a2")
    a3 = AgentName("a3")
    original: dict[AgentName, dict[str, FieldValue]] = {
        a1: {FIELD_PR: make_pr_field(number=42, created=datetime(2026, 1, 1, 0, 0, 17, tzinfo=timezone.utc))},
        a2: {
            FIELD_PR: CreatePrUrlField(
                url="https://example.com/compare", created=datetime(2026, 1, 1, 0, 0, 18, tzinfo=timezone.utc)
            )
        },
        a3: {
            FIELD_PR: PrFetchFailedField(repo="org/repo", created=datetime(2026, 1, 1, 0, 0, 19, tzinfo=timezone.utc))
        },
    }
    save_field_cache(ctx, original)

    data_sources = [GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))]
    loaded = load_field_cache(ctx, data_sources)

    assert isinstance(loaded[a1][FIELD_PR], type(original[a1][FIELD_PR]))
    assert loaded[a1][FIELD_PR] == original[a1][FIELD_PR]
    assert isinstance(loaded[a2][FIELD_PR], CreatePrUrlField)
    assert loaded[a2][FIELD_PR] == original[a2][FIELD_PR]
    assert isinstance(loaded[a3][FIELD_PR], PrFetchFailedField)
    assert loaded[a3][FIELD_PR] == original[a3][FIELD_PR]


def test_load_field_cache_returns_empty_on_corrupt_json(tmp_path: Path) -> None:
    """load_field_cache returns empty dict when the cache file contains invalid JSON."""
    cache_dir = tmp_path / "kanpan"
    cache_dir.mkdir(parents=True)
    (cache_dir / "field_cache.json").write_text("not valid json {{{")
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    result = load_field_cache(ctx, [])
    assert result == {}


def test_load_field_cache_returns_empty_on_non_utf8_bytes(tmp_path: Path) -> None:
    """A cache file with non-utf8 bytes (e.g. partial write) must not crash the TUI."""
    cache_dir = tmp_path / "kanpan"
    cache_dir.mkdir(parents=True)
    # 0xFF is not a valid utf-8 start byte
    (cache_dir / "field_cache.json").write_bytes(b"\xff\xfe\x00bad")
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    result = load_field_cache(ctx, [])
    assert result == {}


def test_load_field_cache_returns_empty_on_top_level_non_dict_json(tmp_path: Path) -> None:
    """load_field_cache returns empty dict when the cache JSON parses but isn't a dict at the top level."""
    cache_dir = tmp_path / "kanpan"
    cache_dir.mkdir(parents=True)
    (cache_dir / "field_cache.json").write_text("[]")
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    result = load_field_cache(ctx, [])
    assert result == {}


def test_load_field_cache_returns_empty_on_invalid_agent_name(tmp_path: Path) -> None:
    """load_field_cache returns empty dict when a top-level key is not a valid AgentName.

    The cache file may have been hand-edited or written by an older incompatible
    version. AgentName construction enforces SafeName's regex and would otherwise
    raise InvalidName; load_field_cache must swallow that and return {}.

    The payload here must be non-empty and validate against the supplied
    adapters -- otherwise deserialize_fields returns {} and the
    ``if agent_fields:`` guard short-circuits before AgentName(...) is
    even called, which would not exercise the swallow path.
    """
    cache_dir = tmp_path / "kanpan"
    cache_dir.mkdir(parents=True)
    pr_payload = make_pr_field(created=datetime(2026, 1, 1, 0, 0, 20, tzinfo=timezone.utc)).model_dump(mode="json")
    # 'a1/x' contains '/', which violates SafeName's regex. The PR payload
    # makes deserialize_fields return a non-empty dict so that the
    # AgentName("a1/x") constructor is actually reached.
    cache_data = {"a1/x": {FIELD_PR: pr_payload}}
    (cache_dir / "field_cache.json").write_text(json.dumps(cache_data))
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    data_sources = [GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))]
    result = load_field_cache(ctx, data_sources)
    assert result == {}


def test_load_field_cache_skips_unknown_types(tmp_path: Path) -> None:
    """load_field_cache drops cache entries whose field key is not declared by any
    data source's ``field_types`` adapter map. With no data sources passed in there
    are no adapters, so every saved field key is unknown and the result is empty.
    """
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    agent_name = AgentName("agent-1")
    original: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {
            "status": StringField(value="hello", created=datetime(2026, 1, 1, 0, 0, 21, tzinfo=timezone.utc))
        },
    }
    save_field_cache(ctx, original)
    # No data sources -> no field-key adapters, so every saved key is unknown and dropped.
    loaded = load_field_cache(ctx, [])
    assert loaded == {}


def test_load_field_cache_drops_legacy_entries_missing_created(tmp_path: Path) -> None:
    """A legacy cache entry without `created` is silently dropped on load.

    Other valid entries in the same file load normally.
    """
    ctx = make_mngr_ctx_with_profile_dir(tmp_path)
    cache_dir = tmp_path / "kanpan"
    cache_dir.mkdir(parents=True)
    # Hand-craft a cache file with one legacy entry (no created) and one fresh
    # entry. Using the post-`kind` wire format directly: per-field deserialize
    # via the StringField TypeAdapter, which rejects payloads missing required
    # fields (validation error, dropped silently).
    cache_payload = {
        "agent-legacy": {
            "status": {"kind": "string", "value": "old"},
        },
        "agent-fresh": {
            "status": {
                "kind": "string",
                "value": "new",
                "created": datetime(2026, 1, 1, 0, 0, 22, tzinfo=timezone.utc).isoformat(),
            },
        },
    }
    (cache_dir / "field_cache.json").write_text(json.dumps(cache_payload))
    data_sources = [_make_mock_data_source("status", StringField)]
    loaded = load_field_cache(ctx, data_sources)
    assert AgentName("agent-legacy") not in loaded
    fresh = loaded[AgentName("agent-fresh")]["status"]
    assert isinstance(fresh, StringField)
    assert fresh.value == "new"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses filesystem permissions, so the write would not fail",
)
def test_save_field_cache_swallows_errors(tmp_path: Path) -> None:
    """save_field_cache does not raise when the write fails, and when it fails it
    leaves no cache file behind (so the swallowed-failure path was truly taken)."""
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o555)
    cache_root = readonly_dir / "subdir_that_cannot_exist"
    ctx = make_mngr_ctx_with_profile_dir(cache_root)
    agent_name = AgentName("agent-1")
    cached: dict[AgentName, dict[str, FieldValue]] = {
        agent_name: {"status": StringField(value="x", created=datetime(2026, 1, 1, 0, 0, 24, tzinfo=timezone.utc))},
    }
    try:
        save_field_cache(ctx, cached)
        # The unwritable parent means mkdir/mkstemp fails; the error is swallowed
        # and no cache file is produced.
        assert not (cache_root / "kanpan" / "field_cache.json").exists()
    finally:
        readonly_dir.chmod(0o755)
