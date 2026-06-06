import os
import subprocess
from collections.abc import Iterator
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import claude_agent_sdk
import pytest

from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.testing import setup_claude_trust_config_for_subprocess
from imbue.mngr_robinhood import agent_sdk as mngr_agent_sdk
from imbue.mngr_robinhood._agent_sdk.sessions import destroy_sessions_in_directory
from imbue.resource_guards.resource_guards import fixture_uses_resources

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


@pytest.fixture(params=[claude_agent_sdk, mngr_agent_sdk], ids=["real_sdk", "mngr_sdk"])
def sdk(request: pytest.FixtureRequest) -> ModuleType:
    """The SDK implementation module under test.

    Parametrized over the real ``claude_agent_sdk`` and the mngr-backed
    ``imbue.mngr_robinhood.agent_sdk`` so every test that uses ``sdk.query`` / ``sdk.ClaudeSDKClient``
    / the session functions runs against both targets and asserts the same documented contract.
    The message/block/option *types* are identical objects across both (the mngr module
    re-exports them), so tests keep importing those directly from ``claude_agent_sdk``.
    """
    return request.param


@pytest.fixture
@fixture_uses_resources("tmux")
def _sdk_tmux_guard() -> None:
    """Satisfy the tmux resource guard uniformly across the live SDK suite.

    The mngr SDK target drives an interactive claude agent that spawns tmux, so all SDK tests
    carry ``@pytest.mark.tmux``. The resource guard, however, fails a *passing* tmux-marked test
    that never actually invokes tmux -- which the real-SDK target (and the no-agent
    error-contract tests) otherwise would. This fixture (pulled in by every live test via
    ``sdk_cwd``) touches tmux once and declares it via ``@fixture_uses_resources``, so the guard
    treats tmux as legitimately exercised by the suite's harness regardless of target.
    """
    subprocess.run(["tmux", "-V"], check=False, capture_output=True, timeout=10.0)


@pytest.fixture
def is_mngr_sdk(sdk: ModuleType) -> bool:
    """True when the current ``sdk`` target is the mngr-backed implementation."""
    return sdk is mngr_agent_sdk


@pytest.fixture
def requires_native_sdk(is_mngr_sdk: bool) -> None:
    """Skip a test for the mngr target when it exercises a surface the mngr transport cannot support.

    Used by tests of features that are inherently unavailable through mngr's transcript-based
    transport (e.g. in-process ``can_use_tool`` / ``hooks`` callbacks, ``interrupt``, partial
    ``StreamEvent`` streaming, live ``get_server_info``). These still run against the real SDK.
    """
    if is_mngr_sdk:
        pytest.skip("not supported by the mngr-backed Agent SDK transport")


@pytest.fixture
def sdk_cwd(tmp_path: Path, is_mngr_sdk: bool, _sdk_tmux_guard: None) -> Iterator[Path]:
    """An isolated working directory for live SDK tests.

    Running the agent in a fresh temp dir (combined with ``setting_sources=[]``) keeps the
    tests hermetic: the agent does not pick up this repo's CLAUDE.md, .claude/ hooks, or git
    state, which would otherwise derail the prompts. For the mngr target, any SDK agents created
    under this directory are destroyed on teardown (the SDK only *stops* them by default).

    The mngr target runs claude *interactively* (in tmux), so -- unlike the real SDK's
    ``--print`` transport -- it would otherwise hang on claude's first-run TUI prompts (trust
    dialog, custom-API-key confirmation). ``setup_claude_trust_config_for_subprocess`` writes a
    ``~/.claude.json`` (in the autouse temp HOME) that pre-accepts onboarding, trusts the cwd,
    and approves the ``ANTHROPIC_API_KEY`` so the agent boots non-interactively.
    """
    if is_mngr_sdk:
        setup_claude_trust_config_for_subprocess(trusted_paths=[tmp_path])
    yield tmp_path
    if is_mngr_sdk:
        destroy_sessions_in_directory(str(tmp_path))


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
