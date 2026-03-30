import tomllib
from pathlib import Path
from typing import Any

from imbue.minds.forwarding_server.mngr_settings import MNGR_SETTINGS_DIR_NAME
from imbue.minds.forwarding_server.mngr_settings import MNGR_SETTINGS_FILE_NAME
from imbue.minds.forwarding_server.mngr_settings import _build_events_self_filter
from imbue.minds.forwarding_server.mngr_settings import _build_list_exclude_filter
from imbue.minds.forwarding_server.mngr_settings import _merge_events_filter
from imbue.minds.forwarding_server.mngr_settings import configure_mngr_settings
from imbue.minds.forwarding_server.vendor_mngr import run_git
from imbue.minds.primitives import AgentName
from imbue.minds.testing import make_git_repo
from imbue.mngr.primitives import AgentId


def _read_settings(repo: Path) -> dict[str, Any]:
    """Read the settings.toml file as a plain dict."""
    settings_path = repo / MNGR_SETTINGS_DIR_NAME / MNGR_SETTINGS_FILE_NAME
    with open(settings_path, "rb") as f:
        return tomllib.load(f)


def test_build_list_exclude_filter_contains_mind_name() -> None:
    result = _build_list_exclude_filter(AgentName("selene"))
    assert 'labels.mind != "selene"' in result
    assert "!has(labels.mind)" in result


def test_build_events_self_filter_contains_agent_id() -> None:
    agent_id = AgentId()
    result = _build_events_self_filter(agent_id)
    assert 'agent_id != "{}"'.format(agent_id) in result
    assert 'source != "mngr/agent_states"' in result


def test_merge_events_filter_returns_new_when_no_existing() -> None:
    merged = _merge_events_filter(None, "a == b")
    assert merged == "a == b"


def test_merge_events_filter_returns_new_when_existing_is_empty() -> None:
    merged = _merge_events_filter("", "a == b")
    assert merged == "a == b"


def test_merge_events_filter_combines_with_existing() -> None:
    merged = _merge_events_filter('source != "foo"', "a == b")
    assert merged == '(source != "foo") && (a == b)'


def test_configure_mngr_settings_creates_settings_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    agent_id = AgentId()
    configure_mngr_settings(repo, AgentName("selene"), agent_id)

    settings_path = repo / MNGR_SETTINGS_DIR_NAME / MNGR_SETTINGS_FILE_NAME
    assert settings_path.exists()

    parsed = _read_settings(repo)

    # Verify [commands.list] exclude filter
    excludes = parsed["commands"]["list"]["exclude"]
    assert len(excludes) == 1
    assert 'labels.mind != "selene"' in excludes[0]

    # Verify [commands.events] filter
    events_filter = parsed["commands"]["events"]["filter"]
    assert str(agent_id) in events_filter
    assert "mngr/agent_states" in events_filter


def test_configure_mngr_settings_commits_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    agent_id = AgentId()
    configure_mngr_settings(repo, AgentName("selene"), agent_id)

    log_output = run_git(
        ["log", "--oneline", "-1"],
        cwd=repo,
        error_message="Failed to read git log",
    )
    assert "mngr settings" in log_output.lower()


def test_configure_mngr_settings_merges_with_existing_list_excludes(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)

    # Create existing settings with an exclude filter
    settings_dir = repo / MNGR_SETTINGS_DIR_NAME
    settings_dir.mkdir()
    settings_path = settings_dir / MNGR_SETTINGS_FILE_NAME
    settings_path.write_text(
        '[commands.list]\n'
        'exclude = [\'state == "STOPPED"\']\n'
    )

    agent_id = AgentId()
    configure_mngr_settings(repo, AgentName("selene"), agent_id)

    parsed = _read_settings(repo)
    excludes = list(parsed["commands"]["list"]["exclude"])
    assert len(excludes) == 2
    assert 'state == "STOPPED"' in excludes[0]
    assert 'labels.mind != "selene"' in excludes[1]


def test_configure_mngr_settings_merges_with_existing_events_filter(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)

    settings_dir = repo / MNGR_SETTINGS_DIR_NAME
    settings_dir.mkdir()
    settings_path = settings_dir / MNGR_SETTINGS_FILE_NAME
    settings_path.write_text(
        '[commands.events]\n'
        'filter = \'source != "delivery_failures"\'\n'
    )

    agent_id = AgentId()
    configure_mngr_settings(repo, AgentName("selene"), agent_id)

    parsed = _read_settings(repo)
    events_filter = parsed["commands"]["events"]["filter"]
    assert 'source != "delivery_failures"' in events_filter
    assert str(agent_id) in events_filter
    assert "&&" in events_filter


def test_configure_mngr_settings_preserves_other_settings(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)

    settings_dir = repo / MNGR_SETTINGS_DIR_NAME
    settings_dir.mkdir()
    settings_path = settings_dir / MNGR_SETTINGS_FILE_NAME
    settings_path.write_text('prefix = "custom-"\n')

    agent_id = AgentId()
    configure_mngr_settings(repo, AgentName("selene"), agent_id)

    parsed = _read_settings(repo)
    assert parsed["prefix"] == "custom-"
    assert "commands" in parsed


def test_configure_mngr_settings_handles_out_of_order_tables(tmp_path: Path) -> None:
    """Regression: tomlkit returns OutOfOrderTableProxy when [commands.*] sub-tables
    are interleaved with other sections, which lacks an .add() method."""
    repo = make_git_repo(tmp_path)

    settings_dir = repo / MNGR_SETTINGS_DIR_NAME
    settings_dir.mkdir()
    settings_path = settings_dir / MNGR_SETTINGS_FILE_NAME
    settings_path.write_text(
        '[commands.list]\n'
        'exclude = [\'state == "STOPPED"\']\n'
        '\n'
        '[other_section]\n'
        'key = "value"\n'
        '\n'
        '[commands.events]\n'
        'filter = \'source != "delivery_failures"\'\n'
    )

    agent_id = AgentId()
    configure_mngr_settings(repo, AgentName("selene"), agent_id)

    parsed = _read_settings(repo)

    # Original exclude preserved + new one added
    excludes = list(parsed["commands"]["list"]["exclude"])
    assert len(excludes) == 2
    assert 'state == "STOPPED"' in excludes[0]

    # Events filter merged with existing
    events_filter = parsed["commands"]["events"]["filter"]
    assert 'source != "delivery_failures"' in events_filter
    assert str(agent_id) in events_filter

    # Other section preserved
    assert parsed["other_section"]["key"] == "value"


def test_configure_mngr_settings_is_idempotent(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    agent_id = AgentId()

    configure_mngr_settings(repo, AgentName("selene"), agent_id)

    first_parsed = _read_settings(repo)
    first_events_filter = first_parsed["commands"]["events"]["filter"]

    # Running again should not add duplicate filters
    configure_mngr_settings(repo, AgentName("selene"), agent_id)

    parsed = _read_settings(repo)
    excludes = list(parsed["commands"]["list"]["exclude"])
    assert len(excludes) == 1

    events_filter = parsed["commands"]["events"]["filter"]
    assert events_filter == first_events_filter
