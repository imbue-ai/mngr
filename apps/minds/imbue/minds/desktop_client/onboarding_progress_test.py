from pathlib import Path

from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.onboarding_progress import resolve_is_onboarding_complete
from imbue.mngr.primitives import AgentId


def test_no_config_store_reads_as_complete(tmp_path: Path) -> None:
    resolver = make_resolver_with_data(agents_json=None)

    assert resolve_is_onboarding_complete(None, resolver) is True


def test_fresh_install_with_no_workspaces_is_incomplete(tmp_path: Path) -> None:
    config = MindsConfig(data_dir=tmp_path)
    resolver = make_resolver_with_data(agents_json=None)

    assert resolve_is_onboarding_complete(config, resolver) is False
    assert config.get_is_onboarding_complete() is False


def test_persisted_flag_wins(tmp_path: Path) -> None:
    config = MindsConfig(data_dir=tmp_path)
    config.set_is_onboarding_complete(True)
    resolver = make_resolver_with_data(agents_json=None)

    assert resolve_is_onboarding_complete(config, resolver) is True


def test_existing_workspace_backfills_the_flag(tmp_path: Path) -> None:
    config = MindsConfig(data_dir=tmp_path)
    resolver = make_resolver_with_data(agents_json=make_agents_json(AgentId.generate()))

    assert resolve_is_onboarding_complete(config, resolver) is True
    # The backfill is written through, so a later launch with every workspace
    # destroyed still reads as complete.
    assert config.get_is_onboarding_complete() is True
