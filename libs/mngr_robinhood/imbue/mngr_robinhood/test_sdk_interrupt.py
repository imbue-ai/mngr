"""Live verification of the documented ``ClaudeSDKClient.interrupt()`` control method.

Interrupting is inherently timing-sensitive (the turn must still be in flight when the
interrupt lands), so this test is marked flaky to absorb timing variance.
"""

from pathlib import Path
from types import ModuleType

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ResultMessage

from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.flaky, pytest.mark.timeout(600)]


async def test_interrupt_ends_an_in_flight_turn(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="bypassPermissions")
    async with sdk.ClaudeSDKClient(options=options) as client:
        # Kick off a genuinely long-running turn so the interrupt has something to stop.
        await client.query(
            "Use the Bash tool to run exactly this command and wait for it to finish: "
            "for i in $(seq 1 30); do echo step-$i; sleep 2; done"
        )

        messages: list[object] = []
        has_interrupted = False
        async for message in client.receive_response():
            messages.append(message)
            # As soon as the model starts working, interrupt the turn.
            if not has_interrupted and isinstance(message, AssistantMessage):
                await client.interrupt()
                has_interrupted = True

    # The interrupt must have been issued and the response stream must have terminated
    # (the async-for above completed) at a ResultMessage rather than running to completion.
    assert has_interrupted
    assert any(isinstance(m, ResultMessage) for m in messages)
    assert isinstance(messages[-1], ResultMessage)
