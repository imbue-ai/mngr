import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())

# The cheapest model alias, used by every live SDK test to keep costs down.
SDK_LIVE_MODEL = "haiku"


@pytest.fixture
def local_host(local_provider: LocalProviderInstance) -> Host:
    """Local-provider Host for tests that exercise host.read_text_file / host.write_file."""
    return local_provider.create_host(HostName(LOCAL_HOST_NAME))


@pytest.fixture
def sdk_live_model() -> str:
    return SDK_LIVE_MODEL


@pytest.fixture
def sdk_cwd(tmp_path: Path) -> Path:
    """An isolated working directory for live SDK tests.

    Running the agent in a fresh temp dir (combined with ``setting_sources=[]``) keeps the
    tests hermetic: the agent does not pick up this repo's CLAUDE.md, .claude/ hooks, or git
    state, which would otherwise derail the prompts.
    """
    return tmp_path


def pytest_collection_modifyitems(config: pytest.Config, items: Sequence[pytest.Item]) -> None:
    # The live SDK suite is opt-in: it makes real, paid API calls and is never run in CI.
    # Skip every `sdk_live`-marked test unless the runner explicitly enabled it AND a key is present.
    is_explicitly_enabled = os.environ.get("RUN_SDK_LIVE_TESTS") == "1"
    is_api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if is_explicitly_enabled and is_api_key_present:
        return
    skip_reason = (
        "ANTHROPIC_API_KEY must be set to run the live SDK tests"
        if is_explicitly_enabled
        else "live SDK tests are opt-in; set RUN_SDK_LIVE_TESTS=1 (and ANTHROPIC_API_KEY) to run them"
    )
    skip_marker = pytest.mark.skip(reason=skip_reason)
    for item in items:
        if item.get_closest_marker("sdk_live") is not None:
            item.add_marker(skip_marker)
