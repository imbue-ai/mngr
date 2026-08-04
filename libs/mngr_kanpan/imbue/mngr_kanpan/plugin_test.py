"""Unit tests for the kanpan plugin's muted field generators.

The online generator (`_muted_online_field`) is exercised end-to-end by the
acceptance tests (a real local agent flows through `list_agents`). The offline
generator (`_muted_offline_field`) reads the persisted `plugin.kanpan.muted`
bit straight off a `DiscoveredAgent`'s certified data, so it is covered here at
the unit level.
"""

from typing import Any

from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import make_test_agent_details
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_sources.github import GitHubDataSource
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.plugin import _muted_offline_field
from imbue.mngr_kanpan.plugin import kanpan_data_sources
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_config

# The offline generator ignores its host_details argument; any valid HostDetails works.
_HOST_DETAILS = make_test_agent_details().host


def _offline_ref(certified_data: dict[str, Any]) -> DiscoveredAgent:
    """Build a DiscoveredAgent with the given certified data for offline-generator tests."""
    return DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("offline-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data=certified_data,
    )


def test_muted_offline_field_true_when_muted() -> None:
    ref = _offline_ref({"plugin": {"kanpan": {"muted": True}}})
    assert _muted_offline_field(ref, _HOST_DETAILS) is True


def test_muted_offline_field_none_when_explicitly_unmuted() -> None:
    # The generator is sparse: it returns None (omitting the field) rather than
    # False, so the board reads it back as unmuted via its `.get(..., False)`.
    ref = _offline_ref({"plugin": {"kanpan": {"muted": False}}})
    assert _muted_offline_field(ref, _HOST_DETAILS) is None


def test_muted_offline_field_none_when_certified_data_empty() -> None:
    assert _muted_offline_field(_offline_ref({}), _HOST_DETAILS) is None


def test_muted_offline_field_none_when_no_kanpan_plugin() -> None:
    ref = _offline_ref({"plugin": {}})
    assert _muted_offline_field(ref, _HOST_DETAILS) is None


def test_muted_offline_field_none_when_no_muted_key() -> None:
    ref = _offline_ref({"plugin": {"kanpan": {}}})
    assert _muted_offline_field(ref, _HOST_DETAILS) is None


# === data source config from settings ===


def _github_source(kanpan_settings: dict[str, Any]) -> GitHubDataSource:
    """Build the GitHub data source the way a settings file would, and return it.

    Goes through `kanpan_data_sources`, the hook mngr actually calls, so the
    settings-to-column path is exercised rather than the config object alone.
    """
    config = KanpanPluginConfig(**kanpan_settings)
    sources = kanpan_data_sources(mngr_ctx=make_mngr_ctx_with_config(config))
    assert sources is not None
    return next(source for source in sources if isinstance(source, GitHubDataSource))


def test_github_data_source_ships_the_prs_column_with_no_settings_at_all() -> None:
    """The column needs no opting in, so an untouched settings file still gets it."""
    assert _github_source({}).columns[FIELD_PR] == "PRS"


def test_github_data_source_settings_leave_the_other_fields_alone() -> None:
    """Turning one github field off must not disturb the ones that default on."""
    source = _github_source({"data_sources": {"github": {"unresolved": False}}})
    assert source.config.unresolved is False
    assert source.config.pr is True
    assert source.config.ci is True
    assert source.config.conflicts is True
