import queue
from typing import Any

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_robinhood._agent_sdk.client import _DRAIN_SENTINEL
from imbue.mngr_robinhood._agent_sdk.client import _TurnMessageStream


async def _collect(stream: _TurnMessageStream) -> list[Any]:
    collected: list[Any] = []
    async for message in stream:
        collected.append(message)
    return collected


@pytest.mark.asyncio
async def test_turn_message_stream_yields_until_sentinel() -> None:
    message_queue: queue.Queue[Any] = queue.Queue()
    message_queue.put("first")
    message_queue.put("second")
    message_queue.put(_DRAIN_SENTINEL)
    stream = _TurnMessageStream(message_queue=message_queue)
    assert await _collect(stream) == ["first", "second"]


@pytest.mark.asyncio
async def test_turn_message_stream_reraises_drain_error_at_end_of_turn() -> None:
    # A drain failure must surface to the consumer rather than looking like a clean end-of-turn.
    message_queue: queue.Queue[Any] = queue.Queue()
    message_queue.put("partial")
    message_queue.put(_DRAIN_SENTINEL)
    stream = _TurnMessageStream(message_queue=message_queue)
    stream.drain_error = MngrError("drain blew up")
    with pytest.raises(MngrError, match="drain blew up"):
        await _collect(stream)
