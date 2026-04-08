import pytest


@pytest.fixture
def enabled_plugins(all_agent_type_plugins: frozenset[str]) -> frozenset[str]:
    return all_agent_type_plugins
