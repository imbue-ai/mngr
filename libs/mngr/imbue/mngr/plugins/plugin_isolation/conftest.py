import pytest

from imbue.mngr.plugin_catalog import PLUGIN_CATALOG


@pytest.fixture
def no_plugins() -> frozenset[str]:
    """Empty plugin set for tests that need zero plugins."""
    return frozenset()


@pytest.fixture
def all_agent_type_plugins() -> frozenset[str]:
    """All plugins that register agent types (claude, opencode, etc.)."""
    return frozenset(
        e.entry_point_name
        for e in PLUGIN_CATALOG
        if e.entry_point_name
        in ("claude", "opencode", "pi_coding", "llm", "code_guardian", "fixme_fairy", "headless_claude", "claude_mind")
    )


@pytest.fixture
def claude_only_plugins() -> frozenset[str]:
    """Only the claude plugin enabled."""
    return frozenset({"claude"})
