import os
import shutil
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_grok.plugin import GrokAgentConfig
from imbue.mngr_grok.testing import GrokStubModelServer
from imbue.mngr_grok.testing import GrokStubServerConfig
from imbue.mngr_grok.testing import build_egress_tripwire_environment
from imbue.mngr_grok.testing import build_stub_grok_config
from imbue.mngr_grok.testing import find_pinned_grok_mismatch
from imbue.mngr_grok.testing import is_pinned_grok_required
from imbue.mngr_grok.testing import running_grok_stub_model_server

suppress_warnings()
register_conftest_hooks(globals())
register_plugin_test_fixtures(globals())


@pytest.fixture(autouse=True)
def remove_inherited_grok_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every inherited ``GROK_*`` variable, such as ``GROK_HOME``, ``GROK_AGENT`` and ``GROK_AUTH_PATH``, and ``XAI_API_KEY``."""
    for name in tuple(os.environ):
        if name.startswith("GROK_") or name == "XAI_API_KEY":
            monkeypatch.delenv(name)


@pytest.fixture
def pinned_grok_binary() -> Path:
    """The `grok` on PATH at the version GROK_VERSION_PIN_PATH pins.

    Without it the test skips, or fails when MNGR_GROK_REQUIRE_PINNED_BINARY=1.
    """
    grok_path = shutil.which("grok")
    mismatch = find_pinned_grok_mismatch(grok_path)
    if grok_path is not None and mismatch is None:
        return Path(grok_path)
    reason = f"These tests drive the real pinned grok: {mismatch}"
    if is_pinned_grok_required():
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture
def grok_stub_server(tmp_path: Path) -> Iterator[GrokStubModelServer]:
    """A running stub model server for the contract tests, logging every request to the test's directory."""
    config = GrokStubServerConfig(
        request_log_path=tmp_path / "grok_stub_requests.jsonl",
        slow_reply_seconds=PositiveFloat(15.0),
        background_shell_seconds=PositiveInt(3),
    )
    with running_grok_stub_model_server(config) as server:
        yield server


@pytest.fixture
def build_hermetic_grok_agent_environment() -> Callable[[str], dict[str, str]]:
    """Builds a grok agent's environment from the stub's origin: every proxy and xAI origin is the stub."""

    def build(stub_origin_url: str) -> dict[str, str]:
        return {
            **build_egress_tripwire_environment(stub_origin_url),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "BROWSER": "/usr/bin/false",
        }

    return build


@pytest.fixture
def hermetic_stub_grok_config(
    grok_stub_server: GrokStubModelServer, build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]]
) -> GrokAgentConfig:
    """The grok agent type on the stub model, with the hermetic environment pointing every origin at the stub."""
    return build_stub_grok_config(
        grok_stub_server, build_hermetic_grok_agent_environment(grok_stub_server.get_origin_url())
    )
