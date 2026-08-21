from __future__ import annotations

import pytest

from infrastructure.websocket.consumers import CLOSE_INVALID_FRAME, HeartbeatConsumerMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [(None, b"binary"), ("not-json", None)])
async def test_malformed_frames_close_and_cleanup(frame):
    consumer = HeartbeatConsumerMixin()
    consumer._heartbeat_task = None
    events: list[object] = []

    async def discard_groups() -> None:
        events.append("discard")

    async def close(*, code: int) -> None:
        events.append(code)

    consumer._discard_groups = discard_groups  # type: ignore[method-assign]
    consumer.close = close  # type: ignore[method-assign]

    await consumer.receive(text_data=frame[0], bytes_data=frame[1])

    assert events == ["discard", CLOSE_INVALID_FRAME]
