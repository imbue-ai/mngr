import pytest


@pytest.fixture
def enabled_plugins(no_plugins: frozenset[str]) -> frozenset[str]:
    return no_plugins
