import pytest


@pytest.fixture
def enabled_plugins(claude_only_plugins: frozenset[str]) -> frozenset[str]:
    return claude_only_plugins
