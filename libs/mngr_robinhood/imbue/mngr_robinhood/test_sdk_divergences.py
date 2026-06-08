"""Living demonstrations of the mngr-backed Agent SDK divergences catalogued in
``docs/sdk_divergences.md``.

Each test asserts the behavior the *real* ``claude_agent_sdk`` documents/exhibits, and is marked
``xfail(strict=True)`` for the mngr target via :func:`_xfail_for_mngr`. So a normal run is green
(real PASS, mngr XFAIL), while ``pytest --runxfail ...`` shows the underlying "real passes / mngr
fails" split that proves the divergence. When a divergence is fixed, the corresponding mngr case
flips to XPASS and the strict marker fails the run -- a built-in reminder to retire the xfail.

The whole module is gated behind the ``sdk_live`` marker (so it never runs in CI) for consistency
with the rest of the live SDK suite; the structural tests (items 1, 6, 7, 8, 9) need no network and
the two behavioral tests (items 3 and 18) make one cheap haiku call each.

The divergence numbers below refer to the entries in ``docs/sdk_divergences.md``.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

import claude_agent_sdk
import pytest
from claude_agent_sdk import CLIConnectionError
from claude_agent_sdk import PermissionResultAllow
from claude_agent_sdk import ResultMessage

from imbue.mngr_robinhood.testing import find_result_message
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.asyncio, pytest.mark.timeout(600)]


def _xfail_for_mngr(request: pytest.FixtureRequest, is_mngr_sdk: bool, divergence: str) -> None:
    """Mark the current (parametrized) test as a strict expected-failure for the mngr target.

    The real-SDK parametrization runs normally; the mngr parametrization is expected to fail
    (the divergence). ``--runxfail`` disables this so the real pass / mngr fail split is visible.
    """
    if is_mngr_sdk:
        request.node.add_marker(pytest.mark.xfail(reason=divergence, strict=True))


# --- divergence 1: public surface is fully re-exported --------------------------------------


async def test_divergence_1_reexports_every_public_name(
    sdk: ModuleType, is_mngr_sdk: bool, request: pytest.FixtureRequest
) -> None:
    _xfail_for_mngr(request, is_mngr_sdk, "divergence 1: not every claude_agent_sdk public name is re-exported")
    missing = sorted(name for name in claude_agent_sdk.__all__ if not hasattr(sdk, name))
    assert missing == []


# --- divergence 6: ClaudeSDKClient exposes every documented control method -------------------


@pytest.mark.parametrize(
    "method_name",
    ["rewind_files", "reconnect_mcp_server", "toggle_mcp_server", "stop_task", "get_context_usage"],
)
async def test_divergence_6_client_exposes_documented_methods(
    sdk: ModuleType, is_mngr_sdk: bool, request: pytest.FixtureRequest, method_name: str
) -> None:
    _xfail_for_mngr(request, is_mngr_sdk, f"divergence 6: ClaudeSDKClient is missing {method_name}()")
    assert method_name in dir(sdk.ClaudeSDKClient)


# --- divergence 7: ClaudeSDKClient accepts the documented transport= argument ----------------


async def test_divergence_7_client_accepts_transport_argument(
    sdk: ModuleType, is_mngr_sdk: bool, request: pytest.FixtureRequest
) -> None:
    _xfail_for_mngr(request, is_mngr_sdk, "divergence 7: ClaudeSDKClient.__init__ rejects transport=")
    # The documented signature is ClaudeSDKClient(options=None, transport=None). Constructing with
    # transport=None must not raise (no connection is made).
    sdk.ClaudeSDKClient(options=None, transport=None)


# --- divergence 8: query() accepts the documented transport= argument ------------------------


async def test_divergence_8_query_accepts_transport_argument(
    sdk: ModuleType, is_mngr_sdk: bool, request: pytest.FixtureRequest
) -> None:
    _xfail_for_mngr(request, is_mngr_sdk, "divergence 8: query() rejects transport=")
    # The documented signature is query(*, prompt, options=None, transport=None). Calling it with
    # transport= must return an async iterator without raising TypeError (we never iterate it, so
    # no API call is made).
    stream = sdk.query(prompt="unused", transport=None)
    assert hasattr(stream, "__anext__")
    await stream.aclose()


# --- divergence 9: the not-connected error is CLIConnectionError -----------------------------


async def test_divergence_9_query_before_connect_raises_cli_connection_error(
    sdk: ModuleType, is_mngr_sdk: bool, request: pytest.FixtureRequest
) -> None:
    _xfail_for_mngr(request, is_mngr_sdk, "divergence 9: not-connected raises RobinhoodError, not CLIConnectionError")
    client = sdk.ClaudeSDKClient()
    # Documented contract: calling query() before connect() raises CLIConnectionError.
    with pytest.raises(CLIConnectionError):
        await client.query("this should fail because connect() was never called")


# --- divergence 3: can_use_tool is not consulted for auto-allowed tools (LIVE) --------------


@pytest.mark.tmux
async def test_divergence_3_can_use_tool_not_invoked_when_auto_allowed(
    sdk: ModuleType, is_mngr_sdk: bool, request: pytest.FixtureRequest, sdk_live_model: str, sdk_cwd: Path
) -> None:
    _xfail_for_mngr(request, is_mngr_sdk, "divergence 3: can_use_tool fires for every tool, even auto-allowed ones")
    recorded_tool_names: list[str] = []

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> PermissionResultAllow:
        recorded_tool_names.append(tool_name)
        return PermissionResultAllow()

    # bypassPermissions auto-allows every tool, so the real SDK never routes a permission "ask" to
    # the callback. can_use_tool requires the streaming-input prompt form.
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="bypassPermissions", can_use_tool=can_use_tool)

    async def _prompt_stream() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": "Use the Bash tool to run exactly this command and nothing else: echo DIVERGENCEMARKER",
            },
        }

    messages = [message async for message in sdk.query(prompt=_prompt_stream(), options=options)]
    # Sanity: the turn completed.
    assert any(isinstance(message, ResultMessage) for message in messages)
    # The documented contract: auto-allowed tools never reach can_use_tool.
    assert recorded_tool_names == []


# --- divergence 18: ResultMessage.stop_reason is populated (LIVE) ----------------------------


@pytest.mark.tmux
async def test_divergence_18_result_message_has_stop_reason(
    sdk: ModuleType, is_mngr_sdk: bool, request: pytest.FixtureRequest, sdk_live_model: str, sdk_cwd: Path
) -> None:
    _xfail_for_mngr(request, is_mngr_sdk, "divergence 18: synthesized ResultMessage.stop_reason is always None")
    options = make_sdk_options(sdk_live_model, sdk_cwd)
    messages = [message async for message in sdk.query(prompt="Reply with exactly the word OK.", options=options)]
    result = find_result_message(messages)
    # The real CLI result event carries a terminal stop_reason (e.g. "end_turn").
    assert result.stop_reason is not None
